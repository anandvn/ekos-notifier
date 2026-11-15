"""Command-line entry point. Subcommands are stubs until 0.1."""

from __future__ import annotations

import argparse
import sys

from town_crier import __version__

# Distinct from argparse's usage-error code (2) so scripts can tell them apart.
EXIT_NOT_IMPLEMENTED = 3

PLANNED = {
    "run": "run the monitoring service",
    "doctor": "check D-Bus, PHD2, INDI, clock, disk, notifier and sync, and suggest fixes",
    "indi-dump": "write all INDI device properties to a file for a device-support request",
    "simulate": "replay a recorded night to preview alerts",
    "install-service": "install the systemd user service",
}

PLANNED_CONFIG = {
    "check": "validate the config file and explain any errors",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="town-crier",
        description="Alerts, hourly digests and session records for KStars/Ekos rigs.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")
    for name, help_text in PLANNED.items():
        sub.add_parser(name, help=f"{help_text} (not yet implemented)")

    config = sub.add_parser("config", help="work with the config file")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    for name, help_text in PLANNED_CONFIG.items():
        config_sub.add_parser(name, help=f"{help_text} (not yet implemented)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    name = args.command
    if name == "config":
        name = f"config {args.config_command}"
    print(f"town-crier {name}: not implemented yet (pre-alpha).", file=sys.stderr)
    return EXIT_NOT_IMPLEMENTED


if __name__ == "__main__":
    raise SystemExit(main())
