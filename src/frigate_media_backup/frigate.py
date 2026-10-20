from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Self
from urllib.parse import quote

import httpx

from .artifact import Artifact
from .config import FrigateConfig
from .events import ClipEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EventQuery:
    after: float | None = None
    before: float | None = None
    limit: int = 100


class FrigateClient:
    def __init__(self, config: FrigateConfig) -> None:
        self.config = config
        verify: bool | str = config.verify_tls
        if config.ca_bundle:
            verify = str(config.ca_bundle)
        self.client = httpx.Client(
            base_url=config.base_url,
            follow_redirects=True,
            timeout=config.request_timeout_seconds,
            verify=verify,
        )
        self._logged_in = False

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        response = self.client.request(method, path, **kwargs)
        if response.status_code == 401 and self.config.needs_auth:
            self.login()
            response = self.client.request(method, path, **kwargs)
        response.raise_for_status()
        return response

    @contextmanager
    def stream(self, method: str, path: str, **kwargs: object) -> Iterator[httpx.Response]:
        with self.client.stream(method, path, **kwargs) as response:
            if response.status_code != 401 or not self.config.needs_auth:
                response.raise_for_status()
                yield response
                return

        self.login()
        with self.client.stream(method, path, **kwargs) as response:
            response.raise_for_status()
            yield response

    def login(self) -> None:
        password = self.config.password_value
        if not (self.config.username and password):
            raise RuntimeError("Frigate credentials are not configured")
        response = self.client.post(
            "/api/login",
            headers={"Accept": "application/json", "X-CSRF-TOKEN": "1"},
            json={"user": self.config.username, "password": password},
        )
        response.raise_for_status()
        self._logged_in = True

    def fetch_snapshot(self, camera: str, event_id: str) -> Artifact:
        response = self.request("GET", f"/api/events/{event_id}/snapshot.jpg")
        return Artifact(
            artifact_id=f"snapshot:{event_id}",
            kind="snapshot",
            camera=camera,
            relative_path=f"{camera}/snapshots/{event_id}.jpg",
            content_type=response.headers.get("content-type", "image/jpeg"),
            data=response.content,
        )

    def list_clip_events(self, query: EventQuery) -> list[ClipEvent]:
        params: dict[str, int | float] = {"limit": query.limit}
        if query.after is not None:
            params["after"] = query.after
        if query.before is not None:
            params["before"] = query.before
        response = self.request("GET", "/api/review", params=params)
        data = response.json()
        if not isinstance(data, list):
            raise TypeError("Frigate review response must be a list")
        events = [parse_review_item(item) for item in data]
        return sorted(
            (event for event in events if event is not None), key=lambda event: event.start_time
        )

    def fetch_clip_to_temp(
        self,
        camera: str,
        event_id: str,
        start_ts: float,
        end_ts: float,
        tmp_dir: Path,
        path_start_ts: float | None = None,
    ) -> Artifact:
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_path: Path | None = None
        final_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=tmp_dir,
                prefix=f"{event_id}.",
                suffix=".mp4.part",
                delete=False,
            ) as handle:
                tmp_path = Path(handle.name)

            with authenticated_hls_proxy(self) as proxy_base_url:
                camera_path = quote(camera, safe="")
                playlist_url = (
                    f"{proxy_base_url}/vod/clip/{camera_path}/start/{start_ts:.6f}"
                    f"/end/{end_ts:.6f}/master.m3u8"
                )
                duration_seconds = max(0.0, end_ts - start_ts)
                timeout_seconds = max(
                    60.0,
                    duration_seconds + (2 * self.config.request_timeout_seconds),
                )
                run_ffmpeg_hls_download(playlist_url, tmp_path, timeout_seconds)

            validate_mp4(tmp_path)
            final_path = tmp_path.with_suffix("")
            tmp_path.replace(final_path)
        except Exception:
            cleanup_download_file(tmp_path)
            cleanup_download_file(final_path)
            raise
        return Artifact(
            artifact_id=f"clip:{event_id}:{start_ts:.6f}:{end_ts:.6f}",
            kind="clip",
            camera=camera,
            relative_path=clip_relative_path(
                camera,
                event_id,
                start_ts if path_start_ts is None else path_start_ts,
            ),
            content_type="video/mp4",
            local_path=final_path,
        )


