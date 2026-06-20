from __future__ import annotations

import tempfile

import boto3

from frigate_media_backup.artifact import Artifact
from frigate_media_backup.config import env_secret


class S3Destination:
    def __init__(
        self,
        *,
        name: str,
        bucket: str,
        prefix: str = "",
        endpoint_url: str | None = None,
        region: str | None = None,
        access_key_id_env: str | None = None,
        secret_access_key_env: str | None = None,
    ) -> None:
        self.name = name
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        key_id = env_secret(access_key_id_env) if access_key_id_env else None
        secret = env_secret(secret_access_key_env) if secret_access_key_env else None
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            aws_access_key_id=key_id,
            aws_secret_access_key=secret,
        )

    def upload(self, artifact: Artifact) -> None:
        key = "/".join(part for part in [self.prefix, artifact.relative_path] if part)
        extra_args = {"ContentType": artifact.content_type}
        if artifact.local_path:
            self.client.upload_file(str(artifact.local_path), self.bucket, key, ExtraArgs=extra_args)
            return
        # boto3's upload_fileobj expects a file-like object; spooling keeps snapshots cheap while
        # preserving a seekable object for retries.
        with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as handle:
            handle.write(artifact.require_bytes())
            handle.seek(0)
            self.client.upload_fileobj(handle, self.bucket, key, ExtraArgs=extra_args)
