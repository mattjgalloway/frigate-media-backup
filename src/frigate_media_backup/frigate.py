from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import tempfile
from typing import Iterator

import httpx

from .artifact import Artifact
from .config import FrigateConfig
from .events import ClipEvent


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

    def __enter__(self) -> FrigateClient:
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
        params: dict[str, int | float] = {
            "has_clip": 1,
            "in_progress": 0,
            "include_thumbnails": 0,
            "limit": query.limit,
        }
        if query.after is not None:
            params["after"] = query.after
        if query.before is not None:
            params["before"] = query.before
        response = self.request("GET", "/api/events", params=params)
        data = response.json()
        if not isinstance(data, list):
            raise ValueError("Frigate events response must be a list")
        events = [parse_clip_event(item) for item in data]
        return sorted((event for event in events if event is not None), key=lambda event: event.start_time)

    def fetch_clip_to_temp(
        self,
        camera: str,
        event_id: str,
        start_ts: float,
        end_ts: float,
        tmp_dir: Path,
    ) -> Artifact:
        tmp_dir.mkdir(parents=True, exist_ok=True)
        path = f"/api/{camera}/start/{start_ts:.6f}/end/{end_ts:.6f}/clip.mp4"
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
                with self.stream("GET", path) as response:
                    for chunk in response.iter_bytes():
                        handle.write(chunk)
            final_path = tmp_path.with_suffix("")
            tmp_path.replace(final_path)
            validate_mp4(final_path)
        except Exception:
            cleanup_download_file(tmp_path)
            cleanup_download_file(final_path)
            raise
        return Artifact(
            artifact_id=f"clip:{event_id}:{start_ts:.6f}:{end_ts:.6f}",
            kind="clip",
            camera=camera,
            relative_path=f"{camera}/clips/{event_id}-{start_ts:.6f}-{end_ts:.6f}.mp4",
            content_type="video/mp4",
            local_path=final_path,
        )


def validate_mp4(path: Path) -> None:
    with path.open("rb") as handle:
        header = handle.read(12)
    if len(header) < 12 or header[4:8] != b"ftyp":
        raise ValueError(f"{path} is not an MP4 file")


def cleanup_download_file(path: Path | None) -> None:
    if path and path.exists():
        path.unlink()


def parse_clip_event(raw: object) -> ClipEvent | None:
    if not isinstance(raw, dict) or not raw.get("has_clip"):
        return None
    event_id = raw.get("id")
    camera = raw.get("camera")
    start_time = raw.get("start_time")
    end_time = raw.get("end_time")
    if not event_id or not camera or start_time is None or end_time is None:
        return None
    return ClipEvent(
        event_id=str(event_id),
        camera=str(camera),
        start_time=float(start_time),
        end_time=float(end_time),
    )
