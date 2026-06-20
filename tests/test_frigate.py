from pathlib import Path

import httpx
import pytest

from frigate_media_backup.config import FrigateConfig
from frigate_media_backup.frigate import EventQuery, FrigateClient, validate_mp4


def test_validate_mp4_accepts_ftyp_header(tmp_path: Path) -> None:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42")

    validate_mp4(path)


def test_request_logs_in_and_retries_on_401() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/review/summary" and len(calls) == 1:
            return httpx.Response(401)
        if request.url.path == "/api/login":
            return httpx.Response(200, json={"success": True})
        return httpx.Response(200, json={"last24Hours": []})

    client = FrigateClient(
        FrigateConfig(
            base_url="https://frigate.example",
            username="backup",
            password="secret",
        )
    )
    client.client = httpx.Client(
        base_url="https://frigate.example",
        transport=httpx.MockTransport(handler),
    )

    response = client.request("GET", "/api/review/summary")

    assert response.status_code == 200
    assert calls == [
        ("GET", "/api/review/summary"),
        ("POST", "/api/login"),
        ("GET", "/api/review/summary"),
    ]


def test_fetch_clip_to_temp_streams_to_file(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/garden/start/1.000000/end/2.000000/clip.mp4"
        return httpx.Response(200, content=b"\x00\x00\x00\x18ftypmp42payload")

    client = FrigateClient(FrigateConfig(base_url="http://frigate:5000"))
    client.client = httpx.Client(
        base_url="http://frigate:5000",
        transport=httpx.MockTransport(handler),
    )

    artifact = client.fetch_clip_to_temp("garden", "event-1", 1, 2, tmp_path)

    assert artifact.local_path is not None
    assert artifact.local_path.read_bytes() == b"\x00\x00\x00\x18ftypmp42payload"
    assert artifact.relative_path == "garden/clips/event-1-1.000000-2.000000.mp4"


def test_fetch_clip_to_temp_removes_invalid_download(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-an-mp4")

    client = FrigateClient(FrigateConfig(base_url="http://frigate:5000"))
    client.client = httpx.Client(
        base_url="http://frigate:5000",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ValueError):
        client.fetch_clip_to_temp("garden", "event-1", 1, 2, tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_list_clip_events_filters_and_sorts_completed_clip_events() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/events"
        assert request.url.params["has_clip"] == "1"
        assert request.url.params["in_progress"] == "0"
        assert request.url.params["include_thumbnails"] == "0"
        assert float(request.url.params["after"]) == 10
        assert float(request.url.params["before"]) == 20
        return httpx.Response(
            200,
            json=[
                {
                    "id": "later",
                    "camera": "garden",
                    "start_time": 15.0,
                    "end_time": 16.0,
                    "has_clip": True,
                },
                {
                    "id": "no-clip",
                    "camera": "garden",
                    "start_time": 11.0,
                    "end_time": 12.0,
                    "has_clip": False,
                },
                {
                    "id": "earlier",
                    "camera": "front",
                    "start_time": 12.0,
                    "end_time": 13.0,
                    "has_clip": True,
                },
            ],
        )

    client = FrigateClient(FrigateConfig(base_url="http://frigate:5000"))
    client.client = httpx.Client(
        base_url="http://frigate:5000",
        transport=httpx.MockTransport(handler),
    )

    events = client.list_clip_events(EventQuery(after=10, before=20, limit=50))

    assert [event.event_id for event in events] == ["earlier", "later"]
