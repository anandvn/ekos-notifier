"""``ekos-notifier doctor``: check that everything Ekos Notifier needs is there and reachable,
and say how to fix what isn't.

Read-only, like the rest of Ekos Notifier: it sends no notification, writes nothing, and only
asks Ekos, INDI and PHD2 the questions the service asks. Each area gives one or more results:
``ok``, ``note`` (worth knowing, nothing wrong), ``warn`` (it works, but not fully) or ``FAIL``
(it can't work until fixed). The exit status is 1 when anything failed.

Every check takes what it reads as arguments with real defaults, so the tests can run them
against fakes.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import sqlite3
import sys
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from ekos_notifier import __version__
from ekos_notifier.i18n import N_, _, ngettext

OK, NOTE, WARN, FAIL = "ok", "note", "warn", "FAIL"
LABELS = {OK: N_("ok"), NOTE: N_("note"), WARN: N_("warn"), FAIL: N_("FAIL")}  # as shown
TIMEOUT = 3.0  # seconds for each connection
INSTALLER = (
    "curl -fsSL https://raw.githubusercontent.com/anandvn/ekos-notifier/main/install.sh | sh"
)


@dataclass(frozen=True)
class Check:
    status: str  # ok | note | warn | FAIL
    text: str
    fix: str | None = None


def _home(path: str | Path) -> str:
    """The path with the home folder as ~, to keep lines short."""
    text, home = str(path), str(Path.home())
    return "~" + text[len(home) :] if text == home or text.startswith(home + "/") else text


# -- install ------------------------------------------------------------------------------


def venv_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    return Path(base) / "ekos-notifier" / "venv"


def check_install(
    which: Callable[[str], str | None] = shutil.which, venv: Path | None = None
) -> list[Check]:
    venv = venv or venv_dir()
    python = "{}.{}".format(*sys.version_info[:2])
    out = [
        Check(
            OK,
            _("Ekos Notifier {version} on Python {python} ({path})").format(
                version=__version__, python=python, path=_home(sys.prefix)
            ),
        )
    ]
    if venv.exists() and not (venv / "bin" / "python").exists():
        # The venv's python is a link to the system's; an OS update to a new Python breaks it.
        out.append(
            Check(
                FAIL,
                _(
                    "the installer's venv ({path}) is broken: the Python it was built on "
                    "is gone (an OS update?)"
                ).format(path=_home(venv)),
                _("run the installer again; it rebuilds the venv: {installer}").format(
                    installer=INSTALLER
                ),
            )
        )
    if which("ekos-notifier") is None:
        rc = "~/.zshrc" if os.environ.get("SHELL", "").endswith("zsh") else "~/.bashrc"
        out.append(
            Check(
                NOTE,
                _("'ekos-notifier' isn't on your PATH (the service doesn't need it)"),
                _("{command}, then open a new terminal").format(
                    command=f"echo 'export PATH=\"$HOME/.local/bin:$PATH\"' >> {rc}"
                ),
            )
        )
    return out


# -- config -------------------------------------------------------------------------------


def check_config(path: str | None = None) -> tuple[object | None, list[Check]]:
    """The loaded config (None if it has errors) and the results."""
    import apprise

    from ekos_notifier import config

    try:
        cfg = config.load(path)
    except config.ConfigError as exc:
        return None, [
            Check(
                FAIL,
                f"{exc}",
                _(
                    "fix it (`ekos-notifier config check` explains); until then the "
                    "service records the night but sends nothing"
                ),
            )
        ]
    if cfg.path is None:
        return cfg, [
            Check(
                WARN,
                _("no config file: Ekos Notifier records the night but sends nothing"),
                _("{command}, then put your notifier under notify:").format(
                    command="ekos-notifier config init"
                ),
            )
        ]
    out = [Check(OK, _home(cfg.path))]
    out += [Check(NOTE, warning) for warning in cfg.warnings]
    if not cfg.notify:
        out.append(
            Check(
                WARN,
                _("no notifier: the night is recorded, but nothing is sent"),
                _("put an Apprise URL (e.g. pover://USER_KEY@APP_TOKEN) under notify:"),
            )
        )
    good = []
    for url in cfg.notify:
        if apprise.Apprise().add(url):
            good.append(url)
        else:
            out.append(
                Check(
                    FAIL,
                    _("notify: not a valid Apprise URL: {scheme}://…").format(
                        scheme=url.split(":", 1)[0]
                    ),
                    _("see https://github.com/caronc/apprise/wiki for the format"),
                )
            )
    if good:
        from ekos_notifier.cli import _private

        names = ", ".join(_private(url).split("?")[0].rstrip("/") for url in good)
        out.append(Check(OK, _("notify: {names}").format(names=names)))
        out.append(Check(NOTE, _("doctor sends nothing: `ekos-notifier test-notify` sends a test")))
    try:
        mode = cfg.path.stat().st_mode
    except OSError:
        mode = 0
    if cfg.notify and mode & 0o077:
        out.append(
            Check(
                WARN,
                _("the config holds your notifier keys and other users can read it"),
                f"chmod 600 {_home(cfg.path)}",
            )
        )
    return cfg, out


# -- service ------------------------------------------------------------------------------

Runner = Callable[[Sequence[str]], "object"]


def _exec_start(unit: str) -> tuple[str | None, str | None]:
    """The unit's Python and --config, from its ExecStart line."""
    for line in unit.splitlines():
        if line.startswith("ExecStart="):
            words = shlex.split(line.partition("=")[2])
            config = words[words.index("--config") + 1] if "--config" in words[:-1] else None
            return (words[0] if words else None), config
    return None, None


