from pathlib import Path

from frigate_media_backup.artifact import Artifact
from frigate_media_backup.state import StateStore


def test_upload_state_round_trip(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")

    assert not store.is_uploaded("artifact-1", "b2")

    store.mark_uploaded("artifact-1", "b2", "camera/clips/file.mp4")

    assert store.is_uploaded("artifact-1", "b2")
    assert not store.is_uploaded("artifact-1", "sftp")


def test_failures_are_recorded(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")

    store.record_failure("artifact-1", "b2", "boom")

    with store.connect() as connection:
        row = connection.execute("SELECT error FROM failures").fetchone()
    assert row == ("boom",)


def test_pending_upload_round_trip(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    cache_file = tmp_path / "cache" / "front" / "clip.mp4"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_bytes(b"mp4")

    store.upsert_pending_upload(
        artifact_id="clip:1",
        destination="b2",
        kind="clip",
        camera="front",
        relative_path="front/clips/1.mp4",
        content_type="video/mp4",
        local_path=cache_file,
        attempt_count=1,
        next_attempt_at=100,
        error="offline",
        now=10,
    )

    assert store.due_pending_uploads(99) == []
    pending = store.due_pending_uploads(100)

    assert len(pending) == 1
    assert pending[0].to_artifact() == Artifact(
        artifact_id="clip:1",
        kind="clip",
        camera="front",
        relative_path="front/clips/1.mp4",
        content_type="video/mp4",
        local_path=cache_file,
    )


def test_mark_uploaded_clears_pending_upload(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    cache_file = tmp_path / "cache" / "front" / "clip.mp4"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_bytes(b"mp4")
    store.upsert_pending_upload(
        artifact_id="clip:1",
        destination="b2",
        kind="clip",
        camera="front",
        relative_path="front/clips/1.mp4",
        content_type="video/mp4",
        local_path=cache_file,
        attempt_count=1,
        next_attempt_at=100,
        error="offline",
        now=10,
    )

    store.mark_uploaded("clip:1", "b2", "front/clips/1.mp4")

    assert store.due_pending_uploads(100) == []


def test_pending_clip_fetch_round_trip(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")

    store.upsert_pending_clip_fetch(
        artifact_id="clip:1:95.000000:125.000000",
        event_id="1",
        camera="front",
        event_start_time=100,
        fetch_start_time=95,
        fetch_end_time=125,
        apply_filters=True,
        apply_padding=True,
        attempt_count=1,
        next_attempt_at=100,
        error="offline",
        now=10,
    )

    assert store.due_pending_clip_fetches(99) == []
    pending = store.due_pending_clip_fetches(100)

    assert len(pending) == 1
    assert pending[0].event_id == "1"
    assert pending[0].event_start_time == 100
    assert pending[0].fetch_start_time == 95
    assert pending[0].fetch_end_time == 125
    assert pending[0].apply_filters is True
    assert pending[0].apply_padding is True
