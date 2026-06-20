# Frigate Media Backup

Docker-friendly offsite backup for Frigate clips and snapshots.

Frigate Media Backup listens to Frigate MQTT events, fetches media from the Frigate API, and uploads each artifact to one or more destinations. It supports authenticated and unauthenticated Frigate API access, TLS verification, Backblaze B2 through the S3-compatible API, SFTP, and local filesystem storage.

## Features

- Back up Frigate review clips from MQTT review-end events.
- Optionally back up Frigate snapshot MQTT image payloads.
- Connect to the unauthenticated Frigate API, usually `http://frigate:5000`.
- Connect to the authenticated Frigate API, usually `http://frigate:8971` or `https://frigate:8971`.
- Keep TLS verification enabled by default, with optional custom CA bundles.
- Upload to multiple destinations per artifact.
- Store upload state in SQLite so restarts do not duplicate completed uploads.
- Stream clips to temporary files before upload, avoiding large in-memory MP4s.
- Backfill recent completed clip events manually or once at daemon startup.
- Run as a non-root Docker container with no inbound ports.

## Quick Start With Docker Compose

Create a working directory on the host that will run the backup service:

```bash
mkdir -p frigate-media-backup/{config,secrets,state,backup,docker}
cd frigate-media-backup
```

Download the example Compose and config files from this repository:

```bash
curl -fsSLo docker/docker-compose.yaml \
  https://raw.githubusercontent.com/mattjgalloway/frigate-media-backup/main/docker/docker-compose.yaml
curl -fsSLo config/config.yaml \
  https://raw.githubusercontent.com/mattjgalloway/frigate-media-backup/main/examples/config.yaml
```

Edit `config/config.yaml` for your Frigate, MQTT, state, upload, and destination settings.

If you use password files, place them under `secrets/` and reference them from the config as paths under `/run/secrets`. For example:

```bash
printf '%s\n' 'your-frigate-password' > secrets/frigate_password
chmod 600 secrets/frigate_password
```

For S3-compatible destinations such as Backblaze B2, put credentials in `.env`:

```bash
cat > .env <<'EOF'
B2_KEY_ID=your-application-key-id
B2_APP_KEY=your-application-key
EOF
chmod 600 .env
```

The image runs as UID `10001`, so make the writable directories accessible to that UID:

```bash
sudo chown -R 10001:10001 state backup
```

Start the service:

```bash
docker compose -f docker/docker-compose.yaml up -d
docker compose -f docker/docker-compose.yaml logs -f frigate-media-backup
```

The container reads `/config/config.yaml`, stores durable state in `/state`, and has no inbound ports.

## Docker Image

Published images are available from GitHub Container Registry:

```text
ghcr.io/mattjgalloway/frigate-media-backup:latest
ghcr.io/mattjgalloway/frigate-media-backup:v0.1.0
ghcr.io/mattjgalloway/frigate-media-backup:sha-<commit>
```

You can also run the container directly:

```bash
docker run --rm \
  --name frigate-media-backup \
  --env-file .env \
  -v "$PWD/config/config.yaml:/config/config.yaml:ro" \
  -v "$PWD/secrets:/run/secrets:ro" \
  -v "$PWD/state:/state" \
  -v "$PWD/backup:/backup" \
  ghcr.io/mattjgalloway/frigate-media-backup:latest
```

To build locally from a checkout:

```bash
docker build -f docker/Dockerfile -t frigate-media-backup:local .
docker run --rm \
  --name frigate-media-backup \
  --env-file .env \
  -v "$PWD/config/config.yaml:/config/config.yaml:ro" \
  -v "$PWD/secrets:/run/secrets:ro" \
  -v "$PWD/state:/state" \
  -v "$PWD/backup:/backup" \
  frigate-media-backup:local
```

## Commands

### Run the Daemon

```bash
frigate-media-backup --config /config/config.yaml
```

This is the default command used by the Docker image. It connects to MQTT and handles new Frigate events as they arrive.

The explicit form is also available:

```bash
frigate-media-backup --config /config/config.yaml run
```

### Upload One Clip

Use `upload-clip` to test a known Frigate time range or manually upload a specific clip:

```bash
frigate-media-backup --config /config/config.yaml upload-clip \
  --camera front \
  --event-id manual-front-test \
  --start 1781971106.255446 \
  --end 1781971119.162927
```

`upload-clip` uses the exact start/end timestamps you provide. It bypasses clip camera filters and does not add configured clip padding, because the command is for explicit admin uploads.

### Backfill Recent Clips

Use `backfill` to upload recently completed Frigate events that already have clips:

```bash
frigate-media-backup --config /config/config.yaml backfill --since-hours 24 --limit 100
```

Preview matching events without uploading:

```bash
frigate-media-backup --config /config/config.yaml backfill \
  --since-hours 24 \
  --limit 100 \
  --dry-run
```

Use explicit timestamps when you want a precise window:

```bash
frigate-media-backup --config /config/config.yaml backfill \
  --after 1781960000 \
  --before 1781970000 \
  --limit 200
```

Backfill respects the configured clip camera allowlist and clip padding. It also uses the SQLite state database, so already-uploaded clip/destination pairs are skipped.

## Configuration

Use `examples/config.yaml` as the canonical configuration reference.

### Frigate

```yaml
frigate:
  base_url: "https://frigate.example.com:8971"
  username: "backup-user"
  password: null
  password_file: "/run/secrets/frigate_password"
  verify_tls: true
  ca_bundle: null
  request_timeout_seconds: 60
```

- `base_url`: Frigate API base URL.
- `username`: Frigate username for authenticated API access. Use `null` for unauthenticated access.
- `password`: Inline Frigate password. Prefer `password_file` for Docker.
- `password_file`: File containing the Frigate password.
- `verify_tls`: Verify HTTPS certificates. Defaults to `true`.
- `ca_bundle`: Optional CA bundle path for a private CA.
- `request_timeout_seconds`: HTTP timeout for Frigate requests.

If any Frigate auth field is set, both `username` and one of `password` or `password_file` must be set.

### MQTT

```yaml
mqtt:
  host: "mosquitto"
  port: 1883
  topic_prefix: "frigate"
  username: null
  password: null
  password_file: null
  client_id: "frigate-media-backup"
  keepalive_seconds: 60
```

- `host`: MQTT broker hostname or IP address.
- `port`: MQTT broker port.
- `topic_prefix`: Frigate MQTT topic prefix. Usually `frigate`.
- `username`: MQTT username, if required.
- `password`: Inline MQTT password. Prefer `password_file` for Docker.
- `password_file`: File containing the MQTT password.
- `client_id`: MQTT client id.
- `keepalive_seconds`: MQTT keepalive interval.

If any MQTT auth field is set, both `username` and one of `password` or `password_file` must be set.

### State

```yaml
state:
  path: "/state/state.sqlite"
  tmp_dir: "/state/tmp"
```

- `path`: SQLite database path used to record completed uploads and failures.
- `tmp_dir`: Temporary directory for in-progress clip downloads.

Mount `/state` to persistent storage. If the container restarts, the SQLite database prevents already-completed artifact/destination pairs from being uploaded again.

### Uploads

```yaml
uploads:
  snapshots:
    enabled: false
    cameras: []
    objects: []
    min_interval_seconds: 60

  clips:
    enabled: true
    cameras: []
    padding_before_seconds: 5
    padding_after_seconds: 5
```

- `uploads.snapshots.enabled`: Enable snapshot uploads from `frigate/<camera>/<object>/snapshot` MQTT payloads.
- `uploads.snapshots.cameras`: Snapshot camera allowlist. Empty means all cameras.
- `uploads.snapshots.objects`: Snapshot object-label allowlist. Empty means all object labels.
- `uploads.snapshots.min_interval_seconds`: Per `(camera, object)` snapshot rate limit.
- `uploads.clips.enabled`: Enable review clip uploads from `frigate/reviews` MQTT `end` messages.
- `uploads.clips.cameras`: Clip camera allowlist. Empty means all cameras.
- `uploads.clips.padding_before_seconds`: Seconds to subtract from Frigate's review start time.
- `uploads.clips.padding_after_seconds`: Seconds to add to Frigate's review end time.

Clips are enabled by default. Snapshots are disabled by default because snapshot topics can be frequent and may include cameras you do not want to back up continuously.

### Startup Backfill