def check_service(
    db: Path,
    runner: Runner | None = None,
    unit_dir: Path | None = None,
    running_pid: Callable[[Path], int | None] | None = None,
) -> list[Check]:
    from ekos_notifier import lock, systemd

    runner = runner or systemd.run
    unit_dir = unit_dir or systemd.unit_dir()
    if running_pid is None:

        def running_pid(db: Path) -> int | None:
            # Only with a lock file there: taking the lock would create one, and doctor writes
            # nothing.
            return lock.running_pid(db) if lock.lock_path(db).exists() else None

    path = unit_dir / systemd.UNIT
    pid = running_pid(db)
    if not path.exists():
        out = [
            Check(
                WARN,
                _("not installed: Ekos Notifier runs only while you run it yourself"),
                "ekos-notifier install-service " + _("(starts it with your session)"),
            )
        ]
        if pid:
            out.append(Check(NOTE, _("running in a terminal (PID {pid})").format(pid=pid)))
        return out
    try:
        text = path.read_text()
    except OSError as exc:
        return [Check(FAIL, _("can't read {path}: {exc}").format(path=_home(path), exc=exc))]
    out = []
    python, config = _exec_start(text)
    if python is None or not Path(python).exists():
        out.append(
            Check(
                FAIL,
                _(
                    "the service's Python ({python}) is gone: it can't start (an OS update "
                    "moved Python on?)"
                ).format(python=python or "none"),
                _("run the installer again: {installer}").format(installer=INSTALLER),
            )
        )
    elif text != systemd.unit_text(python=python, config=config):
        out.append(
            Check(
                WARN,
                _("the service file is from an older version (e.g. no `reload`)"),
                "ekos-notifier install-service",
            )
        )
    try:
        state = getattr(runner(["systemctl", "--user", "is-active", systemd.UNIT]), "stdout", "")
        service_pid = systemd.service_pid(runner)
    except OSError:  # no systemctl
        return [*out, Check(NOTE, _("couldn't ask systemd whether it's running"))]
    state = (state or "").strip() or "unknown"
    if state == "active":
        text = _("running (PID {pid})").format(pid=service_pid) if service_pid else _("running")
        out.append(Check(OK, text))
    elif state == "failed":
        out.append(
            Check(
                FAIL,
                _("it failed and isn't running"),
                _(
                    "see why: journalctl --user -u ekos-notifier -n 50; then: systemctl "
                    "--user restart ekos-notifier"
                ),
            )
        )
    else:
        out.append(
            Check(
                WARN,
                _("installed but not running ({state})").format(state=state),
                "systemctl --user start ekos-notifier",
            )
        )
    if pid and pid != service_pid:
        out.append(
            Check(
                NOTE,
                _("another copy is running (PID {pid}), probably in a terminal").format(pid=pid),
            )
        )
    return out


# -- database -----------------------------------------------------------------------------


