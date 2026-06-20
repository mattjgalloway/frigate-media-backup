from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import tempfile
from typing import Iterator

import httpx

from .artifact import Artifact
from .config import FrigateConfig


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
