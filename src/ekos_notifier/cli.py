"""Command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from ekos_notifier import __version__

# Distinct from argparse's usage-error code (2) so scripts can tell them apart.
EXIT_NOT_IMPLEMENTED = 3

PLANNED = {
    "doctor": "check D-Bus, PHD2, INDI, clock, disk, notifier and sync, and suggest fixes",
    "indi-dump": "write all INDI device properties to a file for a device-support request",
    "simulate": "replay a recorded night to preview alerts",
    "install-service": "install the systemd user service",
}

CONFIG_HELP = "config file (default: ~/.config/ekos-notifier/config.yaml)"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ekos-notifier",
        description="Alerts, hourly digests and session records for KStars/Ekos rigs.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser(
        "run",
        help="run the monitoring service in the foreground",
        description="Run the monitoring service in the foreground until Ctrl-C or SIGTERM. "
        "The systemd service runs this; run it by hand to watch what Ekos Notifier sees.",
    )
    run.add_argument(
        "--db",
        metavar="PATH",
        help="database file (default: ~/.local/share/ekos-notifier/ekos-notifier.db)",
    )
    run.add_argument("--config", metavar="PATH", help=CONFIG_HELP)
    run.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    test = sub.add_parser(
        "test-notify",
        help="send a test message to every notifier in the config",
        description="Send a test message to every notifier in the config and report which "
        "worked. Keys in URLs are hidden in the output.",
    )
    test.add_argument("--config", metavar="PATH", help=CONFIG_HELP)
    test.add_argument(
        "--emergency",
        action="store_true",
        help="send at Emergency priority (Pushover repeats it until you acknowledge it)",
    )

    for name, help_text in PLANNED.items():
        sub.add_parser(name, help=f"{help_text} (not yet implemented)")

    config = sub.add_parser("config", help="work with the config file")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    check = config_sub.add_parser("check", help="validate the config file and explain any errors")
    check.add_argument("--config", metavar="PATH", help=CONFIG_HELP)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    from ekos_notifier import legacy

    legacy.migrate(say=lambda msg: print(msg, file=sys.stderr))
    if args.command == "run":
        return cmd_run(args)
    if args.command == "test-notify":
        return cmd_test_notify(args)
    if args.command == "config":
        return cmd_config_check(args)
    print(f"ekos-notifier {args.command}: not implemented yet (pre-alpha).", file=sys.stderr)
    return EXIT_NOT_IMPLEMENTED


def _load_config(path: str | None):
    from ekos_notifier import config

    try:
        return config.load(path)
    except config.ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return None


def _private(url: str) -> str:
    """The URL with keys hidden, as Apprise prints it."""
    import apprise

    ap = apprise.Apprise()
    return ap[0].url(privacy=True) if ap.add(url) else url.split(":", 1)[0] + "://…"


def cmd_run(args: argparse.Namespace) -> int:
    from ekos_notifier.service import serve
    from ekos_notifier.store import default_path

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = _load_config(args.config)
    if cfg is None:
        return 1
    try:
        asyncio.run(serve(args.db or default_path(), config=cfg))
    except Exception as exc:  # noqa: BLE001 - report, don't dump a traceback on users
        logging.getLogger("ekos_notifier").error("%s", exc, exc_info=args.verbose)
        return 1
    return 0


def cmd_config_check(args: argparse.Namespace) -> int:
    import apprise

    cfg = _load_config(args.config)
    if cfg is None:
        return 1
    if cfg.path is None:
        print("No config file: Ekos Notifier records only and sends nothing.")
        print("Copy config.example.yaml to ~/.config/ekos-notifier/config.yaml to set one up.")
        return 0
    print(f"{cfg.path}: OK")
    problems = 0
    if not cfg.notify:
        print("  notify: none (recording only)")
    for url in cfg.notify:
        if apprise.Apprise().add(url):
            print(f"  notify: {_private(url)}")
        else:
            problems += 1
            print(f"  notify: NOT a valid Apprise URL: {url.split(':', 1)[0]}://…", file=sys.stderr)
    if cfg.notify:
        print(f"  undelivered alerts dropped after {cfg.notify_expire_minutes:g} min")
    print(f"  guiding lost: alert after {cfg.guiding_recovery_minutes:g} min without recovery")
    if cfg.guiding_poor_minutes > 0:
        print(
            f"  guiding poor: alert after {cfg.guiding_poor_minutes:g} min over Ekos's guide "
            "deviation limit"
        )
    else:
        print("  guiding poor: off (guiding_poor_minutes is 0)")
    if "crier" in cfg.raw and "digest" not in cfg.raw:
        print("  note: rename 'crier:' to 'digest:' (the old name still works for now)")
    if cfg.digest_enabled:
        when = "on the clock" if cfg.digest_align_to_clock else "from session start"
        print(f"  digest: every {cfg.digest_interval_minutes:g} min, {when}")
    else:
        print("  digest: off")
    return 1 if problems else 0


def cmd_test_notify(args: argparse.Namespace) -> int:
    from ekos_notifier.notify import send_apprise

    cfg = _load_config(args.config)
    if cfg is None:
        return 1
    if not cfg.notify:
        print("No notifiers in the config ('notify:' is empty or there's no config file).")
        return 1
    priority = "emergency" if args.emergency else "normal"
    body = "If you can read this, Ekos Notifier can reach you."
    if args.emergency:
        body += "\nThis is a test Emergency: acknowledge it to stop the repeats."
    failures = 0
    for url in cfg.notify:
        error = send_apprise(url, priority, "Ekos Notifier test", body)
        if error is None:
            print(f"sent     {_private(url)}")
        else:
            failures += 1
            print(f"FAILED   {_private(url)}: {error}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
