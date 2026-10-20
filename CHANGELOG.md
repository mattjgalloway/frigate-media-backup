# Changelog

## 0.1.3 - 2026-10-20

- Fetch Frigate clip time ranges through the HLS endpoint and remux them locally with FFmpeg.
- Keep Frigate credentials and session cookies out of FFmpeg process arguments by using a
  temporary loopback-only authenticated proxy.
- Bound FFmpeg execution time and clean up partial files after failures or timeouts.
- Include FFmpeg in the Docker image.

## 0.1.2 - 2026-10-19

- Drain deferred retries from the MQTT worker.
- Retry media work after transient failures.