def check_database(db: Path) -> list[Check]:
    from ekos_notifier.store import MIGRATIONS

    if not db.exists():
        return [
            Check(
                NOTE,
                _("{path}: not there yet (made when the service first runs)").format(
                    path=_home(db)
                ),
            )
        ]
    try:
        con = sqlite3.connect(f"file:{quote(str(db))}?mode=ro", uri=True, timeout=2)
        try:
            version = con.execute("PRAGMA user_version").fetchone()[0]
            if version > len(MIGRATIONS):
                return [
                    Check(
                        FAIL,
                        _(
                            "{path} is from a newer Ekos Notifier (schema {version}; this one "
                            "knows up to {count})"
                        ).format(path=_home(db), version=version, count=len(MIGRATIONS)),
                        _("upgrade Ekos Notifier: {installer}").format(installer=INSTALLER),
                    )
                ]
            sessions, last = (0, None)
            if version >= 2:
                sessions, last = con.execute(
                    "SELECT count(*), max(night) FROM sessions WHERE started_at IS NOT NULL"
                ).fetchone()
        finally:
            con.close()
    except sqlite3.Error as exc:
        return [Check(FAIL, _("{path} can't be read: {exc}").format(path=_home(db), exc=exc))]
    size = db.stat().st_size / 1e6
    if last:
        nights = ngettext(
            "{n} session, the last on the night of {night}",
            "{n} sessions, the last on the night of {night}",
            sessions,
        ).format(n=sessions, night=last)
    else:
        nights = ngettext("{n} session", "{n} sessions", sessions).format(n=sessions)
    return [Check(OK, f"{_home(db)} ({size:.1f} MB): {nights}")]


# -- D-Bus and KStars ---------------------------------------------------------------------


@dataclass
class EkosState:
    kstars: bool = False  # KStars is on a reachable session bus
    ekos: str | None = None  # Ekos's status (idle, pending, success, error), None if stopped


