from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Iterator

from .artifact import Artifact


SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
    artifact_id TEXT NOT NULL,
    destination TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (artifact_id, destination)
);

CREATE TABLE IF NOT EXISTS failures (
    artifact_id TEXT NOT NULL,
    destination TEXT NOT NULL,
    error TEXT NOT NULL,
    failed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pending_uploads (
    artifact_id TEXT NOT NULL,
    destination TEXT NOT NULL,
    kind TEXT NOT NULL,
    camera TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    content_type TEXT NOT NULL,
    local_path TEXT NOT NULL,
    attempt_count INTEGER NOT NULL,
    next_attempt_at REAL NOT NULL,
    last_error TEXT NOT NULL,
    last_attempt_at REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (artifact_id, destination)
);

CREATE TABLE IF NOT EXISTS pending_clip_fetches (
    artifact_id TEXT NOT NULL PRIMARY KEY,
    event_id TEXT NOT NULL,
    camera TEXT NOT NULL,
    event_start_time REAL NOT NULL,
    fetch_start_time REAL NOT NULL,
    fetch_end_time REAL NOT NULL,
    apply_filters INTEGER NOT NULL,
    apply_padding INTEGER NOT NULL,
    attempt_count INTEGER NOT NULL,
    next_attempt_at REAL NOT NULL,
    last_error TEXT NOT NULL,
    last_attempt_at REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""


@dataclass(frozen=True)
class PendingUpload:
    artifact_id: str
    destination: str
    kind: str
    camera: str
    relative_path: str
    content_type: str
    local_path: Path
    attempt_count: int

    def to_artifact(self) -> Artifact:
        return Artifact(
            artifact_id=self.artifact_id,
            kind=self.kind,
            camera=self.camera,
            relative_path=self.relative_path,
            content_type=self.content_type,
            local_path=self.local_path,
        )


@dataclass(frozen=True)
class PendingClipFetch:
    artifact_id: str
    event_id: str
    camera: str
    event_start_time: float
    fetch_start_time: float
    fetch_end_time: float
    apply_filters: bool
    apply_padding: bool
    attempt_count: int


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    @property
    def cache_dir(self) -> Path:
        return self.path.parent / "cache"

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            yield connection
            connection.commit()
        finally:
            connection.close()

    def is_uploaded(self, artifact_id: str, destination: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM uploads WHERE artifact_id = ? AND destination = ?",
                (artifact_id, destination),
            ).fetchone()
        return row is not None

    def mark_uploaded(self, artifact_id: str, destination: str, relative_path: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO uploads (artifact_id, destination, relative_path)
                VALUES (?, ?, ?)
                """,
                (artifact_id, destination, relative_path),
            )
            connection.execute(
                "DELETE FROM pending_uploads WHERE artifact_id = ? AND destination = ?",
                (artifact_id, destination),
            )

    def record_failure(self, artifact_id: str, destination: str, error: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO failures (artifact_id, destination, error) VALUES (?, ?, ?)",
                (artifact_id, destination, error),
            )

    def pending_upload_attempt_count(self, artifact_id: str, destination: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT attempt_count FROM pending_uploads
                WHERE artifact_id = ? AND destination = ?
                """,
                (artifact_id, destination),
            ).fetchone()
        return int(row[0]) if row else 0

    def upsert_pending_upload(
        self,
        *,
        artifact_id: str,
        destination: str,
        kind: str,
        camera: str,
        relative_path: str,
        content_type: str,
        local_path: Path,
        attempt_count: int,
        next_attempt_at: float,
        error: str,
        now: float,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO pending_uploads (
                    artifact_id,
                    destination,
                    kind,
                    camera,
                    relative_path,
                    content_type,
                    local_path,
                    attempt_count,
                    next_attempt_at,
                    last_error,
                    last_attempt_at,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(artifact_id, destination) DO UPDATE SET
                    kind = excluded.kind,
                    camera = excluded.camera,
                    relative_path = excluded.relative_path,
                    content_type = excluded.content_type,
                    local_path = excluded.local_path,
                    attempt_count = excluded.attempt_count,
                    next_attempt_at = excluded.next_attempt_at,
                    last_error = excluded.last_error,
                    last_attempt_at = excluded.last_attempt_at,
                    updated_at = excluded.updated_at
                """,
                (
                    artifact_id,
                    destination,
                    kind,
                    camera,
                    relative_path,
                    content_type,
                    str(local_path),
                    attempt_count,
                    next_attempt_at,
                    error,
                    now,
                    now,
                    now,
                ),
            )

    def due_pending_uploads(self, now: float, *, limit: int = 50) -> list[PendingUpload]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT artifact_id, destination, kind, camera, relative_path, content_type,
                    local_path, attempt_count
                FROM pending_uploads
                WHERE next_attempt_at <= ?
                ORDER BY next_attempt_at, updated_at
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
        return [
            PendingUpload(
                artifact_id=row[0],
                destination=row[1],
                kind=row[2],
                camera=row[3],
                relative_path=row[4],
                content_type=row[5],
                local_path=Path(row[6]),
                attempt_count=int(row[7]),
            )
            for row in rows
        ]

    def delete_pending_upload(self, artifact_id: str, destination: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM pending_uploads WHERE artifact_id = ? AND destination = ?",
                (artifact_id, destination),
            )

    def pending_clip_fetch_attempt_count(self, artifact_id: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT attempt_count FROM pending_clip_fetches WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    def upsert_pending_clip_fetch(
        self,
        *,
        artifact_id: str,
        event_id: str,
        camera: str,
        event_start_time: float,
        fetch_start_time: float,
        fetch_end_time: float,
        apply_filters: bool,
        apply_padding: bool,
        attempt_count: int,
        next_attempt_at: float,
        error: str,
        now: float,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO pending_clip_fetches (
                    artifact_id,
                    event_id,
                    camera,
                    event_start_time,
                    fetch_start_time,
                    fetch_end_time,
                    apply_filters,
                    apply_padding,
                    attempt_count,
                    next_attempt_at,
                    last_error,
                    last_attempt_at,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(artifact_id) DO UPDATE SET
                    event_id = excluded.event_id,
                    camera = excluded.camera,
                    event_start_time = excluded.event_start_time,
                    fetch_start_time = excluded.fetch_start_time,
                    fetch_end_time = excluded.fetch_end_time,
                    apply_filters = excluded.apply_filters,
                    apply_padding = excluded.apply_padding,
                    attempt_count = excluded.attempt_count,
                    next_attempt_at = excluded.next_attempt_at,
                    last_error = excluded.last_error,
                    last_attempt_at = excluded.last_attempt_at,
                    updated_at = excluded.updated_at
                """,
                (
                    artifact_id,
                    event_id,
                    camera,
                    event_start_time,
                    fetch_start_time,
                    fetch_end_time,
                    int(apply_filters),
                    int(apply_padding),
                    attempt_count,
                    next_attempt_at,
                    error,
                    now,
                    now,
                    now,
                ),
            )

    def due_pending_clip_fetches(self, now: float, *, limit: int = 50) -> list[PendingClipFetch]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT artifact_id, event_id, camera, event_start_time,
                    fetch_start_time, fetch_end_time, apply_filters, apply_padding,
                    attempt_count
                FROM pending_clip_fetches
                WHERE next_attempt_at <= ?
                ORDER BY next_attempt_at, updated_at
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
        return [
            PendingClipFetch(
                artifact_id=row[0],
                event_id=row[1],
                camera=row[2],
                event_start_time=float(row[3]),
                fetch_start_time=float(row[4]),
                fetch_end_time=float(row[5]),
                apply_filters=bool(row[6]),
                apply_padding=bool(row[7]),
                attempt_count=int(row[8]),
            )
            for row in rows
        ]

    def delete_pending_clip_fetch(self, artifact_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM pending_clip_fetches WHERE artifact_id = ?",
                (artifact_id,),
            )
