from __future__ import annotations

import logging
from pathlib import Path
import time

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
        self.last_snapshot_upload_by_topic: dict[tuple[str, str], float] = {}

    def handle_event(self, event: BackupEvent) -> None:
        if isinstance(event, SnapshotEvent):
            if not self.config.uploads.snapshots.allows(event.camera, event.object_label):
                return
            if self.snapshot_throttled(event):
                return
            self.upload_artifact(event.to_artifact())
            return
        if isinstance(event, ClipEvent):
            if not self.config.uploads.clips.allows(event.camera):
                return
            artifact = self.fetch_clip(event)
            try:
                self.upload_artifact(artifact)
            finally:
                cleanup_temp_file(artifact.local_path)

    def fetch_clip(self, event: ClipEvent) -> Artifact:
        start = max(0, event.start_time - self.config.uploads.clips.padding_before_seconds)
        end = event.end_time + self.config.uploads.clips.padding_after_seconds
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

    def snapshot_throttled(self, event: SnapshotEvent) -> bool:
        min_interval = self.config.uploads.snapshots.min_interval_seconds
        if min_interval <= 0:
            return False
        key = (event.camera, event.object_label)
        now = time.monotonic()
        last_upload = self.last_snapshot_upload_by_topic.get(key)
        if last_upload is not None and now - last_upload < min_interval:
            return True
        self.last_snapshot_upload_by_topic[key] = now
        return False


def cleanup_temp_file(path: Path | None) -> None:
    if path and path.exists():
        path.unlink()
