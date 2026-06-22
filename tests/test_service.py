from pathlib import Path

from frigate_media_backup.artifact import Artifact
from frigate_media_backup.config import (
    AppConfig,
    BackfillConfig,
    BackfillOnStartConfig,
    FrigateConfig,
    MqttConfig,
    ClipUploadsConfig,
    SnapshotUploadsConfig,
    StateConfig,
    UploadsConfig,
)
from frigate_media_backup.events import ClipEvent, SnapshotEvent
from frigate_media_backup.service import BackupService
from frigate_media_backup.state import StateStore


class FakeDestination:
    def __init__(self, name: str, failures_before_success: int = 0) -> None:
        self.name = name
        self.failures_before_success = failures_before_success
        self.uploads: list[Artifact] = []
        self.attempts = 0

    def upload(self, artifact: Artifact) -> None:
        self.attempts += 1
        if self.failures_before_success > 0:
            self.failures_before_success -= 1
            raise RuntimeError(f"{self.name} is temporarily unavailable")
        self.uploads.append(artifact)


class FakeFrigate:
    def __init__(self, clip_path: Path, failures_before_success: int = 0) -> None:
        self.clip_path = clip_path
        self.failures_before_success = failures_before_success
        self.requests: list[tuple[str, str, float, float, Path]] = []

    def fetch_clip_to_temp(
        self,
        camera: str,
        event_id: str,
        start_ts: float,
        end_ts: float,
        tmp_dir: Path,
        *,
        path_start_ts: float | None = None,
    ) -> Artifact:
        self.requests.append((camera, event_id, start_ts, end_ts, tmp_dir))
        if self.failures_before_success > 0:
            self.failures_before_success -= 1
            raise RuntimeError("Frigate clip is not ready")
        return Artifact(
            artifact_id=f"clip:{event_id}:{start_ts:.6f}:{end_ts:.6f}",
            kind="clip",
            camera=camera,
            relative_path=f"{camera}/clips/{event_id}.mp4",
            content_type="video/mp4",
            local_path=self.clip_path,
        )


class MutableClock:
    def __init__(self, now: float = 0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        frigate=FrigateConfig(base_url="http://frigate:5000"),
        mqtt=MqttConfig(host="mosquitto"),
        state=StateConfig(path=tmp_path / "state.sqlite", tmp_dir=tmp_path / "tmp"),
        uploads=UploadsConfig(
            snapshots=SnapshotUploadsConfig(enabled=True),
            clips=ClipUploadsConfig(),
        ),
        backfill=BackfillConfig(on_start=BackfillOnStartConfig()),
        destinations=[],
    )


def make_filtered_config(tmp_path: Path, uploads: UploadsConfig) -> AppConfig:
    config = make_config(tmp_path)
    return AppConfig(
        frigate=config.frigate,
        mqtt=config.mqtt,
        state=config.state,
        uploads=uploads,
        backfill=config.backfill,
        destinations=config.destinations,
    )


def test_service_uploads_snapshot_to_each_destination(tmp_path: Path) -> None:
    destination = FakeDestination("local")
    service = BackupService(
        config=make_config(tmp_path),
        state=StateStore(tmp_path / "state.sqlite"),
        frigate=FakeFrigate(tmp_path / "clip.mp4"),  # type: ignore[arg-type]
        destinations=[destination],
    )

    service.handle_event(SnapshotEvent("snap-1", "doorbell", "person", b"jpg"))

    assert len(destination.uploads) == 1
    assert destination.uploads[0].relative_path == "doorbell/snapshots/snap-1.jpg"


def test_service_filters_snapshots_by_camera_and_object(tmp_path: Path) -> None:
    destination = FakeDestination("local")
    service = BackupService(
        config=make_filtered_config(
            tmp_path,
            UploadsConfig(
                snapshots=SnapshotUploadsConfig(
                    enabled=True,
                    cameras=("front",),
                    objects=("person",),
                ),
                clips=ClipUploadsConfig(),
            ),
        ),
        state=StateStore(tmp_path / "state.sqlite"),
        frigate=FakeFrigate(tmp_path / "clip.mp4"),  # type: ignore[arg-type]
        destinations=[destination],
    )

    service.handle_event(SnapshotEvent("snap-1", "doorbell", "person", b"jpg"))
    service.handle_event(SnapshotEvent("snap-2", "front", "car", b"jpg"))
    service.handle_event(SnapshotEvent("snap-3", "front", "person", b"jpg"))

    assert [artifact.artifact_id for artifact in destination.uploads] == ["snapshot:snap-3"]


