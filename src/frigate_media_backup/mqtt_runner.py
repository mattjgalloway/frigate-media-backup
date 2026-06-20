from __future__ import annotations

import logging
import signal
import threading
from types import FrameType

import paho.mqtt.client as mqtt

from .config import MqttConfig
from .events import parse_mqtt_message
from .service import BackupService

LOGGER = logging.getLogger(__name__)


class MqttRunner:
    def __init__(self, config: MqttConfig, service: BackupService) -> None:
        self.config = config
        self.service = service
        self.stop_event = threading.Event()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=config.client_id)
        if config.username:
            self.client.username_pw_set(config.username, config.password_value)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        self.client.connect(self.config.host, self.config.port, self.config.keepalive_seconds)
        self.client.loop_start()
        self.stop_event.wait()
        self.client.loop_stop()
        self.client.disconnect()

    def stop(self, _signum: int, _frame: FrameType | None) -> None:
        self.stop_event.set()

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
            self.service.handle_event(event)
        except Exception:
            LOGGER.exception("Failed to handle MQTT message from %s", message.topic)

