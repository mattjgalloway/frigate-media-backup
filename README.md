# Frigate Media Backup

Docker-friendly offsite backup worker for Frigate clips and snapshots.

The service listens to Frigate MQTT events, fetches media from the Frigate API, and uploads each artifact to one or more destinations such as Backblaze B2 via S3, SFTP, or a local filesystem path.

## Goals

- Support Frigate's unauthenticated internal API, usually `http://frigate:5000`.
- Support Frigate's authenticated API, usually `http://frigate:8971` or `https://frigate:8971`.
- Keep TLS verification enabled by default, with optional custom CA bundles for private certificates.
- Store durable upload state in SQLite so restarts do not duplicate completed uploads.
- Run as a non-root Docker container with no inbound ports.
- Support multiple destinations per artifact.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## Running

```bash
frigate-media-backup --config /config/config.yaml
```

See [examples/config.yaml](examples/config.yaml) and [docker/docker-compose.yaml](docker/docker-compose.yaml).

