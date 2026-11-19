"""Command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

from ekos_notifier import __version__, i18n
from ekos_notifier.i18n import N_, _

# Distinct from argparse's usage-error code (2) so scripts can tell them apart.
EXIT_NOT_IMPLEMENTED = 3
EXIT_FATAL = 78  # EX_CONFIG: an error retrying won't fix; the unit doesn't restart on it

CONFIG_HELP = N_("config file (default: ~/.config/ekos-notifier/config.yaml)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ekos-notifier",
        description=_("Alerts, hourly digests and session records for KStars/Ekos rigs."),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser(
        "run",
        help=_("run the monitoring service in the foreground"),
        description=_(
            "Run the monitoring service in the foreground until Ctrl-C or SIGTERM. "
            "The systemd service runs this; run it by hand to watch what Ekos "
            "Notifier sees."
        ),
    )
    run.add_argument(
        "--db",
        metavar="PATH",
        help=_("database file (default: ~/.local/share/ekos-notifier/ekos-notifier.db)"),
    )
    run.add_argument("--config", metavar="PATH", help=_(CONFIG_HELP))
    run.add_argument("-v", "--verbose", action="store_true", help=_("debug logging"))

    test = sub.add_parser(
        "test-notify",
        help=_("send a test message to every notifier in the config"),
        description=_(
            "Send a test message to every notifier in the config and report which "
            "worked. Keys in URLs are hidden in the output."
        ),
    )
    test.add_argument("--config", metavar="PATH", help=_(CONFIG_HELP))
    test.add_argument(
        "--emergency",
        action="store_true",
        help=_("send at Emergency priority (Pushover repeats it until you acknowledge it)"),
    )

    dump = sub.add_parser(
        "indi-dump",
        help=_("write your INDI devices' properties to a file, for a device-support request"),
        description=_(
            "Connect to the INDI server (read-only), write every device's "
            "properties to a file with the site location removed, and list what "
            "Ekos Notifier recognises and the readings that could be mapped in the "
            "config. Connect your equipment in Ekos first."
        ),
    )
    dump.add_argument("-o", "--output", metavar="FILE", default="indi-dump.xml")
    dump.add_argument("--host", help=_("INDI server (default: the active Ekos profile's)"))
    dump.add_argument("--port", type=int, help=_("INDI port (default: the profile's, or 7624)"))
    dump.add_argument(
        "--seconds", type=float, default=5.0, help=_("how long to listen (default 5)")
    )
    dump.add_argument("--config", metavar="PATH", help=_(CONFIG_HELP))

    wiz = sub.add_parser(
        "setup",
        help=_("set up notifications, the battery and the digest, with a few questions"),
        description=_(
            "Set up Ekos Notifier with a few questions in the terminal: it shows "
            "what it can see on the rig, sets up a notifier and sends it a test, "
            "and asks about the battery and the hourly digest. It saves to the "
            "config file only when you say so, keeping its comments and other "
            "settings. Run it again any time to change the answers."
        ),
    )
    wiz.add_argument("--config", metavar="PATH", help=_(CONFIG_HELP))

    doc = sub.add_parser(
        "doctor",
        help=_(
            "check the install, config, service, Ekos, INDI, PHD2, clock and disk; suggest fixes"
        ),
        description=_(
            "Check everything Ekos Notifier needs and say how to fix what isn't "
            "right: the install, the config, the service, the database, KStars and "
            "Ekos on D-Bus (and the KStars Flatpak's permissions), the INDI server "
            "and what's recognised on it, PHD2, the clock and the free disk space. "
            "Read-only: nothing is sent or changed. Start KStars and Ekos with "
            "your equipment first to check everything."
        ),
    )
    doc.add_argument("--config", metavar="PATH", help=_(CONFIG_HELP))
    doc.add_argument(
        "--db",
        metavar="PATH",
        help=_("database file (default: ~/.local/share/ekos-notifier/ekos-notifier.db)"),
    )

    svc = sub.add_parser(
        "install-service",
        help=_("run Ekos Notifier as a systemd user service (starts with your session)"),
        description=_(
            "Install, enable and start a systemd user service running "
            "'ekos-notifier run' with this installation, so it starts with your "
            "session and restarts if it fails. Logs: journalctl --user -u "
            "ekos-notifier."
        ),
    )
    svc.add_argument("--no-start", action="store_true", help=_("enable it, but don't start it now"))
    svc.add_argument("--uninstall", action="store_true", help=_("stop, disable and remove it"))
    svc.add_argument("--config", metavar="PATH", help=_("config file for the service to use"))

    sim = sub.add_parser(
        "simulate",
        help=_("replay a night to preview the alerts it would send"),
        description=_(
            "Replay a night through the alert rules with your config and show "
            "every alert it would send, to tune alerts in daylight. With no "
            "arguments, the sample night shipped with Ekos Notifier; or a "
            "recording file; or one of your own sessions from the database "
            "(read-only). Nothing is written, and nothing is sent unless you ask."
        ),
    )
    sim.add_argument("recording", nargs="?", help=_("a recording (.ndjson.gz) to replay"))
    sim.add_argument(
        "--session",
        metavar="ID",
        help=_("replay one of your sessions: 'last' or an id from --list"),
    )
    sim.add_argument("--list", action="store_true", help=_("list your recent sessions"))
    sim.add_argument(
        "--db",
        metavar="PATH",
        help=_("database file (default: ~/.local/share/ekos-notifier/ekos-notifier.db)"),
    )
    sim.add_argument("--config", metavar="PATH", help=_(CONFIG_HELP))
    sim.add_argument("--brief", action="store_true", help=_("titles only"))
    sim.add_argument(
        "--send",
        action="store_true",
        help=_(
            "also send the alerts that would go out at once, marked [Simulated] "
            "(an Emergency is sent as High)"
        ),
    )

    config = sub.add_parser("config", help=_("work with the config file"))
    config_sub = config.add_subparsers(dest="config_command", required=True)
    check = config_sub.add_parser(
        "check", help=_("validate the config file and explain any errors")
    )
    check.add_argument("--config", metavar="PATH", help=_(CONFIG_HELP))
    listing = config_sub.add_parser(
        "alerts",
        help=_("list every alert, its default and your setting (messages: in the config)"),
    )
    listing.add_argument("--config", metavar="PATH", help=_(CONFIG_HELP))
    init = config_sub.add_parser(
        "init", help=_("write the commented example config, if there's no config yet")
    )
    init.add_argument("--config", metavar="PATH", help=_(CONFIG_HELP))
    return parser


def _language(argv: list[str]) -> None:
    """Speak the config's language (``language:``), or the system's when there's no config or it
    can't be read: set before the parser, so ``--help`` is translated too."""
    from ekos_notifier import config

    path = None
    for i, arg in enumerate(argv):
        if arg == "--config" and i + 1 < len(argv):
            path = argv[i + 1]
        elif arg.startswith("--config="):
            path = arg.split("=", 1)[1]
    try:
        language = config.load(path).language
    except Exception:  # noqa: BLE001 - a broken config is reported by the command itself
        language = "auto"
    i18n.use(language)


