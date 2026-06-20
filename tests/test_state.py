from pathlib import Path

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