async def check_dbus(find: Callable[[], list] | None = None) -> tuple[EkosState, list[Check]]:
    from dbus_fast import Message, MessageType
    from dbus_fast.aio import MessageBus

    from ekos_notifier.ekos import states
    from ekos_notifier.ekos.bus import candidates
    from ekos_notifier.ekos.decode import EKOS_PATH, KSTARS_NAME

    find = find or candidates
    found = find()
    if not found:
        return EkosState(), [
            Check(
                FAIL,
                _(
                    "no session bus found (no DBUS_SESSION_BUS_ADDRESS, "
                    "/run/user/<uid>/bus or KStars process)"
                ),
                _("run Ekos Notifier as the user who runs KStars, on the same computer"),
            )
        ]

    async def call(bus, dest, path, iface, member, sig, body):
        msg = Message(
            destination=dest, path=path, interface=iface, member=member, signature=sig, body=body
        )
        try:
            reply = await asyncio.wait_for(bus.call(msg), TIMEOUT)
        except Exception:  # noqa: BLE001 - any failure (a timeout included) is no answer
            return None
        if reply is None or reply.message_type == MessageType.ERROR or not reply.body:
            return None
        body = reply.body[0]
        return getattr(body, "value", body)  # a Variant from Properties.Get

    reached, errors = [], []
    for cand in found:
        try:
            bus = await asyncio.wait_for(MessageBus(bus_address=cand.address).connect(), TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - report it, try the next
            errors.append(f"{cand.source}: {str(exc) or type(exc).__name__}")
            continue
        try:
            has = await call(
                bus,
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "NameHasOwner",
                "s",
                [KSTARS_NAME],
            )
            if not has:
                reached.append(cand.source)
                continue
            status = await call(
                bus,
                KSTARS_NAME,
                EKOS_PATH,
                "org.freedesktop.DBus.Properties",
                "Get",
                "ss",
                [f"{KSTARS_NAME}.Ekos", "ekosStatus"],
            )
        finally:
            bus.disconnect()
        ekos = states.COMMUNICATION.get(status) if isinstance(status, int) else None
        out = [Check(OK, _("KStars found on D-Bus ({source})").format(source=cand.source))]
        if ekos == "success":
            out.append(Check(OK, _("Ekos is started and connected to its devices")))
        elif ekos == "error":
            out.append(
                Check(WARN, _("Ekos is started but couldn't connect its devices (see Ekos's log)"))
            )
        elif ekos is not None:
            out.append(
                Check(
                    NOTE,
                    _("Ekos is {ekos}: start it to check INDI and PHD2 fully").format(ekos=ekos),
                )
            )
        else:
            out.append(Check(NOTE, _("Ekos isn't started: start it to check INDI and PHD2 fully")))
        return EkosState(True, ekos), out
    if reached:
        return EkosState(), [
            Check(
                WARN,
                _("KStars isn't running (looked on the {places})").format(
                    places=", ".join(reached)
                ),
                _("start KStars, then run doctor again; Ekos Notifier waits for it by itself"),
            )
        ]
    return EkosState(), [
        Check(
            FAIL,
            _("no session bus could be reached: {errors}").format(errors="; ".join(errors)),
            _("run Ekos Notifier as the user who runs KStars, on the same computer"),
        )
    ]


def check_kstars(processes: Callable[[], list] | None = None) -> list[Check]:
    """How KStars runs (native or Flatpak) and whether a Flatpak shares what's needed."""
    from ekos_notifier.ekos.bus import kstars_processes
    from ekos_notifier.ekos.settings import parse_kconfig

    procs = (processes or kstars_processes)()
    if not procs:
        return []
    out = [*check_language(procs[0]), *check_numbers(procs[0])]
    if len(procs) > 1:
        pids = ", ".join(str(k.pid) for k in procs)
        out.append(
            Check(
                WARN,
                _(
                    "{count} copies of KStars are running (PIDs {pids}): Ekos Notifier can't "
                    "tell whose settings to read"
                ).format(count=len(procs), pids=pids),
                _("close all but one"),
            )
        )
    for k in procs:
        try:
            info = parse_kconfig((k.root / ".flatpak-info").read_text(errors="replace"))
        except OSError:
            out.append(Check(OK, _("KStars (PID {pid}) runs natively").format(pid=k.pid)))
            continue
        context = info.get("Context", {})

        def has(key: str, value: str, context=context) -> bool:
            return value in context.get(key, "").split(";")

        missing = []
        if not has("shared", "network"):
            missing.append(_("network (to reach INDI and PHD2)"))
        if not has("sockets", "session-bus"):
            missing.append(_("the session bus (to follow Ekos)"))
        if missing:
            out.append(
                Check(
                    FAIL,
                    _("KStars (PID {pid}) is a Flatpak without {missing}").format(
                        pid=k.pid, missing=_(" or ").join(missing)
                    ),
                    _("{command}, then restart KStars").format(
                        command="flatpak override --user --share=network --socket=session-bus"
                        " org.kde.kstars"
                    ),
                )
            )
        else:
            out.append(
                Check(
                    OK,
                    _("KStars (PID {pid}) is a Flatpak sharing network and D-Bus").format(
                        pid=k.pid
                    ),
                )
            )
        if not (has("filesystems", "host") or has("filesystems", "home")):
            out.append(
                Check(
                    NOTE,
                    _(
                        "the KStars Flatpak can't see your home folder: Disk low may check the "
                        "wrong disk"
                    ),
                )
            )
    return out


def check_numbers(k) -> list[Check]:
    """KStars writes some numbers (exposure times…) in its locale: this computer must know it."""
    from ekos_notifier.ekos import translation

    name = translation.numeric_locale(k.environ)
    fmt = translation.number_format(name)
    if fmt is None:
        return [
            Check(
                WARN,
                _(
                    "KStars writes numbers in '{name}', which this computer doesn't have: "
                    "exposure times and other localized numbers in Ekos's log are left "
                    "unread"
                ).format(name=name),
                _(
                    "add the locale: uncomment {locale} in /etc/locale.gen and run `sudo "
                    "locale-gen`, then restart the service"
                ).format(locale=name.split(".")[0]),
            )
        ]
    if fmt in (translation.ENGLISH, translation.C):
        return []
    return [
        Check(
            OK,
            _(
                "KStars writes numbers in '{name}' (decimal '{decimal}'): Ekos's log "
                "is read in that format"
            ).format(name=name, decimal=fmt.decimal),
        )
    ]


def check_language(k) -> list[Check]:
    """KStars in another language: its log is read through its own translations."""
    from ekos_notifier.ekos import translation

    found = translation.find_catalog(k)
    if found is None:
        return []
    language, path = found
    bt = translation.load(path, language)
    if bt is None or not len(bt):
        return [
            Check(
                WARN,
                _(
                    "KStars runs in '{language}', and its translations can't be read: "
                    "alerts that read Ekos's log (Autofocus failed, Plate solve failed…) "
                    "may not fire"
                ).format(language=language),
                _("switch KStars to English (Help > Switch Application Language)"),
            )
        ]
    return [
        Check(
            OK,
            _(
                "KStars runs in '{language}': Ekos's log is read through its "
                "translations ({count} phrases), so alerts work as in English"
            ).format(language=language, count=len(bt)),
        )
    ]


# -- INDI ---------------------------------------------------------------------------------


async def check_indi(
    cfg,
    ekos: EkosState,
    server: Callable[[], tuple[str, int] | None] | None = None,
    collect: Callable[..., Awaitable[list]] | None = None,
) -> list[Check]:
    from ekos_notifier.ekos.settings import INDI_HOST, INDI_PORT, indi_server
    from ekos_notifier.indi import dump
    from ekos_notifier.indi.adapter import recognised
    from ekos_notifier.indi.decode import IndiDecoder
    from ekos_notifier.indi.presets import load_builtin

    host, port = (server or indi_server)() or (INDI_HOST, INDI_PORT)
    if cfg is not None and (cfg.indi_host or cfg.indi_port):
        host, port = cfg.indi_host or INDI_HOST, cfg.indi_port or INDI_PORT
    where = f"{host}:{port}"
    try:
        elements = await (collect or dump.collect)(host, port, TIMEOUT)
    except (OSError, asyncio.TimeoutError):
        if ekos.ekos == "success":
            return [
                Check(
                    FAIL,
                    _("Ekos is running but nothing answers on {where}").format(where=where),
                    _(
                        "if your profile's INDI server is on another computer, set indi: host "
                        "and port in the config"
                    ),
                )
            ]
        return [
            Check(
                NOTE,
                _("no INDI server on {where} (Ekos starts it with your profile)").format(
                    where=where
                ),
            )
        ]
    mappings = cfg.indi_mappings if cfg is not None else ()
    decoder = IndiDecoder(load_builtin(), mappings)
    caps: dict[str, list[str]] = {}
    for el in elements:
        for event in decoder.feed(0.0, el):
            if event.kind == "indi.capabilities":
                caps = event.data["metrics"]
    connected = sum(1 for d in decoder.devices.values() if d.connected)
    devices = f"{len(decoder.devices)} device{'s' if len(decoder.devices) != 1 else ''}"
    if connected != len(decoder.devices):
        devices += f", {connected} connected"
    out = [Check(OK, _("INDI server on {where}: {devices}").format(where=where, devices=devices))]
    if caps:
        out.append(Check(OK, _("recognised: {recognised}").format(recognised=recognised(caps))))
    elif connected:
        out.append(
            Check(
                NOTE,
                _("no power box, weather sensor or camera temperature recognised"),
                _("{command} lists the readings that could be mapped in the config").format(
                    command="ekos-notifier indi-dump"
                ),
            )
        )
    return out


# -- PHD2 ---------------------------------------------------------------------------------


async def phd2_version(host: str, port: int) -> str:
    """PHD2's version, from the Version event it sends on connecting. Raises when unreachable."""
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), TIMEOUT)
    try:
        while True:
            line = await asyncio.wait_for(reader.readline(), TIMEOUT)
            if not line:
                raise ConnectionError("closed")
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("Event") == "Version":
                return str(msg.get("PHDVersion") or "?")
    finally:
        writer.close()