def test_service_throttles_snapshots_by_camera_and_object(tmp_path: Path) -> None:
    destination = FakeDestination("local")
    service = BackupService(
        config=make_filtered_config(
            tmp_path,
            UploadsConfig(
                snapshots=SnapshotUploadsConfig(enabled=True, min_interval_seconds=60),
                clips=ClipUploadsConfig(),
            ),
        ),
        state=StateStore(tmp_path / "state.sqlite"),
        frigate=FakeFrigate(tmp_path / "clip.mp4"),  # type: ignore[arg-type]
        destinations=[destination],
    )

    service.handle_event(SnapshotEvent("snap-1", "front", "person", b"jpg"))
    service.handle_event(SnapshotEvent("snap-2", "front", "person", b"jpg"))
    service.handle_event(SnapshotEvent("snap-3", "front", "car", b"jpg"))

    assert [artifact.artifact_id for artifact in destination.uploads] == [
        "snapshot:snap-1",
        "snapshot:snap-3",
    ]


def test_service_filters_clips_by_camera(tmp_path: Path) -> None:
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    destination = FakeDestination("local")
    frigate = FakeFrigate(clip_path)
    service = BackupService(
        config=make_filtered_config(
            tmp_path,
            UploadsConfig(
                snapshots=SnapshotUploadsConfig(enabled=True),
                clips=ClipUploadsConfig(enabled=True, cameras=("garden",)),
            ),
        ),
        state=StateStore(tmp_path / "state.sqlite"),
        frigate=frigate,  # type: ignore[arg-type]
        destinations=[destination],
    )

    service.handle_event(ClipEvent("review-1", "front", 100.0, 120.0))

    assert destination.uploads == []
    assert frigate.requests == []


def test_service_skips_already_uploaded_artifact(tmp_path: Path) -> None:
    destination = FakeDestination("local")
    state = StateStore(tmp_path / "state.sqlite")
    state.mark_uploaded("snapshot:snap-1", "local", "doorbell/snapshots/snap-1.jpg")
    service = BackupService(
        config=make_config(tmp_path),
        state=state,
        frigate=FakeFrigate(tmp_path / "clip.mp4"),  # type: ignore[arg-type]
        destinations=[destination],
    )

    service.handle_event(SnapshotEvent("snap-1", "doorbell", "person", b"jpg"))

    assert destination.uploads == []


def test_service_fetches_clip_with_padding_and_cleans_up(tmp_path: Path) -> None:
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    destination = FakeDestination("local")
    frigate = FakeFrigate(clip_path)
    service = BackupService(
        config=make_config(tmp_path),
        state=StateStore(tmp_path / "state.sqlite"),
        frigate=frigate,  # type: ignore[arg-type]
        destinations=[destination],
    )

    service.handle_event(ClipEvent("review-1", "garden", 100.0, 120.0))

    assert frigate.requests == [("garden", "review-1", 95.0, 125.0, tmp_path / "tmp")]
    assert len(destination.uploads) == 1
    assert not clip_path.exists()


def test_service_skips_clip_fetch_when_all_destinations_uploaded(tmp_path: Path) -> None:
    destination = FakeDestination("local")
    state = StateStore(tmp_path / "state.sqlite")
    state.mark_uploaded(
        "clip:review-1:95.000000:125.000000",
        "local",
        "garden/clips/review-1-95.000000-125.000000.mp4",
    )
    frigate = FakeFrigate(tmp_path / "clip.mp4")
    service = BackupService(
        config=make_config(tmp_path),
        state=state,
        frigate=frigate,  # type: ignore[arg-type]
        destinations=[destination],
    )

    uploaded = service.upload_clip_event(ClipEvent("review-1", "garden", 100.0, 120.0))

    assert uploaded is False
    assert frigate.requests == []
    assert destination.uploads == []


def test_service_upload_clip_can_bypass_filters_and_padding(tmp_path: Path) -> None:
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    destination = FakeDestination("local")
    frigate = FakeFrigate(clip_path)
    service = BackupService(
        config=make_filtered_config(
            tmp_path,
            UploadsConfig(
                snapshots=SnapshotUploadsConfig(enabled=False),
                clips=ClipUploadsConfig(enabled=True, cameras=("front",)),
            ),
        ),
        state=StateStore(tmp_path / "state.sqlite"),
        frigate=frigate,  # type: ignore[arg-type]
        destinations=[destination],
    )

    uploaded = service.upload_clip_event(
        ClipEvent("manual-1", "garden", 100.0, 120.0),
        apply_filters=False,
        apply_padding=False,
    )

    assert uploaded is True
    assert frigate.requests == [("garden", "manual-1", 100.0, 120.0, tmp_path / "tmp")]
    assert len(destination.uploads) == 1


