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
    def __init__(self, name: str) -> None:
        self.name = name
        self.uploads: list[Artifact] = []

    def upload(self, artifact: Artifact) -> None:
        self.uploads.append(artifact)


class FakeFrigate:
    def __init__(self, clip_path: Path) -> None:
        self.clip_path = clip_path
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
        return Artifact(
            artifact_id=f"clip:{event_id}:{start_ts:.6f}:{end_ts:.6f}",
            kind="clip",
            camera=camera,
            relative_path=f"{camera}/clips/{event_id}.mp4",
            content_type="video/mp4",
            local_path=self.clip_path,
        )


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