async def check_phd2(
    settings: Callable[[], object] | None = None,
    version: Callable[[str, int], Awaitable[str]] = phd2_version,
) -> list[Check]:
    from ekos_notifier.ekos.settings import guider_settings

    s = (settings or guider_settings)()
    if s.uses_phd2 is False:
        name = {"internal": _("its internal guider"), "lin_guider": "Lin Guider"}.get(s.guider)
        return [
            Check(OK, _("Ekos uses {guider}: PHD2 isn't needed").format(guider=name or s.guider))
        ]
    where = f"{s.phd2_host}:{s.phd2_port}"
    try:
        found = await version(s.phd2_host, s.phd2_port)
    except (OSError, asyncio.TimeoutError):
        if s.uses_phd2:
            return [
                Check(
                    WARN,
                    _("Ekos is set to guide with PHD2, but nothing answers on {where}").format(
                        where=where
                    ),
                    _(
                        "start PHD2, with Tools > Enable Server on; Ekos Notifier connects by "
                        "itself once it's up"
                    ),
                )
            ]
        return [
            Check(
                NOTE,
                _("PHD2 isn't running on {where} (Ekos's guider is unknown)").format(where=where),
            )
        ]
    return [Check(OK, _("PHD2 {found} on {where}").format(found=found, where=where))]


