"""Command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

from ekos_notifier import __version__

# Distinct from argparse's usage-error code (2) so scripts can tell them apart.
EXIT_NOT_IMPLEMENTED = 3
EXIT_FATAL = 78  # EX_CONFIG: an error retrying won't fix; the unit doesn't restart on it

PLANNED = {
    "doctor": "check D-Bus, PHD2, INDI, clock, disk, notifier and sync, and suggest fixes",
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

    dump = sub.add_parser(
        "indi-dump",
        help="write your INDI devices' properties to a file, for a device-support request",
        description="Connect to the INDI server (read-only), write every device's properties to "
        "a file with the site location removed, and list what Ekos Notifier recognises and the "
        "readings that could be mapped in the config. Connect your equipment in Ekos first.",
    )
    dump.add_argument("-o", "--output", metavar="FILE", default="indi-dump.xml")
    dump.add_argument("--host", help="INDI server (default: the active Ekos profile's)")
    dump.add_argument("--port", type=int, help="INDI port (default: the profile's, or 7624)")
    dump.add_argument("--seconds", type=float, default=5.0, help="how long to listen (default 5)")
    dump.add_argument("--config", metavar="PATH", help=CONFIG_HELP)

    svc = sub.add_parser(
        "install-service",
        help="run Ekos Notifier as a systemd user service (starts with your session)",
        description="Install, enable and start a systemd user service running 'ekos-notifier "
        "run' with this installation, so it starts with your session and restarts if it fails. "
        "Logs: journalctl --user -u ekos-notifier.",
    )
    svc.add_argument("--no-start", action="store_true", help="enable it, but don't start it now")
    svc.add_argument("--uninstall", action="store_true", help="stop, disable and remove it")
    svc.add_argument("--config", metavar="PATH", help="config file for the service to use")

    sim = sub.add_parser(
        "simulate",
        help="replay a night to preview the alerts it would send",
        description="Replay a night through the alert rules with your config and show every "
        "alert it would send, to tune alerts in daylight. With no arguments, the sample night "
        "shipped with Ekos Notifier; or a recording file; or one of your own sessions from the "
        "database (read-only). Nothing is written, and nothing is sent unless you ask.",
    )
    sim.add_argument("recording", nargs="?", help="a recording (.ndjson.gz) to replay")
    sim.add_argument(
        "--session", metavar="ID", help="replay one of your sessions: 'last' or an id from --list"
    )
    sim.add_argument("--list", action="store_true", help="list your recent sessions")
    sim.add_argument(
        "--db",
        metavar="PATH",
        help="database file (default: ~/.local/share/ekos-notifier/ekos-notifier.db)",
    )
    sim.add_argument("--config", metavar="PATH", help=CONFIG_HELP)
    sim.add_argument("--brief", action="store_true", help="titles only")
    sim.add_argument(
        "--send",
        action="store_true",
        help="also send the alerts that would go out at once, marked [Simulated] "
        "(an Emergency is sent as High)",
    )

    for name, help_text in PLANNED.items():
        sub.add_parser(name, help=f"{help_text} (not yet implemented)")

    config = sub.add_parser("config", help="work with the config file")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    check = config_sub.add_parser("check", help="validate the config file and explain any errors")
    check.add_argument("--config", metavar="PATH", help=CONFIG_HELP)
    listing = config_sub.add_parser(
        "alerts",
        help="list every alert, its default and your setting (messages: in the config)",
    )
    listing.add_argument("--config", metavar="PATH", help=CONFIG_HELP)
    init = config_sub.add_parser(
        "init", help="write the commented example config, if there's no config yet"
    )
    init.add_argument("--config", metavar="PATH", help=CONFIG_HELP)
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
    if args.command == "config" and args.config_command == "alerts":
        return cmd_config_alerts(args)
    if args.command == "config" and args.config_command == "init":
        return cmd_config_init(args)
    if args.command == "config":
        return cmd_config_check(args)
    if args.command == "indi-dump":
        return cmd_indi_dump(args)
    if args.command == "install-service":
        return cmd_install_service(args)
    if args.command == "simulate":
        return cmd_simulate(args)
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
    from ekos_notifier.lock import AlreadyRunning
    from ekos_notifier.service import serve
    from ekos_notifier.store import default_path

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from pathlib import Path

    from ekos_notifier import config as config_module
    from ekos_notifier.store import IncompatibleDatabase

    logger = logging.getLogger("ekos_notifier")
    try:
        cfg = config_module.load(args.config)
    except config_module.ConfigError as exc:
        path = Path(args.config) if args.config else config_module.default_path()
        if not path.exists():  # `--config` naming a file that isn't there: a typo, say so
            print(f"Config error: {exc}", file=sys.stderr)
            return 1
        # A mistake in the file mustn't cost the night: record it, send nothing, and pick up
        # the file as soon as it's fixed (the config watcher).
        logger.error(
            "Config error: %s. Running with recording only, no alerts, until it's fixed: "
            "fix the file (ekos-notifier config check) and it's picked up by itself.",
            exc,
        )
        cfg = config_module.Config(path=path)
    try:
        asyncio.run(serve(args.db or default_path(), config=cfg, watch_config=True))
    except AlreadyRunning as exc:
        logger.error("%s", exc)
        return 1
    except IncompatibleDatabase as exc:
        logger.error("%s", exc)
        return EXIT_FATAL
    except Exception as exc:  # noqa: BLE001 - report, don't dump a traceback on users
        logger.error("%s", exc, exc_info=args.verbose)
        return 1
    return 0


def cmd_config_alerts(args: argparse.Namespace) -> int:
    from ekos_notifier.messages import ALERTS, LEVELS

    cfg = _load_config(args.config)
    if cfg is None:
        return 1
    print(f"{'alert':<28} {'default':<10} {'yours':<10} what")
    for name, alert in ALERTS.items():
        level = cfg.messages.level(name)
        yours = level if level != alert.default else ""
        print(f"{name:<28} {alert.default:<10} {yours:<10} {alert.what}")
    print()
    print(f"Set any of them under 'messages:' in the config: {' | '.join(LEVELS)}.")
    print("off: never sent or listed; digest: listed in the hourly digest only;")
    print("quiet: sent silently; normal, high, emergency: sent at once at that priority.")
    return 0


def cmd_config_init(args: argparse.Namespace) -> int:
    from ekos_notifier import config

    path, written = config.init(args.config)
    if not written:
        print(f"{path} already exists: left as it is.")
        return 0
    print(f"Wrote {path} (readable only by you).")
    print("Add your notifier under notify:, then run `ekos-notifier config check`.")
    return 0


def cmd_config_check(args: argparse.Namespace) -> int:
    import apprise

    cfg = _load_config(args.config)
    if cfg is None:
        return 1
    if cfg.path is None:
        print("No config file: Ekos Notifier records only and sends nothing.")
        print("Run `ekos-notifier config init` to write a commented one to fill in.")
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
    if cfg.dew_margin_c > 0:
        print(f"  dew risk: margin under {cfg.dew_margin_c:g} °C or RH over 90% for 5 min")
    else:
        print("  dew risk: off (dew_margin_c is 0)")
    if cfg.cooler_tolerance_c > 0:
        print(f"  cooler: alert {cfg.cooler_tolerance_c:g} °C over the setpoint")
    else:
        print("  cooler: off (cooler_tolerance_c is 0)")
    if cfg.disk_low_gb > 0:
        print(f"  disk: alert under {cfg.disk_low_gb:g} GB free where Ekos saves frames")
    else:
        print("  disk: off (disk_low_gb is 0)")
    if cfg.low_volts is None and cfg.critical_volts is None:
        print(f"  battery: {cfg.battery}, voltage alerts off")
    else:
        levels = [
            f"{name} under {v:g} V"
            for name, v in (("low", cfg.low_volts), ("critical", cfg.critical_volts))
            if v is not None
        ]
        print(f"  battery: {cfg.battery}, {', '.join(levels)}")
    if "preset" in (cfg.raw.get("power") or {}):
        print("  note: 'power.preset' isn't used: power boxes are recognised automatically")
    for name, level in cfg.messages.levels.items():
        print(f"  message {name}: {level}")
    for name in cfg.messages.lowered_emergencies():
        level = cfg.messages.level(name)
        print(f"  note: {name} is an Emergency by default; you've set it to {level}")
    if cfg.indi_host or cfg.indi_port:
        print(f"  indi: {cfg.indi_host or 'localhost'}:{cfg.indi_port or 7624} (set in config)")
    else:
        print("  indi: the active Ekos profile's server")
    for m in cfg.indi_mappings:
        print(f"  indi metric: {m.metric} <- {m.device} / {m.prop}.{m.element}")
    return 1 if problems else 0


def cmd_indi_dump(args: argparse.Namespace) -> int:
    from pathlib import Path

    from ekos_notifier.ekos.settings import INDI_HOST, INDI_PORT, indi_server
    from ekos_notifier.indi import dump
    from ekos_notifier.indi.presets import load_builtin

    cfg = _load_config(args.config)
    if cfg is None:
        return 1
    found = indi_server() or (INDI_HOST, INDI_PORT)
    host = args.host or cfg.indi_host or found[0]
    port = args.port or cfg.indi_port or found[1]
    try:
        elements = asyncio.run(dump.collect(host, port, args.seconds))
    except (OSError, asyncio.TimeoutError) as exc:
        print(f"Can't reach the INDI server on {host}:{port} ({exc}).", file=sys.stderr)
        print("Start Ekos with your equipment profile first.", file=sys.stderr)
        return 1
    if not elements:
        print(f"The INDI server on {host}:{port} sent no properties.", file=sys.stderr)
        return 1
    out = Path(args.output)
    dump.write(dump.scrub(elements), out)
    for line in dump.summary(elements, load_builtin()):
        print(line)
    print(f"\nWrote {len(elements)} properties to {out} (site location removed).")
    print("Attach it to a 'Device support' issue, or map a candidate in the config's indi.metrics.")
    return 0


def cmd_install_service(args: argparse.Namespace) -> int:
    from pathlib import Path

    from ekos_notifier import systemd
    from ekos_notifier.lock import running_pid
    from ekos_notifier.store import default_path

    try:
        if args.uninstall:
            lines = systemd.uninstall()
        else:
            config = str(Path(args.config).resolve()) if args.config else None
            start = not args.no_start
            pid = running_pid(default_path())
            if start and pid and pid == systemd.service_pid():
                # The running copy is the service itself (re-installing, or an upgrade):
                # restart it on the new unit and code rather than warning about it.
                lines = systemd.install(config=config, restart=True)
            elif start and pid:
                print(
                    f"Ekos Notifier is already running (PID {pid}), probably in a terminal: "
                    "the service will be enabled but not started. Stop that one, then run: "
                    "systemctl --user start ekos-notifier"
                )
                lines = systemd.install(start=False, config=config)
            else:
                lines = systemd.install(start=start, config=config)
    except (OSError, RuntimeError) as exc:
        print(f"install-service: {exc}", file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    from ekos_notifier import simulate
    from ekos_notifier.notify import send_apprise
    from ekos_notifier.service import build_rules
    from ekos_notifier.store import default_path

    cfg = _load_config(args.config)
    if cfg is None:
        return 1
    db = args.db or default_path()
    try:
        if args.list:
            rows = simulate.sessions(db)
            if not rows:
                print("No imaging sessions in the database yet.")
            for r in rows:
                start = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["started_at"]))
                end = (
                    time.strftime("%H:%M", time.localtime(r["ended_at"])) if r["ended_at"] else "…"
                )
                why = r["end_reason"] or r["state"]
                print(f"{r['id'][:8]}  night of {r['night']}  {start}–{end}  {why}")
            return 0
        if args.session and args.recording:
            print("Give a recording or --session, not both.", file=sys.stderr)
            return 2
        if args.session:
            night = simulate.session(db, args.session)
        elif args.recording:
            night = simulate.recording(args.recording)
        else:
            night = simulate.sample()
    except simulate.SimulateError as exc:
        print(f"simulate: {exc}", file=sys.stderr)
        return 1
    alerts = simulate.run(night, build_rules(cfg, live=False))
    print("\n".join(simulate.report(night, alerts, brief=args.brief)))
    if args.send:
        if not cfg.notify:
            print("Nothing sent: no notifiers in the config.", file=sys.stderr)
            return 1
        errors = simulate.send(alerts, cfg.notify, send_apprise)
        for error in errors:
            print(f"FAILED: {error}", file=sys.stderr)
        return 1 if errors else 0
    return 0


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
