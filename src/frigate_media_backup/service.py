from __future__ import annotations

import logging
from pathlib import Path

from .artifact import Artifact
from .config import AppConfig
from .destinations.base import Destination
from .events import BackupEvent, ClipEvent, SnapshotEvent
from .frigate import FrigateClient
from .state import StateStore

LOGGER = logging.getLogger(__name__)


class BackupService:
    def __init__(
        self,
        *,
        config: AppConfig,
        state: StateStore,
        frigate: FrigateClient,
        destinations: list[Destination],
    ) -> None:
        self.config = config
        self.state = state
        self.frigate = frigate
        self.destinations = destinations

    def handle_event(self, event: BackupEvent) -> None:
        if isinstance(event, SnapshotEvent):
            if not self.config.uploads.include_snapshots:
                return
            self.upload_artifact(event.to_artifact())
            return
        if isinstance(event, ClipEvent):
            if not self.config.uploads.include_clips:
                return
            artifact = self.fetch_clip(event)
            try:
                self.upload_artifact(artifact)
            finally:
                cleanup_temp_file(artifact.local_path)

    def fetch_clip(self, event: ClipEvent) -> Artifact:
        start = max(0, event.start_time - self.config.uploads.clip_padding_before_seconds)
        end = event.end_time + self.config.uploads.clip_padding_after_seconds
        return self.frigate.fetch_clip_to_temp(
            event.camera,
            event.event_id,
            start,
            end,
            self.config.state.tmp_dir,
        )

    def upload_artifact(self, artifact: Artifact) -> None:
        for destination in self.destinations:
            if self.state.is_uploaded(artifact.artifact_id, destination.name):
                LOGGER.info(
                    "Skipping already uploaded artifact",
                    extra={"artifact_id": artifact.artifact_id, "destination": destination.name},
                )
                continue
            try:
                destination.upload(artifact)
            except Exception as exc:
                self.state.record_failure(artifact.artifact_id, destination.name, str(exc))
                LOGGER.exception(
                    "Upload failed",
                    extra={"artifact_id": artifact.artifact_id, "destination": destination.name},
                )
                raise
            self.state.mark_uploaded(
                artifact.artifact_id,
                destination.name,
                artifact.relative_path,
            )
            LOGGER.info(
                "Upload complete",
                extra={"artifact_id": artifact.artifact_id, "destination": destination.name},
            )


def cleanup_temp_file(path: Path | None) -> None:
    if path and path.exists():
        path.unlink()

