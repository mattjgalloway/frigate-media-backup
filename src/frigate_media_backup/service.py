from __future__ import annotations

import hashlib
import logging
from pathlib import Path
import shutil
import time
from typing import Callable, TypeVar

from .artifact import Artifact
from .config import AppConfig
from .destinations.base import Destination
from .events import BackupEvent, ClipEvent, SnapshotEvent
from .frigate import FrigateClient
from .state import StateStore

LOGGER = logging.getLogger(__name__)
FETCH_FAILURE_DESTINATION = "frigate"
DEFAULT_FETCH_RETRY_DELAYS_SECONDS = (0.0, 5.0, 15.0, 45.0, 90.0)
DEFAULT_UPLOAD_RETRY_DELAYS_SECONDS = (0.0, 5.0, 20.0, 60.0)
DEFAULT_DEFERRED_RETRY_DELAYS_SECONDS = (300.0, 900.0, 3600.0, 10800.0, 43200.0, 86400.0)
T = TypeVar("T")


class BackupService:
    def __init__(
        self,
        *,
        config: AppConfig,
        state: StateStore,
        frigate: FrigateClient,
        destinations: list[Destination],
        fetch_retry_delays_seconds: tuple[float, ...] = DEFAULT_FETCH_RETRY_DELAYS_SECONDS,
        upload_retry_delays_seconds: tuple[float, ...] = DEFAULT_UPLOAD_RETRY_DELAYS_SECONDS,
        deferred_retry_delays_seconds: tuple[float, ...] = DEFAULT_DEFERRED_RETRY_DELAYS_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.state = state
        self.frigate = frigate
        self.destinations = destinations
        self.fetch_retry_delays_seconds = fetch_retry_delays_seconds
        self.upload_retry_delays_seconds = upload_retry_delays_seconds
        self.deferred_retry_delays_seconds = deferred_retry_delays_seconds
        self.sleep = sleep
        self.clock = clock
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
            self.upload_clip_event(event)

    def upload_clip_event(
        self,
        event: ClipEvent,
        *,
        apply_filters: bool = True,
        apply_padding: bool = True,
    ) -> bool:
        if apply_filters and not self.config.uploads.clips.allows(event.camera):
            return False
        artifact_id = self.clip_artifact_id(event, apply_padding=apply_padding)
        if self.all_destinations_uploaded(artifact_id):
            self.state.delete_pending_clip_fetch(artifact_id)
            LOGGER.info("Skipping already uploaded clip", extra={"artifact_id": artifact_id})
            return False
        artifact = self.fetch_clip_with_retry(
            event,
            artifact_id=artifact_id,
            apply_filters=apply_filters,
            apply_padding=apply_padding,
        )
        cached_artifact = self.cache_artifact(artifact)
        try:
            uploaded = self.upload_artifact(cached_artifact)
            self.state.delete_pending_clip_fetch(artifact_id)
            return uploaded
        finally:
            if artifact.local_path != cached_artifact.local_path:
                cleanup_temp_file(artifact.local_path)

    def fetch_clip(self, event: ClipEvent, *, apply_padding: bool = True) -> Artifact:
        start, end = self.clip_window(event, apply_padding=apply_padding)
        return self.frigate.fetch_clip_to_temp(
            event.camera,
            event.event_id,
            start,
            end,
            self.config.state.tmp_dir,
            path_start_ts=event.start_time,
        )

    def fetch_clip_with_retry(
        self,
        event: ClipEvent,
        *,
        artifact_id: str,
        apply_filters: bool = True,
        apply_padding: bool = True,
    ) -> Artifact:
        try:
            return retry_with_backoff(
                lambda: self.fetch_clip(event, apply_padding=apply_padding),
                delays_seconds=self.fetch_retry_delays_seconds,
                sleep=self.sleep,
                description=f"fetch clip {artifact_id} from Frigate",
                extra={"artifact_id": artifact_id, "destination": FETCH_FAILURE_DESTINATION},
            )
        except Exception as exc:
            self.state.record_failure(artifact_id, FETCH_FAILURE_DESTINATION, str(exc))
            self.schedule_clip_fetch_retry(
                event,
                artifact_id=artifact_id,
                apply_filters=apply_filters,
                apply_padding=apply_padding,
                error=str(exc),
            )
            LOGGER.exception(
                "Clip fetch failed after retries",
                extra={"artifact_id": artifact_id, "destination": FETCH_FAILURE_DESTINATION},
            )
            raise

    def clip_artifact_id(self, event: ClipEvent, *, apply_padding: bool = True) -> str:
        start, end = self.clip_window(event, apply_padding=apply_padding)
        return f"clip:{event.event_id}:{start:.6f}:{end:.6f}"

    def clip_window(self, event: ClipEvent, *, apply_padding: bool = True) -> tuple[float, float]:
        if not apply_padding:
            return event.start_time, event.end_time
        start = max(0, event.start_time - self.config.uploads.clips.padding_before_seconds)
        end = event.end_time + self.config.uploads.clips.padding_after_seconds
        return start, end

    def all_destinations_uploaded(self, artifact_id: str) -> bool:
        return all(
            self.state.is_uploaded(artifact_id, destination.name)
            for destination in self.destinations
        )

    def upload_artifact(self, artifact: Artifact) -> bool:
        if self.all_destinations_uploaded(artifact.artifact_id):
            LOGGER.info("Skipping already uploaded artifact", extra={"artifact_id": artifact.artifact_id})
            return False
        artifact = self.cache_artifact(artifact)
        uploaded_any = False
        for destination in self.destinations:
            if self.state.is_uploaded(artifact.artifact_id, destination.name):
                LOGGER.info(
                    "Skipping already uploaded artifact",
                    extra={"artifact_id": artifact.artifact_id, "destination": destination.name},
                )
                continue
            try:
                retry_with_backoff(
                    lambda: destination.upload(artifact),
                    delays_seconds=self.upload_retry_delays_seconds,
                    sleep=self.sleep,
                    description=f"upload {artifact.artifact_id} to {destination.name}",
                    extra={"artifact_id": artifact.artifact_id, "destination": destination.name},
                )
            except Exception as exc:
                self.state.record_failure(artifact.artifact_id, destination.name, str(exc))
                self.schedule_upload_retry(artifact, destination.name, str(exc))
                LOGGER.exception(
                    "Upload failed after retries",
                    extra={"artifact_id": artifact.artifact_id, "destination": destination.name},
                )
                continue
            self.state.mark_uploaded(
                artifact.artifact_id,
                destination.name,
                artifact.relative_path,
            )
            uploaded_any = True
            LOGGER.info(
                "Upload complete",
                extra={"artifact_id": artifact.artifact_id, "destination": destination.name},
            )
        self.cleanup_cached_artifact_if_complete(artifact)
        return uploaded_any

    def process_due_retries(self, *, limit: int = 50) -> int:
        processed = 0
        processed += self.retry_due_clip_fetches(limit=limit)
        processed += self.retry_due_uploads(limit=limit)
        return processed

    def retry_due_clip_fetches(self, *, limit: int = 50) -> int:
        processed = 0
        for pending in self.state.due_pending_clip_fetches(self.clock(), limit=limit):
            if pending.apply_filters and not self.config.uploads.clips.allows(pending.camera):
                self.state.delete_pending_clip_fetch(pending.artifact_id)
                continue
            processed += 1
            artifact: Artifact | None = None
            try:
                artifact = retry_with_backoff(
                    lambda: self.frigate.fetch_clip_to_temp(
                        pending.camera,
                        pending.event_id,
                        pending.fetch_start_time,
                        pending.fetch_end_time,
                        self.config.state.tmp_dir,
                        path_start_ts=pending.event_start_time,
                    ),
                    delays_seconds=self.fetch_retry_delays_seconds,
                    sleep=self.sleep,
                    description=f"retry fetch clip {pending.artifact_id} from Frigate",
                    extra={
                        "artifact_id": pending.artifact_id,
                        "destination": FETCH_FAILURE_DESTINATION,
                    },
                )
                cached_artifact = self.cache_artifact(artifact)
                self.upload_artifact(cached_artifact)
                self.state.delete_pending_clip_fetch(pending.artifact_id)
            except Exception:
                self.state.record_failure(
                    pending.artifact_id,
                    FETCH_FAILURE_DESTINATION,
                    "Deferred Frigate clip fetch retry failed",
                )
                self.schedule_clip_fetch_window_retry(
                    event_id=pending.event_id,
                    camera=pending.camera,
                    event_start_time=pending.event_start_time,
                    fetch_start_time=pending.fetch_start_time,
                    fetch_end_time=pending.fetch_end_time,
                    artifact_id=pending.artifact_id,
                    apply_filters=pending.apply_filters,
                    apply_padding=pending.apply_padding,
                    error="Deferred Frigate clip fetch retry failed",
                )
                LOGGER.exception(
                    "Deferred Frigate clip fetch retry failed",
                    extra={
                        "artifact_id": pending.artifact_id,
                        "destination": FETCH_FAILURE_DESTINATION,
                    },
                )
            finally:
                if artifact is not None:
                    cleanup_temp_file(artifact.local_path)
        return processed

    def retry_due_uploads(self, *, limit: int = 50) -> int:
        destinations_by_name = {destination.name: destination for destination in self.destinations}
        processed = 0
        for pending in self.state.due_pending_uploads(self.clock(), limit=limit):
            processed += 1
            if self.state.is_uploaded(pending.artifact_id, pending.destination):
                self.state.delete_pending_upload(pending.artifact_id, pending.destination)
                continue
            destination = destinations_by_name.get(pending.destination)
            if destination is None:
                self.schedule_upload_retry(
                    pending.to_artifact(),
                    pending.destination,
                    f"Destination {pending.destination!r} is not configured",
                )
                continue
            artifact = pending.to_artifact()
            if not artifact.require_file().exists():
                self.schedule_upload_retry(
                    artifact,
                    pending.destination,
                    f"Cached artifact is missing: {artifact.local_path}",
                )
                continue
            try:
                retry_with_backoff(
                    lambda: destination.upload(artifact),
                    delays_seconds=self.upload_retry_delays_seconds,
                    sleep=self.sleep,
                    description=f"retry upload {artifact.artifact_id} to {destination.name}",
                    extra={"artifact_id": artifact.artifact_id, "destination": destination.name},
                )
            except Exception as exc:
                self.state.record_failure(artifact.artifact_id, destination.name, str(exc))
                self.schedule_upload_retry(artifact, destination.name, str(exc))
                LOGGER.exception(
                    "Deferred upload retry failed",
                    extra={"artifact_id": artifact.artifact_id, "destination": destination.name},
                )
                continue
            self.state.mark_uploaded(artifact.artifact_id, destination.name, artifact.relative_path)
            self.cleanup_cached_artifact_if_complete(artifact)
            LOGGER.info(
                "Deferred upload retry complete",
                extra={"artifact_id": artifact.artifact_id, "destination": destination.name},
            )
        return processed

    def schedule_upload_retry(self, artifact: Artifact, destination: str, error: str) -> None:
        now = self.clock()
        attempt_count = self.state.pending_upload_attempt_count(
            artifact.artifact_id,
            destination,
        ) + 1
        self.state.upsert_pending_upload(
            artifact_id=artifact.artifact_id,
            destination=destination,
            kind=artifact.kind,
            camera=artifact.camera,
            relative_path=artifact.relative_path,
            content_type=artifact.content_type,
            local_path=artifact.require_file(),
            attempt_count=attempt_count,
            next_attempt_at=now + self.deferred_retry_delay(attempt_count),
            error=error,
            now=now,
        )

    def schedule_clip_fetch_retry(
        self,
        event: ClipEvent,
        *,
        artifact_id: str,
        apply_filters: bool,
        apply_padding: bool,
        error: str,
    ) -> None:
        now = self.clock()
        attempt_count = self.state.pending_clip_fetch_attempt_count(artifact_id) + 1
        fetch_start_time, fetch_end_time = self.clip_window(event, apply_padding=apply_padding)
        self.schedule_clip_fetch_window_retry(
            event_id=event.event_id,
            camera=event.camera,
            event_start_time=event.start_time,
            fetch_start_time=fetch_start_time,
            fetch_end_time=fetch_end_time,
            artifact_id=artifact_id,
            apply_filters=apply_filters,
            apply_padding=apply_padding,
            error=error,
            now=now,
            attempt_count=attempt_count,
        )

    def schedule_clip_fetch_window_retry(
        self,
        *,
        event_id: str,
        camera: str,
        event_start_time: float,
        fetch_start_time: float,
        fetch_end_time: float,
        artifact_id: str,
        apply_filters: bool,
        apply_padding: bool,
        error: str,
        now: float | None = None,
        attempt_count: int | None = None,
    ) -> None:
        now = self.clock() if now is None else now
        if attempt_count is None:
            attempt_count = self.state.pending_clip_fetch_attempt_count(artifact_id) + 1
        self.state.upsert_pending_clip_fetch(
            artifact_id=artifact_id,
            event_id=event_id,
            camera=camera,
            event_start_time=event_start_time,
            fetch_start_time=fetch_start_time,
            fetch_end_time=fetch_end_time,
            apply_filters=apply_filters,
            apply_padding=apply_padding,
            attempt_count=attempt_count,
            next_attempt_at=now + self.deferred_retry_delay(attempt_count),
            error=error,
            now=now,
        )

    def deferred_retry_delay(self, attempt_count: int) -> float:
        if not self.deferred_retry_delays_seconds:
            return 0.0
        index = min(max(attempt_count, 1) - 1, len(self.deferred_retry_delays_seconds) - 1)
        return self.deferred_retry_delays_seconds[index]

    def cache_artifact(self, artifact: Artifact) -> Artifact:
        cache_path = self.cache_path_for(artifact)
        if artifact.local_path == cache_path:
            return artifact
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        if artifact.local_path is not None:
            shutil.copy2(artifact.local_path, cache_path)
        else:
            cache_path.write_bytes(artifact.require_bytes())
        return Artifact(
            artifact_id=artifact.artifact_id,
            kind=artifact.kind,
            camera=artifact.camera,
            relative_path=artifact.relative_path,
            content_type=artifact.content_type,
            source_url=artifact.source_url,
            local_path=cache_path,
            data=None,
        )

    def cache_path_for(self, artifact: Artifact) -> Path:
        suffix = Path(artifact.relative_path).suffix or ".bin"
        digest = hashlib.sha256(artifact.artifact_id.encode("utf-8")).hexdigest()
        return self.state.cache_dir / f"{digest}{suffix}"

    def cleanup_cached_artifact_if_complete(self, artifact: Artifact) -> None:
        if not self.all_destinations_uploaded(artifact.artifact_id):
            return
        cleanup_temp_file(artifact.local_path)

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


def retry_with_backoff(
    operation: Callable[[], T],
    *,
    delays_seconds: tuple[float, ...],
    sleep: Callable[[float], None],
    description: str,
    extra: dict[str, str],
) -> T:
    if not delays_seconds:
        delays_seconds = (0.0,)
    last_error: Exception | None = None
    for attempt, delay_seconds in enumerate(delays_seconds, start=1):
        if delay_seconds > 0:
            sleep(delay_seconds)
        try:
            return operation()
        except Exception as exc:
            last_error = exc
            if attempt == len(delays_seconds):
                break
            LOGGER.warning(
                "%s failed on attempt %s/%s; retrying",
                description,
                attempt,
                len(delays_seconds),
                extra=extra,
                exc_info=True,
            )
    if last_error is None:
        raise RuntimeError(f"{description} was not attempted")
    raise last_error
