from pathlib import Path
from typing import Any

import certifi

from frigate_media_backup.artifact import Artifact
from frigate_media_backup.destinations.filesystem import FilesystemDestination
from frigate_media_backup.destinations.s3 import S3Destination


def test_s3_destination_uses_certifi_bundle(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    def fake_client(service_name: str, **kwargs: Any) -> object:
        captured["service_name"] = service_name
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("frigate_media_backup.destinations.s3.boto3.client", fake_client)

    S3Destination(name="b2", bucket="backup")

    assert captured["service_name"] == "s3"
    assert captured["verify"] == certifi.where()


def test_filesystem_destination_writes_bytes(tmp_path: Path) -> None:
    destination = FilesystemDestination("local", tmp_path)
    artifact = Artifact(
        artifact_id="snapshot-1",
        kind="snapshot",
        camera="doorbell",
        relative_path="doorbell/snapshots/snapshot-1.jpg",
        content_type="image/jpeg",
        data=b"jpg",
    )

    destination.upload(artifact)

    assert (tmp_path / "doorbell/snapshots/snapshot-1.jpg").read_bytes() == b"jpg"


def test_filesystem_destination_copies_file(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    destination = FilesystemDestination("local", tmp_path / "out")
    artifact = Artifact(
        artifact_id="clip-1",
        kind="clip",
        camera="garden",
        relative_path="garden/clips/clip-1.mp4",
        content_type="video/mp4",
        local_path=source,
    )

    destination.upload(artifact)

    assert (tmp_path / "out/garden/clips/clip-1.mp4").read_bytes() == b"video"
