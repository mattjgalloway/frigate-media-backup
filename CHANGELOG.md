# Changelog

## 0.1.5 - 2026-10-21

- Use Certifi 2026.10.19 or newer for S3-compatible HTTPS certificate verification, avoiding
  Botocore's lagging bundled CA store after the eMudhra intermediate CA compromise.

## 0.1.4 - 2026-10-20

- Retire deferred clip fetches after a configurable maximum age instead of retrying
  recordings that Frigate has already expired.
- Preserve upstream Frigate 4xx status codes through the authenticated HLS proxy so missing
  recordings are no longer reported as generic proxy 5xx failures.

## 0.1.3 - 2026-10-20

- Fetch Frigate clip time ranges through the HLS endpoint and remux them locally with FFmpeg.
- Keep Frigate credentials and session cookies out of FFmpeg process arguments by using a
  temporary loopback-only authenticated proxy.
- Bound FFmpeg execution time and clean up partial files after failures or timeouts.
- Include FFmpeg in the Docker image.

## 0.1.2 - 2026-10-19

- Drain deferred retries from the MQTT worker.
- Retry media work after transient failures.