# -- clock --------------------------------------------------------------------------------


def check_clock(runner: Runner | None = None) -> list[Check]:
    from ekos_notifier import systemd

    runner = runner or systemd.run
    try:
        result = runner(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    except OSError:
        return [Check(NOTE, _("couldn't check the clock (no timedatectl)"))]
    answer = (getattr(result, "stdout", "") or "").strip()
    if answer == "yes":
        return [Check(OK, _("the clock is synchronised"))]
    if answer == "no":
        return [
            Check(
                WARN,
                _(
                    "the clock isn't synchronised: alert times, twilight and the meridian "
                    "checks depend on it"
                ),
                _(
                    "connect to the internet for a minute, or use a GPS (gpsd with chrony) "
                    "at a dark site"
                ),
            )
        ]
    return [Check(NOTE, _("couldn't check the clock"))]


# -- disk ---------------------------------------------------------------------------------


def last_frame_folder(db: Path) -> str | None:
    """The folder of the last frame Ekos saved, from the database."""
    if not db.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{quote(str(db))}?mode=ro", uri=True, timeout=2)
        try:
            row = con.execute(
                "SELECT file FROM frames WHERE file IS NOT NULL AND file != '' "
                "ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return str(Path(row[0]).parent) if row else None


def check_disk(
    cfg,
    db: Path,
    free: Callable[[str], int | None] | None = None,
    folder: str | None = None,
) -> list[Check]:
    from ekos_notifier.disk import GB, free_bytes

    free = free or free_bytes
    frames = folder or last_frame_folder(db)
    places: Iterable[tuple[str, str]] = (
        [(frames, _("where Ekos saved its last frame"))]
        if frames
        else [(str(Path.home()), _("the home folder (no frames recorded yet)"))]
    )
    low = cfg.disk_low_gb if cfg is not None else 10.0
    out = []
    for path, what in places:
        n = free(path)
        if n is None:
            out.append(
                Check(NOTE, _("couldn't check the free space in {path}").format(path=_home(path)))
            )
        elif low > 0 and n < low * GB:
            out.append(
                Check(
                    WARN,
                    _("{n} GB free in {path}, {what}: under the {low} GB alert").format(
                        n=f"{n / GB:.1f}", path=_home(path), what=what, low=f"{low:g}"
                    ),
                    _("move finished frames off before the night"),
                )
            )
        else:
            out.append(
                Check(
                    OK,
                    _("{n} GB free in {path}, {what}").format(
                        n=f"{n / GB:.0f}", path=_home(path), what=what
                    ),
                )
            )
    return out


# -- all of it ----------------------------------------------------------------------------


async def run(config_path: str | None, db: Path, say: Callable[[str], None] = print) -> int:
    """Run every check, printing each area as it's done. Returns the exit status."""
    results: list[Check] = []

    def show(area: str, checks: list[Check]) -> None:
        if not checks:
            return
        say(_(area))
        for c in checks:
            say(f"  {_(LABELS[c.status]):<5} {c.text}")
            if c.fix:
                say(f"        → {c.fix}")
        results.extend(checks)

    say(_("Ekos Notifier doctor ({version})").format(version=__version__))
    say("")
    show(N_("Install"), check_install())
    cfg, checks = check_config(config_path)
    show(N_("Config"), checks)
    show(N_("Service"), check_service(db))
    show(N_("Database"), check_database(db))
    ekos, checks = await check_dbus()
    show(N_("KStars and Ekos"), checks + check_kstars())
    show(N_("INDI"), await check_indi(cfg, ekos))
    show(N_("PHD2"), await check_phd2())
    show(N_("Clock"), check_clock())
    show(N_("Disk"), check_disk(cfg, db))
    failed = sum(c.status == FAIL for c in results)
    warned = sum(c.status == WARN for c in results)
    say("")
    if not failed and not warned:
        say(_("All good."))
    else:
        problems = ngettext("{n} problem", "{n} problems", failed).format(n=failed)
        warnings = ngettext("{n} warning", "{n} warnings", warned).format(n=warned)
        if failed and warned:
            say(
                _("{problems} and {warnings}: the → lines say what to do.").format(
                    problems=problems, warnings=warnings
                )
            )
        else:
            say(
                _("{problems}: the → lines say what to do.").format(
                    problems=problems if failed else warnings
                )
            )
    return 1 if failed else 0