def main(argv: list[str] | None = None) -> int:
    _language(sys.argv[1:] if argv is None else argv)
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
    if args.command == "doctor":
        return cmd_doctor(args)
    if args.command == "setup":
        return cmd_setup(args)
    print(f"ekos-notifier {args.command}: not implemented yet (pre-alpha).", file=sys.stderr)
    return EXIT_NOT_IMPLEMENTED


def _load_config(path: str | None):
    from ekos_notifier import config

    try:
        return config.load(path)
    except config.ConfigError as exc:
        print(_("Config error: {exc}").format(exc=exc), file=sys.stderr)
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
            print(_("Config error: {exc}").format(exc=exc), file=sys.stderr)
            return 1
        # A mistake in the file mustn't cost the night: record it, send nothing, and pick up
        # the file as soon as it's fixed (the config watcher).
        logger.error(
            "Config error: %s. Running with recording only, no alerts, until it's fixed: "
            "fix the file (ekos-notifier config check) and it's picked up by itself.",
            exc,
        )
        cfg = config_module.Config(path=path)
    for warning in cfg.warnings:
        logger.warning("config: %s", warning)
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
    print(f"{_('alert'):<28} {_('default'):<10} {_('yours'):<10} {_('what')}")
    for name, alert in ALERTS.items():
        level = cfg.messages.level(name)
        yours = level if level != alert.default else ""
        print(f"{name:<28} {alert.default:<10} {yours:<10} {_(alert.what)}")
    print()
    print(
        _("Set any of them under 'messages:' in the config: {levels}.").format(
            levels=" | ".join(LEVELS)
        )
    )
    print(_("off: never sent or listed; digest: listed in the hourly digest only;"))
    print(_("quiet: sent silently; normal, high, emergency: sent at once at that priority."))
    return 0


