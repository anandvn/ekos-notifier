"""Command-line entry point. Subcommands are stubs until 0.1."""

from __future__ import annotations

import argparse
import sys

from town_crier import __version__

PLANNED = {
    "run": "run the monitoring service",
    "doctor": "check D-Bus, PHD2, INDI, clock, disk, notifier and sync, and suggest fixes",
    "indi-dump": "write all INDI device properties to a file for a device-support request",
    "simulate": "replay a recorded night to preview alerts",
    "install-service": "install the systemd user service",
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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    print(f"town-crier {args.command}: not implemented yet (pre-alpha).", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
