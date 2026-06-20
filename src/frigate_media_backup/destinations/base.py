from __future__ import annotations

from typing import Protocol

from frigate_media_backup.artifact import Artifact


class Destination(Protocol):
    name: str

    def upload(self, artifact: Artifact) -> None:
        """Upload an artifact or raise an exception."""