class AuthenticatedHlsProxyHandler(BaseHTTPRequestHandler):
    """Loopback proxy that keeps Frigate credentials out of FFmpeg arguments."""

    protocol_version = "HTTP/1.1"

    def __init__(
        self,
        *args: object,
        frigate: FrigateClient,
        **kwargs: object,
    ) -> None:
        self.frigate = frigate
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:
        self.proxy_request("GET")

    def do_HEAD(self) -> None:
        self.proxy_request("HEAD")

    def proxy_request(self, method: str) -> None:
        request_headers = {
            name: value
            for name in ("Accept", "Range", "User-Agent")
            if (value := self.headers.get(name)) is not None
        }
        try:
            response = self.frigate.request(method, self.path, headers=request_headers)
        except httpx.HTTPStatusError as exc:
            upstream_status = exc.response.status_code
            proxy_status = upstream_status if 400 <= upstream_status < 500 else 502
            logger.warning(
                "Frigate HLS proxy upstream returned HTTP %s",
                upstream_status,
            )
            self.send_error(proxy_status, "Frigate HLS request failed")
            return
        except httpx.HTTPError as exc:
            logger.warning("Frigate HLS proxy request failed: %s", type(exc).__name__)
            self.send_error(502, "Frigate HLS request failed")
            return

        content = response.content
        body = b"" if method == "HEAD" else content
        self.send_response(response.status_code)
        for name in ("Content-Type", "Content-Range", "Accept-Ranges"):
            if value := response.headers.get(name):
                self.send_header(name, value)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Connection", "close")
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def log_message(self, _format: str, *_args: object) -> None:
        return


class LoopbackThreadingHttpServer(ThreadingHTTPServer):
    daemon_threads = True


@contextmanager
def authenticated_hls_proxy(frigate: FrigateClient) -> Iterator[str]:
    """Expose Frigate through an ephemeral loopback-only authenticated proxy."""

    handler = partial(AuthenticatedHlsProxyHandler, frigate=frigate)
    server = LoopbackThreadingHttpServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, name="frigate-hls-proxy", daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def run_ffmpeg_hls_download(
    playlist_url: str,
    output_path: Path,
    timeout_seconds: float,
) -> None:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        playlist_url,
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(output_path),
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("FFmpeg is required to download Frigate HLS clips") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"FFmpeg HLS download timed out after {timeout_seconds:.0f} seconds"
        ) from exc

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip()
        if len(stderr) > 4000:
            stderr = stderr[-4000:]
        detail = f": {stderr}" if stderr else ""
        raise RuntimeError(f"FFmpeg HLS download failed with exit code {result.returncode}{detail}")


def validate_mp4(path: Path) -> None:
    with path.open("rb") as handle:
        header = handle.read(12)
    if len(header) < 12 or header[4:8] != b"ftyp":
        raise ValueError(f"{path} is not an MP4 file")


def cleanup_download_file(path: Path | None) -> None:
    if path and path.exists():
        path.unlink()


SAFE_PATH_COMPONENT_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")


def clip_relative_path(camera: str, event_id: str, event_start_ts: float) -> str:
    event_start = datetime.fromtimestamp(event_start_ts, UTC)
    filename = f"{event_start:%Y-%m-%d_%H-%M-%S}_{safe_path_component(event_id)}.mp4"
    return f"{safe_path_component(camera)}/clips/{filename}"


def safe_path_component(value: str) -> str:
    safe = SAFE_PATH_COMPONENT_PATTERN.sub("_", value).strip("._-")
    if not safe:
        return "unknown"
    return safe


def parse_review_item(raw: object) -> ClipEvent | None:
    if not isinstance(raw, dict):
        return None
    review_id = raw.get("id")
    camera = raw.get("camera")
    start_time = raw.get("start_time")
    end_time = raw.get("end_time")
    if not review_id or not camera or start_time is None or end_time is None:
        return None
    return ClipEvent(
        event_id=str(review_id),
        camera=str(camera),
        start_time=float(start_time),
        end_time=float(end_time),
    )
