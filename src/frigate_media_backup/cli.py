from __future__ import annotations

import argparse
import logging

from .config import load_config
from .destinations.factory import build_destinations
from .frigate import FrigateClient
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
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args.config)
    state = StateStore(config.state.path)
    destinations = build_destinations(config.destinations)
    with FrigateClient(config.frigate) as frigate:
        service = BackupService(
            config=config,
            state=state,
            frigate=frigate,
            destinations=destinations,
        )
        runner = MqttRunner(config.mqtt, service)
        runner.run_forever()
    return 0