def test_service_retries_clip_fetch_before_upload(tmp_path: Path) -> None:
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    destination = FakeDestination("local")
    frigate = FakeFrigate(clip_path, failures_before_success=2)
    service = BackupService(
        config=make_config(tmp_path),
        state=StateStore(tmp_path / "state.sqlite"),
        frigate=frigate,  # type: ignore[arg-type]
        destinations=[destination],
        fetch_retry_delays_seconds=(0, 0, 0),
    )

    uploaded = service.upload_clip_event(ClipEvent("review-1", "garden", 100.0, 120.0))

    assert uploaded is True
    assert len(frigate.requests) == 3
    assert len(destination.uploads) == 1


def test_service_records_clip_fetch_failure_after_retries(tmp_path: Path) -> None:
    clock = MutableClock()
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    state = StateStore(tmp_path / "state.sqlite")
    frigate = FakeFrigate(clip_path, failures_before_success=3)
    service = BackupService(
        config=make_config(tmp_path),
        state=state,
        frigate=frigate,  # type: ignore[arg-type]
        destinations=[FakeDestination("local")],
        fetch_retry_delays_seconds=(0, 0),
        clock=clock,
    )

    try:
        service.upload_clip_event(ClipEvent("review-1", "garden", 100.0, 120.0))
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected clip fetch to fail")

    with state.connect() as connection:
        row = connection.execute(
            "SELECT artifact_id, destination, error FROM failures"
        ).fetchone()
    assert row == (
        "clip:review-1:95.000000:125.000000",
        "frigate",
        "Frigate clip is not ready",
    )
    assert len(state.due_pending_clip_fetches(299)) == 0
    assert len(state.due_pending_clip_fetches(300)) == 1


def test_service_retries_destination_upload(tmp_path: Path) -> None:
    destination = FakeDestination("local", failures_before_success=2)
    state = StateStore(tmp_path / "state.sqlite")
    service = BackupService(
        config=make_config(tmp_path),
        state=state,
        frigate=FakeFrigate(tmp_path / "clip.mp4"),  # type: ignore[arg-type]
        destinations=[destination],
        upload_retry_delays_seconds=(0, 0, 0),
    )

    uploaded = service.upload_artifact(
        Artifact(
            artifact_id="snapshot:snap-1",
            kind="snapshot",
            camera="front",
            relative_path="front/snapshots/snap-1.jpg",
            content_type="image/jpeg",
            data=b"jpg",
        )
    )

    assert uploaded is True
    assert destination.attempts == 3
    assert state.is_uploaded("snapshot:snap-1", "local")


def test_service_records_failed_destination_and_continues(tmp_path: Path) -> None:
    clock = MutableClock()
    failed = FakeDestination("b2", failures_before_success=3)
    successful = FakeDestination("local")
    state = StateStore(tmp_path / "state.sqlite")
    service = BackupService(
        config=make_config(tmp_path),
        state=state,
        frigate=FakeFrigate(tmp_path / "clip.mp4"),  # type: ignore[arg-type]
        destinations=[failed, successful],
        upload_retry_delays_seconds=(0, 0),
        clock=clock,
    )

    uploaded = service.upload_artifact(
        Artifact(
            artifact_id="snapshot:snap-1",
            kind="snapshot",
            camera="front",
            relative_path="front/snapshots/snap-1.jpg",
            content_type="image/jpeg",
            data=b"jpg",
        )
    )

    assert uploaded is True
    assert failed.attempts == 2
    assert successful.attempts == 1
    assert not state.is_uploaded("snapshot:snap-1", "b2")
    assert state.is_uploaded("snapshot:snap-1", "local")
    with state.connect() as connection:
        row = connection.execute(
            "SELECT artifact_id, destination, error FROM failures"
        ).fetchone()
    assert row == ("snapshot:snap-1", "b2", "b2 is temporarily unavailable")
    pending = state.due_pending_uploads(300)
    assert len(pending) == 1
    assert pending[0].destination == "b2"
    assert pending[0].local_path.exists()


def test_service_retries_due_destination_upload_from_cache(tmp_path: Path) -> None:
    clock = MutableClock()
    failed_then_successful = FakeDestination("b2", failures_before_success=2)
    local = FakeDestination("local")
    state = StateStore(tmp_path / "state.sqlite")
    service = BackupService(
        config=make_config(tmp_path),
        state=state,
        frigate=FakeFrigate(tmp_path / "clip.mp4"),  # type: ignore[arg-type]
        destinations=[failed_then_successful, local],
        upload_retry_delays_seconds=(0, 0),
        deferred_retry_delays_seconds=(300,),
        clock=clock,
    )

    uploaded = service.upload_artifact(
        Artifact(
            artifact_id="snapshot:snap-1",
            kind="snapshot",
            camera="front",
            relative_path="front/snapshots/snap-1.jpg",
            content_type="image/jpeg",
            data=b"jpg",
        )
    )

    assert uploaded is True
    assert state.is_uploaded("snapshot:snap-1", "local")
    assert not state.is_uploaded("snapshot:snap-1", "b2")
    pending = state.due_pending_uploads(300)
    assert len(pending) == 1
    cache_path = pending[0].local_path
    assert cache_path.exists()

    clock.now = 300
    processed = service.process_due_retries()

    assert processed == 1
    assert state.is_uploaded("snapshot:snap-1", "b2")
    assert state.due_pending_uploads(300) == []
    assert not cache_path.exists()


