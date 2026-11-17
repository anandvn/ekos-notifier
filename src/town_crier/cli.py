"""Command-line entry point. Subcommands are stubs until 0.1."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from town_crier import __version__

# Distinct from argparse's usage-error code (2) so scripts can tell them apart.
EXIT_NOT_IMPLEMENTED = 3

PLANNED = {
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
    run = sub.add_parser(
        "run",
        help="run the monitoring service in the foreground",
        description="Run the monitoring service in the foreground until Ctrl-C or SIGTERM. "
        "The systemd service runs this; run it by hand to watch what Town Crier sees.",
    )
    run.add_argument(
        "--db",
        metavar="PATH",
        help="database file (default: ~/.local/share/town-crier/town-crier.db)",
    )
    run.add_argument("-v", "--verbose", action="store_true", help="debug logging")
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
    if name == "run":
        return cmd_run(args)
    if name == "config":
        name = f"config {args.config_command}"
    print(f"town-crier {name}: not implemented yet (pre-alpha).", file=sys.stderr)
    return EXIT_NOT_IMPLEMENTED


def cmd_run(args: argparse.Namespace) -> int:
    from town_crier.service import serve
    from town_crier.store import default_path

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(serve(args.db or default_path()))
    except Exception as exc:  # noqa: BLE001 - report, don't dump a traceback on users
        logging.getLogger("town_crier").error("%s", exc, exc_info=args.verbose)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
