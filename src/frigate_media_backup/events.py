from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import time
from typing import Any

from .artifact import Artifact


@dataclass(frozen=True)
class SnapshotEvent:
    event_id: str
    camera: str
    object_label: str
    image: bytes

    def to_artifact(self) -> Artifact:
        return Artifact(
            artifact_id=f"snapshot:{self.event_id}",
            kind="snapshot",
            camera=self.camera,
            relative_path=f"{self.camera}/snapshots/{self.event_id}.jpg",
            content_type="image/jpeg",
            data=self.image,
        )


@dataclass(frozen=True)
class ClipEvent:
    event_id: str
    camera: str
    start_time: float
    end_time: float


BackupEvent = SnapshotEvent | ClipEvent


def parse_mqtt_message(topic: str, payload: bytes, topic_prefix: str = "frigate") -> BackupEvent | None:
    parts = topic.strip("/").split("/")
    if not parts or parts[0] != topic_prefix:
        return None
    if len(parts) >= 4 and parts[3] == "snapshot":
        return parse_snapshot(parts, payload)
    if len(parts) >= 2 and parts[1] == "reviews":
        return parse_review(payload)
    return None


def parse_snapshot(parts: list[str], payload: bytes) -> SnapshotEvent:
    camera = parts[1]
    object_label = parts[2]
    digest = hashlib.sha256(b"\0".join([camera.encode(), object_label.encode(), payload])).hexdigest()
    # Frigate snapshot MQTT payloads do not carry a stable event id in the topic, so include time
    # to avoid collapsing repeated snapshots with identical bytes.
    event_id = f"{int(time.time())}-{digest[:16]}"
    return SnapshotEvent(
        event_id=event_id,
        camera=camera,
        object_label=object_label,
        image=payload,
    )


def parse_review(payload: bytes) -> ClipEvent | None:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("type") != "end":
        return None
    after = data.get("after")
    if not isinstance(after, dict):
        return None
    end_time = after.get("end_time")
    if end_time is None:
        return None
    event_id = require_review_value(after, "id")
    camera = require_review_value(after, "camera")
    start_time = float(require_review_value(after, "start_time"))
    return ClipEvent(
        event_id=str(event_id),
        camera=str(camera),
        start_time=start_time,
        end_time=float(end_time),
    )


def require_review_value(review: dict[str, Any], key: str) -> Any:
    value = review.get(key)
    if value is None:
        raise ValueError(f"Review payload missing {key}")
    return value

