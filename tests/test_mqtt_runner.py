from __future__ import annotations

import json
from dataclasses import dataclass

from frigate_media_backup.config import MqttConfig
from frigate_media_backup.events import ClipEvent
from frigate_media_backup.mqtt_runner import MqttRunner


class FakeService:
    def __init__(self) -> None:
        self.events: list[ClipEvent] = []

    def handle_event(self, event: ClipEvent) -> None:
        self.events.append(event)


@dataclass(frozen=True)
class FakeMqttMessage:
    topic: str
    payload: bytes


def test_mqtt_message_is_queued_without_processing_immediately() -> None:
    service = FakeService()
    runner = MqttRunner(MqttConfig(host="mosquitto"), service)  # type: ignore[arg-type]

    runner.on_message(None, None, review_message("review-live"))  # type: ignore[arg-type]

    assert service.events == []
    assert runner.event_queue.qsize() == 1

    runner.start_worker()
    runner.event_queue.join()
    runner.stop_worker()

    assert [event.event_id for event in service.events] == ["review-live"]


def test_live_mqtt_events_are_prioritised_over_startup_backfill() -> None:
    service = FakeService()
    runner = MqttRunner(
        MqttConfig(host="mosquitto"),
        service,  # type: ignore[arg-type]
        startup_events=[ClipEvent("startup-1", "front", 10.0, 20.0)],
    )

    runner.enqueue_startup_events()
    runner.on_message(None, None, review_message("live-1"))  # type: ignore[arg-type]

    runner.start_worker()
    runner.event_queue.join()
    runner.stop_worker()

    assert [event.event_id for event in service.events] == ["live-1", "startup-1"]


def review_message(review_id: str) -> FakeMqttMessage:
    payload = {
        "type": "end",
        "after": {
            "id": review_id,
            "camera": "front",
            "start_time": 100.0,
            "end_time": 110.0,
        },
    }
    return FakeMqttMessage("frigate/reviews", json.dumps(payload).encode())
