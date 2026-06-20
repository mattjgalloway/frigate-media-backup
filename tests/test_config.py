from pathlib import Path

import pytest

from frigate_media_backup.config import ConfigError, load_config, parse_config


def base_config(tmp_path: Path) -> dict[str, object]:
    return {
        "frigate": {"base_url": "http://frigate:5000/"},
        "mqtt": {"host": "mosquitto"},
        "state": {
            "path": str(tmp_path / "state.sqlite"),
            "tmp_dir": str(tmp_path / "tmp"),
        },
        "destinations": [{"type": "filesystem", "name": "local", "path": str(tmp_path)}],
    }


def test_parse_config_trims_frigate_base_url(tmp_path: Path) -> None:
    config = parse_config(base_config(tmp_path))

    assert config.frigate.base_url == "http://frigate:5000"
    assert config.mqtt.topic_prefix == "frigate"
    assert config.destinations[0].name == "local"


def test_frigate_auth_requires_username_and_password(tmp_path: Path) -> None:
    raw = base_config(tmp_path)
    raw["frigate"] = {"base_url": "https://frigate:8971", "username": "backup"}

    with pytest.raises(ConfigError, match="Frigate auth requires"):
        parse_config(raw)


def test_password_file_is_loaded(tmp_path: Path) -> None:
    password_file = tmp_path / "password"
    password_file.write_text("secret\n", encoding="utf-8")
    raw = base_config(tmp_path)
    raw["frigate"] = {
        "base_url": "https://frigate:8971",
        "username": "backup",
        "password_file": str(password_file),
    }

    config = parse_config(raw)

    assert config.frigate.password_value == "secret"


def test_nested_upload_controls_are_parsed(tmp_path: Path) -> None:
    raw = base_config(tmp_path)
    raw["uploads"] = {
        "snapshots": {
            "enabled": True,
            "cameras": ["front"],
            "objects": ["person"],
            "min_interval_seconds": 30,
        },
        "clips": {
            "enabled": True,
            "cameras": ["garden"],
            "padding_before_seconds": 10,
            "padding_after_seconds": 15,
        },
    }

    config = parse_config(raw)

    assert config.uploads.snapshots.enabled is True
    assert config.uploads.snapshots.cameras == ("front",)
    assert config.uploads.snapshots.objects == ("person",)
    assert config.uploads.snapshots.min_interval_seconds == 30
    assert config.uploads.clips.cameras == ("garden",)
    assert config.uploads.clips.padding_before_seconds == 10
    assert config.uploads.clips.padding_after_seconds == 15


def test_destination_names_must_be_unique(tmp_path: Path) -> None:
    raw = base_config(tmp_path)
    raw["destinations"] = [
        {"type": "filesystem", "name": "local", "path": str(tmp_path / "a")},
        {"type": "filesystem", "name": "local", "path": str(tmp_path / "b")},
    ]

    with pytest.raises(ConfigError, match="unique"):
        parse_config(raw)


def test_example_config_loads() -> None:
    config = load_config(Path("examples/config.yaml"))

    assert config.destinations[0].type == "filesystem"
