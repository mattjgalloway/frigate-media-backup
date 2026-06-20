from __future__ import annotations

from frigate_media_backup.config import ConfigError, DestinationConfig

from .base import Destination
from .filesystem import FilesystemDestination
from .s3 import S3Destination
from .sftp import SftpDestination


def build_destinations(configs: list[DestinationConfig]) -> list[Destination]:
    return [build_destination(config) for config in configs]


def build_destination(config: DestinationConfig) -> Destination:
    options = dict(config.options)
    destination_type = config.type.lower()
    if destination_type == "filesystem":
        return FilesystemDestination(name=config.name, path=required(options, "path"))
    if destination_type == "s3":
        return S3Destination(name=config.name, **options)
    if destination_type == "sftp":
        return SftpDestination(name=config.name, **options)
    raise ConfigError(f"Unknown destination type: {config.type}")


def required(options: dict[str, object], key: str) -> object:
    value = options.get(key)
    if value is None:
        raise ConfigError(f"Destination option {key} is required")
    return value

