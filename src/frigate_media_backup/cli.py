from __future__ import annotations

import argparse


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
    parser = build_parser()
    parser.parse_args(argv)
    parser.error("service implementation is not wired yet")
    return 2