def cmd_config_init(args: argparse.Namespace) -> int:
    from ekos_notifier import config

    path, written = config.init(args.config)
    if not written:
        print(_("{path} already exists: left as it is.").format(path=path))
        return 0
    print(_("Wrote {path} (readable only by you).").format(path=path))
    print(_("Add your notifier under notify:, then run `ekos-notifier config check`."))
    return 0


def cmd_config_check(args: argparse.Namespace) -> int:
    import apprise

    cfg = _load_config(args.config)
    if cfg is None:
        return 1
    if cfg.path is None:
        print(_("No config file: Ekos Notifier records only and sends nothing."))
        print(_("Run `ekos-notifier config init` to write a commented one to fill in."))
        return 0
    print(_("{path}: OK").format(path=cfg.path))
    for warning in cfg.warnings:
        print("  " + _("note: {warning}").format(warning=warning))
    problems = 0
    if not cfg.notify:
        print("  " + _("notify: none (recording only)"))
    for url in cfg.notify:
        if apprise.Apprise().add(url):
            print("  " + _("notify: {url}").format(url=_private(url)))
        else:
            problems += 1
            print(
                "  "
                + _("notify: NOT a valid Apprise URL: {scheme}://…").format(
                    scheme=url.split(":", 1)[0]
                ),
                file=sys.stderr,
            )
    if cfg.notify:
        print(
            "  "
            + _("undelivered alerts dropped after {notify_expire_minutes} min").format(
                notify_expire_minutes=f"{cfg.notify_expire_minutes:g}"
            )
        )
    print(
        "  "
        + _("guiding lost: alert after {guiding_recovery_minutes} min without recovery").format(
            guiding_recovery_minutes=f"{cfg.guiding_recovery_minutes:g}"
        )
    )
    if cfg.guiding_poor_minutes > 0:
        print(
            "  "
            + _(
                "guiding poor: alert after {guiding_poor_minutes} min over Ekos's "
                "guide deviation limit"
            ).format(guiding_poor_minutes=f"{cfg.guiding_poor_minutes:g}")
        )
    else:
        print("  " + _("guiding poor: off (guiding_poor_minutes is 0)"))
    if "crier" in cfg.raw and "digest" not in cfg.raw:
        print("  " + _("note: rename 'crier:' to 'digest:' (the old name still works for now)"))
    if cfg.digest_enabled:
        when = _("on the clock") if cfg.digest_align_to_clock else _("from session start")
        print(
            "  "
            + _("digest: every {digest_interval_minutes} min, {when}").format(
                digest_interval_minutes=f"{cfg.digest_interval_minutes:g}", when=when
            )
        )
    else:
        print("  " + _("digest: off"))
    if cfg.dew_margin_c > 0:
        print(
            "  "
            + _("dew risk: margin under {dew_margin_c} °C or RH over 90% for 5 min").format(
                dew_margin_c=f"{cfg.dew_margin_c:g}"
            )
        )
    else:
        print("  " + _("dew risk: off (dew_margin_c is 0)"))
    if cfg.cooler_tolerance_c > 0:
        print(
            "  "
            + _("cooler: alert {cooler_tolerance_c} °C over the setpoint").format(
                cooler_tolerance_c=f"{cfg.cooler_tolerance_c:g}"
            )
        )
    else:
        print("  " + _("cooler: off (cooler_tolerance_c is 0)"))
    if cfg.disk_low_gb > 0:
        print(
            "  "
            + _("disk: alert under {disk_low_gb} GB free where Ekos saves frames").format(
                disk_low_gb=f"{cfg.disk_low_gb:g}"
            )
        )
    else:
        print("  " + _("disk: off (disk_low_gb is 0)"))
    if cfg.low_volts is None and cfg.critical_volts is None:
        print("  " + _("battery: {battery}, voltage alerts off").format(battery=cfg.battery))
    else:
        levels = [
            f"{name} at or under {v:g} V"
            for name, v in (("low", cfg.low_volts), ("critical", cfg.critical_volts))
            if v is not None
        ]
        print(
            "  "
            + _("battery: {battery}, {levels}").format(
                battery=cfg.battery, levels=", ".join(levels)
            )
        )
    for name, level in cfg.messages.levels.items():
        print("  " + _("message {name}: {level}").format(name=name, level=level))
    for name in cfg.messages.lowered_emergencies():
        level = cfg.messages.level(name)
        print(
            "  "
            + _("note: {name} is an Emergency by default; you've set it to {level}").format(
                name=name, level=level
            )
        )
    if cfg.indi_host or cfg.indi_port:
        print(
            "  "
            + _("indi: {host}:{port} (set in config)").format(
                host=cfg.indi_host or "localhost", port=cfg.indi_port or 7624
            )
        )
    else:
        print("  " + _("indi: the active Ekos profile's server"))
    for m in cfg.indi_mappings:
        print(
            "  "
            + _("indi metric: {metric} <- {device} / {prop}.{element}").format(
                metric=m.metric, device=m.device, prop=m.prop, element=m.element
            )
        )
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
        print(
            _("Can't reach the INDI server on {host}:{port} ({exc}).").format(
                host=host, port=port, exc=exc
            ),
            file=sys.stderr,
        )
        print(_("Start Ekos with your equipment profile first."), file=sys.stderr)
        return 1
    if not elements:
        print(
            _("The INDI server on {host}:{port} sent no properties.").format(host=host, port=port),
            file=sys.stderr,
        )
        return 1
    out = Path(args.output)
    dump.write(dump.scrub(elements), out)
    for line in dump.summary(elements, load_builtin()):
        print(line)
    print(
        "\n"
        + _("Wrote {count} properties to {path} (site location removed).").format(
            count=len(elements), path=out
        )
    )
    print(
        _("Attach it to a 'Device support' issue, or map a candidate in the config's indi.metrics.")
    )
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
                    _(
                        "Ekos Notifier is already running (PID {pid}), probably in a terminal: "
                        "the service will be enabled but not started. Stop that one, then run: "
                        "systemctl --user start ekos-notifier"
                    ).format(pid=pid)
                )
                lines = systemd.install(start=False, config=config)
            else:
                lines = systemd.install(start=start, config=config)
    except (OSError, RuntimeError) as exc:
        print(_("install-service: {exc}").format(exc=exc), file=sys.stderr)
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
                print(_("No imaging sessions in the database yet."))
            for r in rows:
                start = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["started_at"]))
                end = (
                    time.strftime("%H:%M", time.localtime(r["ended_at"])) if r["ended_at"] else "…"
                )
                why = r["end_reason"] or r["state"]
                print(
                    _("{id}  night of {night}  {start}–{end}  {why}").format(
                        id=r["id"][:8], night=r["night"], start=start, end=end, why=why
                    )
                )
            return 0
        if args.session and args.recording:
            print(_("Give a recording or --session, not both."), file=sys.stderr)
            return 2
        if args.session:
            night = simulate.session(db, args.session)
        elif args.recording:
            night = simulate.recording(args.recording)
        else:
            night = simulate.sample()
    except simulate.SimulateError as exc:
        print(_("simulate: {exc}").format(exc=exc), file=sys.stderr)
        return 1
    alerts = simulate.run(night, build_rules(cfg, live=False))
    print("\n".join(simulate.report(night, alerts, brief=args.brief)))
    if args.send:
        if not cfg.notify:
            print(_("Nothing sent: no notifiers in the config."), file=sys.stderr)
            return 1
        errors = simulate.send(alerts, cfg.notify, send_apprise)
        for error in errors:
            print(_("FAILED: {error}").format(error=error), file=sys.stderr)
        return 1 if errors else 0
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    from ekos_notifier import wizard

    if not sys.stdin.isatty():
        print("ekos-notifier setup asks questions: run it in a terminal.", file=sys.stderr)
        return 2
    return wizard.run(args.config, wizard.Prompter())


def cmd_doctor(args: argparse.Namespace) -> int:
    from pathlib import Path

    from ekos_notifier import doctor
    from ekos_notifier.store import default_path

    return asyncio.run(doctor.run(args.config, Path(args.db) if args.db else default_path()))


def cmd_test_notify(args: argparse.Namespace) -> int:
    from ekos_notifier.notify import send_apprise

    cfg = _load_config(args.config)
    if cfg is None:
        return 1
    if not cfg.notify:
        print(_("No notifiers in the config ('notify:' is empty or there's no config file)."))
        return 1
    priority = "emergency" if args.emergency else "normal"
    body = _("If you can read this, Ekos Notifier can reach you.")
    if args.emergency:
        body += "\n" + _("This is a test Emergency: acknowledge it to stop the repeats.")
    failures = 0
    for url in cfg.notify:
        error = send_apprise(url, priority, _("Ekos Notifier test"), body)
        if error is None:
            print(_("sent     {url}").format(url=_private(url)))
        else:
            failures += 1
            print(_("FAILED   {url}: {error}").format(url=_private(url), error=error))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
