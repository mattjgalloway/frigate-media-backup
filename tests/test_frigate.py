import subprocess
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import urlopen

import httpx
import pytest

from frigate_media_backup.config import FrigateConfig
from frigate_media_backup.frigate import (
    EventQuery,
    FrigateClient,
    authenticated_hls_proxy,
    clip_relative_path,
    validate_mp4,
)


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


def test_fetch_clip_to_temp_uses_authenticated_hls_proxy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/login":
            return httpx.Response(
                200,
                json={"success": True},
                headers={"set-cookie": "frigate_token=session-value; Path=/; HttpOnly"},
            )
        if "frigate_token=session-value" not in request.headers.get("cookie", ""):
            return httpx.Response(401)
        if request.url.path.endswith("/master.m3u8"):
            return httpx.Response(
                200,
                content=b"#EXTM3U\nindex.m3u8\n",
                headers={"content-type": "application/vnd.apple.mpegurl"},
            )
        if request.url.path.endswith("/index.m3u8"):
            return httpx.Response(
                200,
                content=b"#EXTM3U\n#EXT-X-ENDLIST\n",
                headers={"content-type": "application/vnd.apple.mpegurl"},
            )
        return httpx.Response(404)

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        command_text = " ".join(command)
        assert "secret" not in command_text
        assert "session-value" not in command_text
        assert "/vod/clip/garden/start/1.000000/end/2.000000/master.m3u8" in command_text

        playlist_url = command[command.index("-i") + 1]
        with urlopen(playlist_url, timeout=2) as response:
            master = response.read().decode()
        media_path = next(line for line in master.splitlines() if not line.startswith("#"))
        with urlopen(urljoin(playlist_url, media_path), timeout=2) as response:
            assert response.read() == b"#EXTM3U\n#EXT-X-ENDLIST\n"

        Path(command[-1]).write_bytes(b"\x00\x00\x00\x18ftypmp42payload")
        return subprocess.CompletedProcess(command, 0, b"", b"")

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
    monkeypatch.setattr(subprocess, "run", fake_run)

    artifact = client.fetch_clip_to_temp("garden", "event-1", 1, 2, tmp_path)

    assert artifact.local_path is not None
    assert artifact.local_path.read_bytes() == b"\x00\x00\x00\x18ftypmp42payload"
    assert artifact.relative_path == "garden/clips/1970-01-01_00-00-01_event-1.mp4"
    assert calls == [
        (
            "GET",
            "/vod/clip/garden/start/1.000000/end/2.000000/master.m3u8",
        ),
        ("POST", "/api/login"),
        (
            "GET",
            "/vod/clip/garden/start/1.000000/end/2.000000/master.m3u8",
        ),
        (
            "GET",
            "/vod/clip/garden/start/1.000000/end/2.000000/index.m3u8",
        ),
    ]


def test_authenticated_hls_proxy_preserves_upstream_not_found() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "No recordings found."})

    client = FrigateClient(FrigateConfig(base_url="http://frigate:5000"))
    client.client = httpx.Client(
        base_url="http://frigate:5000",
        transport=httpx.MockTransport(handler),
    )

    with (
        authenticated_hls_proxy(client) as proxy_base_url,
        pytest.raises(HTTPError) as exc_info,
    ):
        urlopen(f"{proxy_base_url}/vod/clip/garden/start/1/end/2/master.m3u8")

    assert exc_info.value.code == 404


def test_clip_relative_path_uses_event_start_time_and_safe_components() -> None:
    assert (
        clip_relative_path("../front camera", "../event 1", 1781982522.0)
        == "front_camera/clips/2026-06-20_19-08-42_event_1.mp4"
    )


def test_fetch_clip_to_temp_removes_failed_ffmpeg_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        Path(command[-1]).write_bytes(b"partial-output")
        return subprocess.CompletedProcess(command, 1, b"", b"broken playlist")

    client = FrigateClient(FrigateConfig(base_url="http://frigate:5000"))
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="broken playlist"):
        client.fetch_clip_to_temp("garden", "event-1", 1, 2, tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_fetch_clip_to_temp_removes_output_when_ffmpeg_times_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        Path(command[-1]).write_bytes(b"partial-output")
        raise subprocess.TimeoutExpired(command, 60)

    client = FrigateClient(FrigateConfig(base_url="http://frigate:5000"))
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="timed out after 121 seconds"):
        client.fetch_clip_to_temp("garden", "event-1", 1, 2, tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_fetch_clip_to_temp_reports_missing_ffmpeg_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(_command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError("ffmpeg")

    client = FrigateClient(FrigateConfig(base_url="http://frigate:5000"))
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="FFmpeg is required"):
        client.fetch_clip_to_temp("garden", "event-1", 1, 2, tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_list_clip_events_fetches_review_items_and_sorts_them() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/review"
        assert float(request.url.params["after"]) == 10
        assert float(request.url.params["before"]) == 20
        return httpx.Response(
            200,
            json=[
                {
                    "id": "review-later",
                    "camera": "garden",
                    "start_time": 15.0,
                    "end_time": 16.0,
                },
                {
                    "id": "incomplete",
                    "camera": "garden",
                    "start_time": 11.0,
                },
                {
                    "id": "review-earlier",
                    "camera": "front",
                    "start_time": 12.0,
                    "end_time": 13.0,
                },
            ],
        )

    client = FrigateClient(FrigateConfig(base_url="http://frigate:5000"))
    client.client = httpx.Client(
        base_url="http://frigate:5000",
        transport=httpx.MockTransport(handler),
    )

    events = client.list_clip_events(EventQuery(after=10, before=20, limit=50))

    assert [event.event_id for event in events] == ["review-earlier", "review-later"]
