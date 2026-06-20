from __future__ import annotations

from pathlib import Path
import shutil

from frigate_media_backup.artifact import Artifact


class FilesystemDestination:
    def __init__(self, name: str, path: str | Path) -> None:
        self.name = name
        self.root = Path(path)

    def upload(self, artifact: Artifact) -> None:
        destination = self.root / artifact.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if artifact.local_path:
            shutil.copy2(artifact.local_path, destination)
        else:
            destination.write_bytes(artifact.require_bytes())

