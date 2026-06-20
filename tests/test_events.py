import json

from frigate_media_backup.events import ClipEvent, SnapshotEvent, parse_mqtt_message


def test_parse_snapshot_topic() -> None:
    event = parse_mqtt_message("frigate/doorbell/person/snapshot", b"jpg", "frigate")

    assert isinstance(event, SnapshotEvent)
    assert event.camera == "doorbell"
    assert event.object_label == "person"
    assert event.to_artifact().relative_path.startswith("doorbell/snapshots/")


def test_parse_review_end_message() -> None:
    payload = {
        "type": "end",
        "after": {
            "id": "review-1",
            "camera": "garden",
            "start_time": 100.0,
            "end_time": 120.0,
        },
    }

    event = parse_mqtt_message("frigate/reviews", json.dumps(payload).encode(), "frigate")

    assert event == ClipEvent(
        event_id="review-1",
        camera="garden",
        start_time=100.0,
        end_time=120.0,
    )


def test_parse_review_ignores_updates() -> None:
    payload = {
        "type": "update",
        "after": {
            "id": "review-1",
            "camera": "garden",
            "start_time": 100.0,
            "end_time": None,
        },
    }

    assert parse_mqtt_message("frigate/reviews", json.dumps(payload).encode(), "frigate") is None

