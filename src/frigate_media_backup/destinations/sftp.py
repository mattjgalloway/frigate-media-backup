from __future__ import annotations

from pathlib import PurePosixPath
import posixpath

import paramiko

from frigate_media_backup.artifact import Artifact


class SftpDestination:
    def __init__(
        self,
        *,
        name: str,
        host: str,
        username: str,
        path: str,
        port: int = 22,
        key_file: str | None = None,
        password: str | None = None,
    ) -> None:
        self.name = name
        self.host = host
        self.port = port
        self.username = username
        self.path = path.rstrip("/")
        self.key_file = key_file
        self.password = password

    def upload(self, artifact: Artifact) -> None:
        remote_path = str(PurePosixPath(self.path) / artifact.relative_path)
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        client.connect(
            self.host,
            port=self.port,
            username=self.username,
            key_filename=self.key_file,
            password=self.password,
        )
        try:
            sftp = client.open_sftp()
            try:
                ensure_remote_dir(sftp, posixpath.dirname(remote_path))
                if artifact.local_path:
                    sftp.put(str(artifact.local_path), remote_path)
                else:
                    with sftp.file(remote_path, "wb") as handle:
                        handle.write(artifact.require_bytes())
            finally:
                sftp.close()
        finally:
            client.close()


def ensure_remote_dir(sftp: paramiko.SFTPClient, path: str) -> None:
    parts = [part for part in path.split("/") if part]
    current = ""
    for part in parts:
        current += "/" + part
        try:
            sftp.stat(current)
        except FileNotFoundError:
            sftp.mkdir(current)

