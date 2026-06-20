from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    kind: str
    camera: str
    relative_path: str
    content_type: str
    source_url: str | None = None
    local_path: Path | None = None
    data: bytes | None = None

    def require_file(self) -> Path:
        if self.local_path is None:
            raise ValueError(f"artifact {self.artifact_id} does not have a local file")
        return self.local_path

    def require_bytes(self) -> bytes:
        if self.data is None:
            raise ValueError(f"artifact {self.artifact_id} does not have in-memory data")
        return self.data

