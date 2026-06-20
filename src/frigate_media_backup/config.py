from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when configuration is invalid."""


@dataclass(frozen=True)
class FrigateConfig:
    base_url: str
    username: str | None = None
    password: str | None = None
    password_file: Path | None = None
    verify_tls: bool = True
    ca_bundle: Path | None = None
    request_timeout_seconds: float = 60

    @property
    def password_value(self) -> str | None:
        return read_secret(self.password, self.password_file)

    @property
    def needs_auth(self) -> bool:
        return bool(self.username or self.password or self.password_file)


@dataclass(frozen=True)
class MqttConfig:
    host: str
    port: int = 1883
    topic_prefix: str = "frigate"
    username: str | None = None
    password: str | None = None
    password_file: Path | None = None
    client_id: str = "frigate-media-backup"
    keepalive_seconds: int = 60

    @property
    def password_value(self) -> str | None:
        return read_secret(self.password, self.password_file)


@dataclass(frozen=True)
class StateConfig:
    path: Path
    tmp_dir: Path


@dataclass(frozen=True)
class UploadsConfig:
    include_snapshots: bool = True
    include_clips: bool = True
    clip_padding_before_seconds: float = 5
    clip_padding_after_seconds: float = 5


@dataclass(frozen=True)
class DestinationConfig:
    type: str
    name: str
    options: dict[str, Any]


@dataclass(frozen=True)
class AppConfig:
    frigate: FrigateConfig
    mqtt: MqttConfig
    state: StateConfig
    uploads: UploadsConfig
    destinations: list[DestinationConfig]


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ConfigError("Top-level config must be a mapping")
    return parse_config(raw)


def parse_config(raw: dict[str, Any]) -> AppConfig:
    frigate_raw = require_mapping(raw, "frigate")
    mqtt_raw = require_mapping(raw, "mqtt")
    state_raw = require_mapping(raw, "state")
    uploads_raw = raw.get("uploads") or {}
    if not isinstance(uploads_raw, dict):
        raise ConfigError("uploads must be a mapping")
    destinations_raw = raw.get("destinations")
    if not isinstance(destinations_raw, list) or not destinations_raw:
        raise ConfigError("destinations must be a non-empty list")

    frigate = FrigateConfig(
        base_url=require_str(frigate_raw, "base_url").rstrip("/"),
        username=optional_str(frigate_raw, "username"),
        password=optional_str(frigate_raw, "password"),
        password_file=optional_path(frigate_raw, "password_file"),
        verify_tls=bool(frigate_raw.get("verify_tls", True)),
        ca_bundle=optional_path(frigate_raw, "ca_bundle"),
        request_timeout_seconds=float(frigate_raw.get("request_timeout_seconds", 60)),
    )
    if frigate.needs_auth and not (frigate.username and frigate.password_value):
        raise ConfigError("Frigate auth requires both username and password/password_file")

    mqtt = MqttConfig(
        host=require_str(mqtt_raw, "host"),
        port=int(mqtt_raw.get("port", 1883)),
        topic_prefix=str(mqtt_raw.get("topic_prefix", "frigate")).strip("/"),
        username=optional_str(mqtt_raw, "username"),
        password=optional_str(mqtt_raw, "password"),
        password_file=optional_path(mqtt_raw, "password_file"),
        client_id=str(mqtt_raw.get("client_id", "frigate-media-backup")),
        keepalive_seconds=int(mqtt_raw.get("keepalive_seconds", 60)),
    )
    if (mqtt.username or mqtt.password or mqtt.password_file) and not (
        mqtt.username and mqtt.password_value
    ):
        raise ConfigError("MQTT auth requires both username and password/password_file")

    state = StateConfig(
        path=Path(require_str(state_raw, "path")),
        tmp_dir=Path(require_str(state_raw, "tmp_dir")),
    )
    uploads = UploadsConfig(
        include_snapshots=bool(uploads_raw.get("include_snapshots", True)),
        include_clips=bool(uploads_raw.get("include_clips", True)),
        clip_padding_before_seconds=float(uploads_raw.get("clip_padding_before_seconds", 5)),
        clip_padding_after_seconds=float(uploads_raw.get("clip_padding_after_seconds", 5)),
    )
    destinations = [parse_destination(item, i) for i, item in enumerate(destinations_raw)]
    names = [destination.name for destination in destinations]
    if len(set(names)) != len(names):
        raise ConfigError("destination names must be unique")

    return AppConfig(
        frigate=frigate,
        mqtt=mqtt,
        state=state,
        uploads=uploads,
        destinations=destinations,
    )


def parse_destination(raw: Any, index: int) -> DestinationConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"destination {index} must be a mapping")
    destination_type = require_str(raw, "type")
    name = str(raw.get("name") or destination_type)
    options = {key: value for key, value in raw.items() if key not in {"type", "name"}}
    return DestinationConfig(type=destination_type, name=name, options=options)


def require_mapping(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"{key} must be a mapping")
    return value


def require_str(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{key} must be a non-empty string")
    return value


def optional_str(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string when set")
    return value


def optional_path(raw: dict[str, Any], key: str) -> Path | None:
    value = optional_str(raw, key)
    return Path(value) if value else None


def read_secret(value: str | None, path: Path | None) -> str | None:
    if value and path:
        raise ConfigError("Set either inline secret or secret file, not both")
    if value:
        return value
    if not path:
        return None
    return path.read_text(encoding="utf-8").strip()


def env_secret(env_name: str) -> str:
    value = os.environ.get(env_name)
    if not value:
        raise ConfigError(f"Environment variable {env_name} is required")
    return value