```yaml
backfill:
  on_start:
    enabled: false
    since_hours: 24
    limit: 100
```

- `backfill.on_start.enabled`: Run a bounded clip backfill before the MQTT daemon starts.
- `backfill.on_start.since_hours`: Look back this many hours from process startup.
- `backfill.on_start.limit`: Maximum completed clip events to request from Frigate.

Startup backfill is disabled by default so first boot cannot unexpectedly upload a large historical backlog. When enabled, it uses the same SQLite upload state as normal operation, so clips already uploaded to all configured destinations are skipped before the MP4 is fetched again.

### Destinations

Configure one or more destinations. An artifact is marked complete only after a destination confirms upload success.

#### Filesystem

```yaml
destinations:
  - type: filesystem
    name: local
    path: "/backup/frigate"
```

- `name`: Unique destination name for state tracking.
- `path`: Root directory where artifacts are written.

Mount the host directory into the container if you use this destination.

#### Backblaze B2 or S3-Compatible Storage

```yaml
destinations:
  - type: s3
    name: b2
    bucket: "your-bucket"
    prefix: "frigate/"
    endpoint_url: "https://s3.your-region.backblazeb2.com"
    region: "your-region"
    access_key_id_env: "B2_KEY_ID"
    secret_access_key_env: "B2_APP_KEY"
```

- `bucket`: Bucket name.
- `prefix`: Optional object key prefix.
- `endpoint_url`: S3 endpoint URL. Required for B2 and other S3-compatible providers.
- `region`: Provider region.
- `access_key_id_env`: Environment variable containing the access key id.
- `secret_access_key_env`: Environment variable containing the secret access key.

For Backblaze B2, create a restricted application key for the target bucket and prefix where possible.

#### SFTP

```yaml
destinations:
  - type: sftp
    name: offsite
    host: "backup.example.com"
    port: 22
    username: "frigate"
    known_hosts_file: "/run/secrets/known_hosts"
    key_file: "/run/secrets/sftp_key"
    password: null
    path: "/srv/frigate"
```

- `host`: SFTP server hostname or IP address.
- `port`: SSH port.
- `username`: SSH username.
- `known_hosts_file`: Optional known-hosts file path inside the container.
- `key_file`: Private key path inside the container.
- `password`: Optional SSH password.
- `path`: Remote destination root.

The SFTP client loads system host keys and rejects unknown host keys. For Docker, mount a known-hosts file and set `known_hosts_file`.

## Security

Prefer one of these Frigate connectivity models:

- Use Frigate's authenticated HTTPS API with a certificate trusted by the backup container. This is the best default for separate hosts.
- Use Frigate's authenticated HTTPS API with a private CA and set `ca_bundle` to the mounted CA bundle path.
- Use Frigate's unauthenticated API only when Frigate and this service share a private Docker network on the same host, or when the network path is otherwise strongly isolated.
- If Frigate and this service are on different machines, use real TLS, a private CA, WireGuard, Tailscale, or another secure tunnel between them.

Avoid `verify_tls: false` except for short-lived testing. It disables certificate validation and makes it easier for another system on the path to impersonate Frigate.

Do not commit secrets. Use Docker secrets, mounted files under `/run/secrets`, or environment variables passed from a local `.env` file.

## Artifact Paths

Artifacts are uploaded using deterministic relative paths:

```text
<camera>/snapshots/<snapshot-id>.jpg
<camera>/clips/<review-id>-<start>-<end>.mp4
```

For S3-compatible destinations, `prefix` is prepended to that path.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
ruff check .
pytest
```

Run locally without Docker:

```bash
frigate-media-backup --config config/config.yaml
```

## Release

CI runs linting and tests on pull requests and pushes to `main`. The Docker workflow builds images for pull requests and publishes to GitHub Container Registry for pushes to `main`, version tags such as `v0.1.0`, and manual workflow runs.

Release checklist:

```bash
ruff check .
pytest
git status --short
git tag v0.1.0
git push origin main
git push origin v0.1.0
```

After the tag workflow completes, verify that the GHCR package is public and that the versioned image can be pulled:

```bash
docker pull ghcr.io/mattjgalloway/frigate-media-backup:v0.1.0
```

## License

MIT. See [LICENSE](LICENSE).
