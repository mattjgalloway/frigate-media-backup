from __future__ import annotations

import itertools
import logging
import queue
import signal
import threading
from types import FrameType

import paho.mqtt.client as mqtt

from .config import MqttConfig
from .events import BackupEvent, parse_mqtt_message
from .service import BackupService

LOGGER = logging.getLogger(__name__)
STOP_WORKER = object()


class MqttRunner:
    def __init__(
        self,
        config: MqttConfig,
        service: BackupService,
        startup_events: list[BackupEvent] | None = None,
    ) -> None:
        self.config = config
        self.service = service
        self.startup_events = startup_events or []
        self.stop_event = threading.Event()
        self.event_queue: queue.PriorityQueue[tuple[int, int, BackupEvent | object]] = (
            queue.PriorityQueue()
        )
        self.sequence = itertools.count()
        self.worker_thread: threading.Thread | None = None
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=config.client_id)
        if config.username:
            self.client.username_pw_set(config.username, config.password_value)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        self.start_worker()
        self.enqueue_startup_events()
        try:
            self.client.connect(self.config.host, self.config.port, self.config.keepalive_seconds)
            self.client.loop_start()
            self.stop_event.wait()
        finally:
            self.client.loop_stop()
            self.client.disconnect()
            self.stop_worker()

    def stop(self, _signum: int, _frame: FrameType | None) -> None:
        self.stop_event.set()

    def start_worker(self) -> None:
        if self.worker_thread is not None:
            return
        self.worker_thread = threading.Thread(
            target=self.process_events,
            name="frigate-media-backup-worker",
            daemon=True,
        )
        self.worker_thread.start()

    def stop_worker(self) -> None:
        if self.worker_thread is None:
            return
        self.event_queue.put((-1, next(self.sequence), STOP_WORKER))
        self.worker_thread.join()
        self.worker_thread = None

    def enqueue_startup_events(self) -> None:
        for event in self.startup_events:
            self.enqueue_event(event, priority=10)
        if self.startup_events:
            LOGGER.info("Queued %s startup backfill event(s)", len(self.startup_events))

    def enqueue_event(self, event: BackupEvent, *, priority: int = 0) -> None:
        self.event_queue.put((priority, next(self.sequence), event))

    def process_events(self) -> None:
        while True:
            _priority, _sequence, event = self.event_queue.get()
            try:
                if event is STOP_WORKER:
                    return
                self.service.handle_event(event)
            except Exception:
                LOGGER.exception("Failed to process queued backup event")
            finally:
                self.event_queue.task_done()

    def on_connect(
        self,
        _client: mqtt.Client,
        _userdata: object,
        _flags: mqtt.ConnectFlags,
        reason_code: mqtt.ReasonCode,
        _properties: mqtt.Properties | None,
    ) -> None:
        if reason_code != 0:
            LOGGER.error("MQTT connection failed: %s", reason_code)
            return
        topic = f"{self.config.topic_prefix}/#"
        LOGGER.info("Connected to MQTT; subscribing to %s", topic)
        self.client.subscribe(topic)

    def on_message(
        self,
        _client: mqtt.Client,
        _userdata: object,
        message: mqtt.MQTTMessage,
    ) -> None:
        try:
            event = parse_mqtt_message(message.topic, message.payload, self.config.topic_prefix)
            if event is None:
                return
            self.enqueue_event(event)
        except Exception:
            LOGGER.exception("Failed to handle MQTT message from %s", message.topic)