def test_service_retries_due_clip_fetch(tmp_path: Path) -> None:
    clock = MutableClock()
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    destination = FakeDestination("local")
    state = StateStore(tmp_path / "state.sqlite")
    frigate = FakeFrigate(clip_path, failures_before_success=1)
    service = BackupService(
        config=make_config(tmp_path),
        state=state,
        frigate=frigate,  # type: ignore[arg-type]
        destinations=[destination],
        fetch_retry_delays_seconds=(0,),
        deferred_retry_delays_seconds=(300,),
        clock=clock,
    )

    try:
        service.upload_clip_event(ClipEvent("review-1", "garden", 100.0, 120.0))
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected initial clip fetch to fail")

    assert len(state.due_pending_clip_fetches(300)) == 1

    clip_path.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    clock.now = 300
    processed = service.process_due_retries()

    assert processed == 1
    assert state.due_pending_clip_fetches(300) == []
    assert state.is_uploaded("clip:review-1:95.000000:125.000000", "local")
    assert len(destination.uploads) == 1


def test_due_clip_fetch_retry_uses_persisted_window_after_padding_change(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    destination = FakeDestination("local")
    state = StateStore(tmp_path / "state.sqlite")
    frigate = FakeFrigate(clip_path, failures_before_success=1)
    service = BackupService(
        config=make_config(tmp_path),
        state=state,
        frigate=frigate,  # type: ignore[arg-type]
        destinations=[destination],
        fetch_retry_delays_seconds=(0,),
        deferred_retry_delays_seconds=(300,),
        clock=clock,
    )

    try:
        service.upload_clip_event(ClipEvent("review-1", "garden", 100.0, 120.0))
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected initial clip fetch to fail")

    changed_padding_service = BackupService(
        config=make_filtered_config(
            tmp_path,
            UploadsConfig(
                snapshots=SnapshotUploadsConfig(enabled=True),
                clips=ClipUploadsConfig(
                    padding_before_seconds=0,
                    padding_after_seconds=0,
                ),
            ),
        ),
        state=state,
        frigate=frigate,  # type: ignore[arg-type]
        destinations=[destination],
        fetch_retry_delays_seconds=(0,),
        deferred_retry_delays_seconds=(300,),
        clock=clock,
    )

    clip_path.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    clock.now = 300
    processed = changed_padding_service.process_due_retries()

    assert processed == 1
    assert frigate.requests[-1] == ("garden", "review-1", 95.0, 125.0, tmp_path / "tmp")
    assert state.due_pending_clip_fetches(300) == []
    assert state.is_uploaded("clip:review-1:95.000000:125.000000", "local")
    assert not state.is_uploaded("clip:review-1:100.000000:120.000000", "local")


def test_due_clip_fetch_remains_pending_if_upload_state_is_not_durable(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    state = StateStore(tmp_path / "state.sqlite")
    frigate = FakeFrigate(clip_path)
    service = BackupService(
        config=make_config(tmp_path),
        state=state,
        frigate=frigate,  # type: ignore[arg-type]
        destinations=[FakeDestination("local")],
        fetch_retry_delays_seconds=(0,),
        deferred_retry_delays_seconds=(300,),
        clock=clock,
    )
    state.upsert_pending_clip_fetch(
        artifact_id="clip:review-1:95.000000:125.000000",
        event_id="review-1",
        camera="garden",
        event_start_time=100,
        fetch_start_time=95,
        fetch_end_time=125,
        apply_filters=True,
        apply_padding=True,
        attempt_count=1,
        next_attempt_at=0,
        error="offline",
        now=0,
    )

    def fail_upload(_artifact: Artifact) -> bool:
        raise RuntimeError("process died before upload state was durable")

    service.upload_artifact = fail_upload  # type: ignore[method-assign]

    processed = service.process_due_retries()

    assert processed == 1
    pending = state.due_pending_clip_fetches(300)
    assert len(pending) == 1
    assert pending[0].artifact_id == "clip:review-1:95.000000:125.000000"
