from __future__ import annotations

import argparse
import logging
import time

from .config import load_config
from .destinations.factory import build_destinations
from .events import ClipEvent
from .frigate import EventQuery, FrigateClient
from .mqtt_runner import MqttRunner
from .service import BackupService
from .state import StateStore


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Back up Frigate media to offsite storage.")
    parser.add_argument(
        "-c",
        "--config",
        default="/config/config.yaml",
        help="Path to YAML config file.",
    )
    subcommands = parser.add_subparsers(dest="command")

    run = subcommands.add_parser("run", help="Run the MQTT backup daemon.")
    run.set_defaults(command="run")

    upload_clip = subcommands.add_parser(
        "upload-clip",
        help="Fetch and upload one explicit Frigate clip time range.",
    )
    upload_clip.add_argument("--camera", required=True, help="Frigate camera name.")
    upload_clip.add_argument("--event-id", required=True, help="Artifact/event id to use.")
    upload_clip.add_argument("--start", type=float, required=True, help="Clip start timestamp.")
    upload_clip.add_argument("--end", type=float, required=True, help="Clip end timestamp.")
    upload_clip.set_defaults(command="upload-clip")

    backfill = subcommands.add_parser(
        "backfill",
        help="Upload recent completed Frigate review items.",
    )
    backfill.add_argument(
        "--since-hours",
        type=float,
        default=24,
        help="Look back this many hours when --after is not set.",
    )
    backfill.add_argument("--after", type=float, default=None, help="Only include events after this timestamp.")
    backfill.add_argument("--before", type=float, default=None, help="Only include events before this timestamp.")
    backfill.add_argument("--limit", type=int, default=100, help="Maximum events to request from Frigate.")
    backfill.add_argument(
        "--dry-run",
        action="store_true",
        help="List matching events without uploading.",
    )
    backfill.set_defaults(command="backfill")
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args.config)
    with FrigateClient(config.frigate) as frigate:
        command = args.command or "run"
        if command == "backfill" and args.dry_run:
            events = frigate.list_clip_events(build_event_query(args))
            for event in events:
                print(
                    f"{event.event_id} {event.camera} "
                    f"{event.start_time:.6f} {event.end_time:.6f}"
                )
            print(f"matched {len(events)} event(s)")
            return 0

        state = StateStore(config.state.path)
        destinations = build_destinations(config.destinations)
        service = BackupService(
            config=config,
            state=state,
            frigate=frigate,
            destinations=destinations,
        )
        if command == "upload-clip":
            event = ClipEvent(
                event_id=args.event_id,
                camera=args.camera,
                start_time=args.start,
                end_time=args.end,
            )
            uploaded = service.upload_clip_event(
                event,
                apply_filters=False,
                apply_padding=False,
            )
            print("uploaded" if uploaded else "already uploaded")
            return 0
        if command == "backfill":
            events = frigate.list_clip_events(build_event_query(args))
            uploaded_count = run_backfill(service, events)
            print(f"uploaded {uploaded_count} of {len(events)} event(s)")
            return 0
        if config.backfill.on_start.enabled:
            events = frigate.list_clip_events(
                EventQuery(
                    after=time.time() - (config.backfill.on_start.since_hours * 3600),
                    limit=config.backfill.on_start.limit,
                )
            )
            uploaded_count = run_backfill(service, events)
            logging.getLogger(__name__).info(
                "Startup backfill complete: uploaded %s of %s event(s)",
                uploaded_count,
                len(events),
            )
        runner = MqttRunner(config.mqtt, service)
        runner.run_forever()
    return 0


def run_backfill(service: BackupService, events: list[ClipEvent]) -> int:
    uploaded_count = 0
    for event in events:
        if service.upload_clip_event(event):
            uploaded_count += 1
    return uploaded_count


def build_event_query(args: argparse.Namespace) -> EventQuery:
    after = args.after
    if after is None and args.since_hours is not None:
        after = time.time() - (args.since_hours * 3600)
    return EventQuery(after=after, before=args.before, limit=args.limit)
