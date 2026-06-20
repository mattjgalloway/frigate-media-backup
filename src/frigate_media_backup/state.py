from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Iterator


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
"""


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)

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

    def record_failure(self, artifact_id: str, destination: str, error: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO failures (artifact_id, destination, error) VALUES (?, ?, ?)",
                (artifact_id, destination, error),
            )

