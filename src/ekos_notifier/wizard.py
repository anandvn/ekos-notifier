"""``ekos-notifier setup``: a few questions in the terminal, written into the config file.

It shows what it can see on the rig (KStars, INDI devices, PHD2), sets up a notifier and sends
it a test, picks the battery and the hourly digest, then shows what will change and saves only
when you say so. Enter keeps the current answer; Ctrl-C leaves without saving.

The config is edited in place, line by line, so its comments and every other setting stay as
they are: the notifier list, ``power:`` and ``digest:`` are the only lines touched. The result is
read back with the config loader before it's saved; if it doesn't say what was asked (a file
laid out in a way this editor doesn't follow), nothing is written and the lines to add by hand
are shown instead.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from ekos_notifier import config as config_module
from ekos_notifier.i18n import N_, _
from ekos_notifier.power import BATTERIES


class Abort(Exception):
    """The user left (Ctrl-C, or the end of input)."""


class CantEdit(Exception):
    """The file is laid out in a way the line editor doesn't follow (e.g. ``power: {…}``)."""


# -- asking ---------------------------------------------------------------------------------


class Prompter:
    def __init__(
        self, read: Callable[[str], str] = input, say: Callable[[str], None] = print
    ) -> None:
        self._read = read
        self.say = say

    def _ask(self, prompt: str) -> str:
        try:
            return self._read(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            raise Abort from None

    def text(self, question: str, default: str = "") -> str:
        hint = f" [{default}]" if default else ""
        return self._ask(f"{question}{hint}: ") or default

    def required(self, question: str, check: Callable[[str], str | None] | None = None) -> str:
        """An answer that can't be empty; ``check`` returns why one is wrong, or None."""
        while True:
            answer = self._ask(f"{question}: ")
            problem = _("an answer is needed (Ctrl-C to leave)") if not answer else None
            if answer and check:
                problem = check(answer)
            if problem is None:
                return answer
            self.say(f"  {problem}")

    def yes(self, question: str, default: bool = True) -> bool:
        while True:
            answer = self._ask(f"{question} {'[Y/n]' if default else '[y/N]'} ").lower()
            if not answer:
                return default
            if answer in ("y", "yes", "n", "no"):
                return answer.startswith("y")
            self.say("  y or n")

    def choice(
        self, question: str, options: list[tuple[str, str]], default: str, mark: str = "current"
    ) -> str:
        """One of ``options`` (key, label), by number; Enter picks ``default``, labelled
        ``mark`` (none if empty)."""
        self.say(question)
        for i, (key, label) in enumerate(options, start=1):
            mark_it = f" ({mark})" if mark and key == default else ""
            self.say(f"  {i}. {label}{mark_it}")
        number = next(i for i, (key, _) in enumerate(options, start=1) if key == default)
        while True:
            answer = self._ask(
                _("Choose 1-{count} [{default}]: ").format(count=len(options), default=number)
            )
            if not answer:
                return default
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return options[int(answer) - 1][0]
            self.say("  " + _("a number from 1 to {count}").format(count=len(options)))

    def number(self, question: str, default: float, low: float, high: float) -> float:
        while True:
            answer = self._ask(f"{question} [{default:g}]: ")
            if not answer:
                return default
            try:
                value = float(answer)
            except ValueError:
                value = None
            if value is not None and low <= value <= high:
                return value
            self.say(
                "  " + _("a number from {low} to {high}").format(low=f"{low:g}", high=f"{high:g}")
            )


# -- editing the config text ----------------------------------------------------------------

_TOP = re.compile(r"^[^\s#]")  # a top-level line: not indented, not a comment


def _section(lines: list[str], name: str) -> tuple[int, int] | None:
    """(index of ``name:``, index after its block), or None if there's no such section."""
    head = re.compile(rf"^{re.escape(name)}:\s*(#.*)?$")
    for i, line in enumerate(lines):
        if head.match(line):
            end = i + 1
            while end < len(lines) and not _TOP.match(lines[end]):
                end += 1
            # Blank lines and unindented comments at the end introduce the next section.
            while end > i + 1 and (not lines[end - 1].strip() or lines[end - 1].startswith("#")):
                end -= 1
            return i, end
    return None


def _comment(rest: str) -> str:
    """The trailing comment (with its spacing) of a simple value, or ''."""
    m = re.search(r"\s+#.*$", rest)
    return m.group(0) if m else ""


def set_value(text: str, section: str, key: str, value: str) -> str:
    """``section.key: value``: changes the line, uncomments an example, or adds it."""
    lines = text.splitlines()
    found = _section(lines, section)
    if found is None:
        if any(re.match(rf"^{re.escape(section)}:", line) for line in lines):
            raise CantEdit(section)  # there, but not as a block of lines
        tail = "" if not lines or not lines[-1].strip() else "\n"
        return text.rstrip("\n") + f"\n{tail}{section}:\n  {key}: {value}\n"
    start, end = found
    active = re.compile(rf"^(\s+){re.escape(key)}:(.*)$")
    example = re.compile(rf"^(\s+)#\s?{re.escape(key)}:\s*\S*(.*)$")
    for i in range(start + 1, end):
        m = active.match(lines[i])
        if m:
            lines[i] = f"{m.group(1)}{key}: {value}{_comment(m.group(2))}"
            return "\n".join(lines) + "\n"
    for i in range(start + 1, end):
        m = example.match(lines[i])
        if m:
            lines[i] = f"{m.group(1)}{key}: {value}{_comment(m.group(2))}"
            return "\n".join(lines) + "\n"
    lines.insert(start + 1, f"  {key}: {value}")
    return "\n".join(lines) + "\n"


def comment_out(text: str, section: str, key: str) -> str:
    """Turn an active ``section.key`` into a comment (the preset's level applies again)."""
    lines = text.splitlines()
    found = _section(lines, section)
    if found is None:
        return text
    active = re.compile(rf"^(\s+)({re.escape(key)}:.*)$")
    for i in range(found[0] + 1, found[1]):
        m = active.match(lines[i])
        if m:
            lines[i] = f"{m.group(1)}# {m.group(2)}"
    return "\n".join(lines) + "\n"


def _quote(url: str) -> str:
    return '"' + url.replace("\\", "\\\\").replace('"', '\\"') + '"'


def set_notify(text: str, urls: list[str]) -> str:
    """The ``notify:`` list: the given URLs, keeping the commented examples."""
    lines = text.splitlines()
    head = re.compile(r"^notify:(.*)$")
    for i, line in enumerate(lines):
        if head.match(line):
            end = i + 1
            while end < len(lines) and not _TOP.match(lines[end]):
                end += 1
            block = [line for line in lines[i + 1 : end] if not re.match(r"^\s+-", line)]
            items = [f"  - {_quote(url)}" for url in urls]
            lines[i:end] = ["notify:", *items, *block]
            return "\n".join(lines) + "\n"
    items = "".join(f"  - {_quote(url)}\n" for url in urls)
    return f"notify:\n{items}\n" + text


# -- the answers ----------------------------------------------------------------------------


@dataclass
class Answers:
    notify: list[str] = field(default_factory=list)
    tested: set[str] = field(default_factory=set)
    battery: str = "agm_12v"
    low_volts: float | None = None  # None: the preset's level (custom: no such alert)
    critical_volts: float | None = None
    digest_enabled: bool = True
    digest_interval: float = 60.0


def apply(text: str, a: Answers, cfg: config_module.Config | None = None) -> str:
    """The config text with the answers in. With ``cfg`` (what the text says now), only the
    settings that change are touched."""
    cfg = cfg or config_module.Config(notify=["(none)"], digest_enabled=not a.digest_enabled)
    if a.notify != cfg.notify:
        text = set_notify(text, a.notify)
    if (a.battery, levels(a)) != (cfg.battery, (cfg.low_volts, cfg.critical_volts)):
        text = set_value(text, "power", "battery", a.battery)
        for key, value in (("low_volts", a.low_volts), ("critical_volts", a.critical_volts)):
            if a.battery != "mains" and value is not None:
                text = set_value(text, "power", key, f"{value:g}")
            else:
                text = comment_out(text, "power", key)
    if a.digest_enabled != cfg.digest_enabled:
        text = set_value(text, "digest", "enabled", "true" if a.digest_enabled else "false")
    if a.digest_interval != cfg.digest_interval_minutes:
        text = set_value(text, "digest", "interval_minutes", f"{a.digest_interval:g}")
    return text


def matches(cfg: config_module.Config, a: Answers) -> bool:
    """The loaded config says what was asked."""
    return (
        cfg.notify == a.notify
        and cfg.battery == a.battery
        and (cfg.low_volts, cfg.critical_volts) == levels(a)
        and cfg.digest_enabled == a.digest_enabled
        and cfg.digest_interval_minutes == a.digest_interval
    )


def levels(a: Answers) -> tuple[float | None, float | None]:
    """The (low, critical) levels the answers give."""
    if a.battery == "mains":
        return None, None
    preset = BATTERIES.get(a.battery, (None, None))
    return (
        a.low_volts if a.low_volts is not None else preset[0],
        a.critical_volts if a.critical_volts is not None else preset[1],
    )


def by_hand(a: Answers) -> str:
    """The settings as lines to paste, when the file can't be edited safely."""
    out = ["notify:", *(f"  - {_quote(u)}" for u in a.notify), "power:", f"  battery: {a.battery}"]
    if a.battery != "mains":
        out += [
            f"  {key}: {value:g}"
            for key, value in (("low_volts", a.low_volts), ("critical_volts", a.critical_volts))
            if value is not None
        ]
    out += [
        "digest:",
        f"  enabled: {'true' if a.digest_enabled else 'false'}",
        f"  interval_minutes: {a.digest_interval:g}",
    ]
    return "\n".join(out)


def check_text(text: str, path: Path) -> config_module.Config:
    """Load ``text`` as the config at ``path`` would be loaded (raises ConfigError)."""
    fd, tmp = tempfile.mkstemp(prefix=".setup-", suffix=".yaml", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        return config_module.load(tmp)
    finally:
        os.unlink(tmp)


def save(path: Path, text: str) -> Path | None:
    """Write ``text`` atomically, readable only by you; the old file is kept as ``.bak``.
    Returns the backup's path, if there was a file."""
    backup = None
    if path.exists():
        backup = path.with_name(path.name + ".bak")
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(path.read_text(encoding="utf-8"))
    fd, tmp = tempfile.mkstemp(prefix=".setup-", suffix=".yaml", dir=path.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return backup


# -- the questions --------------------------------------------------------------------------

PUSHOVER_KEY = re.compile(r"^[A-Za-z0-9]{30}$")
SERVICES = [
    ("pushover", N_("Pushover (phone app; can repeat an Emergency until you acknowledge it)")),
    ("ntfy", N_("ntfy (free phone app, no account needed)")),
    ("telegram", N_("Telegram (a bot of your own)")),
    ("discord", N_("Discord (a channel webhook)")),
    ("other", N_("another service: paste an Apprise URL")),
]


def _private(url: str) -> str:
    from ekos_notifier.cli import _private as private

    return private(url).split("?")[0].rstrip("/")


def _valid(url: str) -> str | None:
    import apprise

    return None if apprise.Apprise().add(url) else _("not a URL Apprise understands")


def ask_url(p: Prompter, service: str) -> str:
    if service == "pushover":
        p.say(_("Your user key is at the top of https://pushover.net once you're logged in."))
        p.say(_("For the token, create an application there (call it Ekos Notifier)."))

        def key(answer: str) -> str | None:
            return None if PUSHOVER_KEY.match(answer) else _("that's 30 letters and digits")

        user = p.required(_("User key"), key)
        token = p.required(_("Application token"), key)
        return f"pover://{user}@{token}"
    if service == "ntfy":
        p.say(_("Install the ntfy app and subscribe to a topic. On ntfy.sh anyone who knows the"))
        p.say(_("topic can read it, so make it hard to guess."))
        topic = p.text(_("Topic"), f"ekos-{secrets.token_hex(5)}")
        server = p.text(_("Server"), "ntfy.sh")
        return f"ntfys://{server}/{topic}"
    if service == "telegram":
        p.say(_("Create a bot with @BotFather (it gives you the token), send your bot a message,"))
        p.say(_("then find your chat id (e.g. with @userinfobot)."))
        token = p.required(_("Bot token"))
        chat = p.required(_("Chat id"))
        return f"tgram://{token}/{chat}"
    if service == "discord":
        p.say(_("In Discord: channel settings > Integrations > Webhooks > New Webhook > Copy URL."))
        return p.required(_("Webhook URL"), _valid)
    p.say(_("Any Apprise URL works: https://github.com/caronc/apprise/wiki"))
    return p.required(_("URL"), _valid)


def ask_notify(p: Prompter, current: list[str], send: Callable[[str], str | None]) -> Answers:
    a = Answers(notify=list(current))
    p.say("")
    p.say(_("== Notifications =="))
    if current:
        p.say(_("You have: {notifiers}").format(notifiers=", ".join(_private(u) for u in current)))
        what = p.choice(
            _("What would you like to do?"),
            [("keep", _("keep them")), ("add", _("add another")), ("replace", _("replace them"))],
            "keep",
        )
        if what == "keep":
            return a
        if what == "replace":
            a.notify = []
    elif not p.yes(
        _("Set up a notifier now? (Without one, nights are recorded but nothing is sent.)")
    ):
        return a
    while True:
        url = ask_url(
            p, p.choice(_("Which service?"), [(k, _(v)) for k, v in SERVICES], "pushover", mark="")
        )
        if url in a.notify:
            p.say(_("You have that one already."))
        elif p.yes(_("Send it a test message now?")):
            p.say(_("Sending…"))
            error = send(url)
            if error is None:
                p.say(_("Sent. Check your phone."))
                a.tested.add(url)
                if not p.yes(_("Did it arrive?")):
                    p.say(_("Check the details you entered, and the app's notification settings."))
                    if p.yes(_("Try again?")):
                        continue
            else:
                p.say(_("It didn't send: {error}").format(error=error))
                if p.yes(_("Try again?")):
                    continue
                if not p.yes(_("Keep it anyway?"), default=False):
                    url = ""
            if url:
                a.notify.append(url)
        else:
            a.notify.append(url)
        if not p.yes(_("Add another notifier?"), default=False):
            return a


def ask_power(p: Prompter, cfg: config_module.Config, a: Answers, seen: Seen) -> None:
    p.say("")
    p.say(_("== Battery =="))
    if seen.voltage is not None:
        p.say(_("Your power box reads {voltage} V now.").format(voltage=f"{seen.voltage:.1f}"))
    elif seen.indi:
        p.say(_("No power box voltage recognised on INDI: battery alerts need one."))
    a.battery = p.choice(
        _("What powers the rig?"),
        [
            ("agm_12v", _("12 V lead-acid/AGM (low 12.0 V, critical 11.5 V)")),
            ("lifepo4_12v", _("12 V LiFePO4 (low 12.8 V, critical 12.0 V)")),
            ("mains", _("mains power (no battery alerts)")),
            ("custom", _("another battery: I'll give the levels")),
        ],
        cfg.battery if cfg.battery in ("agm_12v", "lifepo4_12v", "mains", "custom") else "agm_12v",
    )
    power = cfg.raw.get("power") if isinstance(cfg.raw.get("power"), dict) else {}
    own = {k: power[k] for k in ("low_volts", "critical_volts") if power.get(k) is not None}
    if a.battery in ("agm_12v", "lifepo4_12v") and own:
        mine = ", ".join(f"{k.split('_')[0]} {v:g} V" for k, v in own.items())
        if p.yes(
            _("Your config sets its own levels ({mine}). Keep them?").format(mine=mine),
            default=False,
        ):
            a.low_volts = cfg.low_volts if "low_volts" in own else None
            a.critical_volts = cfg.critical_volts if "critical_volts" in own else None
    if a.battery == "custom":
        a.low_volts = p.number(_("Battery low at or under (V)"), cfg.low_volts or 12.0, 5, 60)
        a.critical_volts = p.number(
            _("Battery critical at or under (V)"),
            min(cfg.critical_volts or 11.5, a.low_volts),
            5,
            a.low_volts,
        )


def ask_digest(p: Prompter, cfg: config_module.Config, a: Answers) -> None:
    p.say("")
    p.say(_("== Hourly digest =="))
    p.say(_('During a session: a silent "All\'s well" with progress and conditions, or'))
    p.say(_('"Attention" when something\'s wrong.'))
    a.digest_enabled = p.yes(_("Send it?"), cfg.digest_enabled)
    a.digest_interval = cfg.digest_interval_minutes
    if a.digest_enabled:
        a.digest_interval = p.number(
            _("Every how many minutes"), cfg.digest_interval_minutes, 10, 720
        )


# -- what's on the rig ----------------------------------------------------------------------


@dataclass
class Seen:
    lines: list[str] = field(default_factory=list)  # doctor-style results
    indi: bool = False  # an INDI server answered
    voltage: float | None = None  # the power box's input voltage


async def detect(config: config_module.Config | None) -> Seen:
    """KStars, the INDI devices and PHD2, as doctor sees them, plus the battery voltage."""
    from ekos_notifier import doctor
    from ekos_notifier.ekos.settings import INDI_HOST, INDI_PORT, indi_server
    from ekos_notifier.indi import dump
    from ekos_notifier.indi.decode import IndiDecoder
    from ekos_notifier.indi.presets import load_builtin

    seen = Seen()
    ekos, checks = await doctor.check_dbus()
    host, port = indi_server() or (INDI_HOST, INDI_PORT)
    if config is not None and (config.indi_host or config.indi_port):
        host, port = config.indi_host or INDI_HOST, config.indi_port or INDI_PORT
    try:
        elements = await dump.collect(host, port, doctor.TIMEOUT)
        seen.indi = True
    except (OSError, asyncio.TimeoutError):
        elements = None

    async def collected(*_):
        if elements is None:
            raise ConnectionRefusedError
        return elements

    checks += await doctor.check_indi(config, ekos, lambda: (host, port), collected)
    checks += await doctor.check_phd2()
    decoder = IndiDecoder(load_builtin(), config.indi_mappings if config else ())
    for el in elements or []:
        for event in decoder.feed(0.0, el):
            if event.kind == "metric" and event.data["metric"] == "power.voltage":
                seen.voltage = event.data["value"]
    seen.lines = [f"  {c.status:<5} {c.text}" for c in checks]
    return seen


# -- all of it ------------------------------------------------------------------------------


def summary(a: Answers) -> list[str]:
    if a.notify:
        notify = [
            "  "
            + (_("notify: {url} (tested)") if u in a.tested else _("notify: {url}")).format(
                url=_private(u)
            )
            for u in a.notify
        ]
    else:
        notify = ["  " + _("notify: none (the night is recorded, nothing is sent)")]
    if a.battery == "mains":
        battery = _("mains, no battery alerts")
    else:
        low, critical = levels(a)
        battery = a.battery
        if low is not None:
            battery += ", " + _("low at {volts} V").format(volts=f"{low:g}")
        if critical is not None:
            battery += ", " + _("critical at {volts} V").format(volts=f"{critical:g}")
    if a.digest_enabled:
        digest = _("every {minutes} min").format(minutes=f"{a.digest_interval:g}")
    else:
        digest = _("off")
    return [
        *notify,
        "  " + _("battery: {battery}").format(battery=battery),
        "  " + _("digest: {digest}").format(digest=digest),
    ]


async def _detect_default(cfg):
    return await detect(cfg)


def run(
    config_path: str | None,
    p: Prompter,
    *,
    send: Callable[[str], str | None] | None = None,
    find: Callable[[config_module.Config | None], Awaitable[Seen]] = _detect_default,
    service: Callable[[], bool] | None = None,
    install_service: Callable[[], int] | None = None,
) -> int:
    """The wizard. ``service`` says whether the service is installed; ``install_service``
    installs it. Returns the exit status."""
    from ekos_notifier import systemd
    from ekos_notifier.notify import send_apprise

    path = Path(config_path) if config_path else config_module.default_path()
    send = send or (
        lambda url: send_apprise(
            url,
            "normal",
            _("Ekos Notifier test"),
            _("If you can read this, Ekos Notifier can reach you."),
        )
    )
    if path.exists():
        try:
            cfg = config_module.load(path)
        except config_module.ConfigError as exc:
            p.say(_("Your config has a mistake: {exc}").format(exc=exc))
            p.say(_("Fix it first (ekos-notifier config check explains), then run setup again."))
            return 1
        text = path.read_text(encoding="utf-8")
    else:
        cfg = config_module.Config()
        text = config_module.example_text().replace(
            "# Copy to ~/.config/ekos-notifier/config.yaml. ", "# "
        )
    p.say(_("Ekos Notifier setup"))
    p.say(
        _("Your answers go into {path}; the rest of the file is kept as it is.").format(path=path)
    )
    p.say(_("Enter keeps the answer in [brackets]. Ctrl-C leaves without saving."))
    p.say("")
    try:
        p.say(_("== Your rig =="))
        try:
            seen = asyncio.run(find(cfg))
        except KeyboardInterrupt:
            raise Abort from None
        for line in seen.lines:
            p.say(line)
        a = ask_notify(p, cfg.notify, send)
        ask_power(p, cfg, a, seen)
        ask_digest(p, cfg, a)
        p.say("")
        p.say(_("== Ready =="))
        for line in summary(a):
            p.say(line)
        if not p.yes(_("Save to {path}?").format(path=path)):
            p.say(_("Nothing saved."))
            return 1
    except Abort:
        p.say("")
        p.say(_("Nothing saved."))
        return 1
    try:
        new = apply(text, a, cfg)
        ok = matches(check_text(new, path) if path.parent.exists() else _check_elsewhere(new), a)
    except (CantEdit, config_module.ConfigError):
        ok = False
    if not ok:
        p.say(_("Your config is laid out in a way setup can't edit safely, so it's unchanged."))
        p.say(
            _("Put these settings in {path} by hand (replacing the ones there):").format(path=path)
        )
        p.say("")
        p.say(by_hand(a))
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = save(path, new)
    if backup:
        p.say(_("Saved {path} (the old one is {backup}).").format(path=path, backup=backup.name))
    else:
        p.say(_("Saved {path}.").format(path=path))
    installed = (service or (lambda: (systemd.unit_dir() / systemd.UNIT).exists()))()
    if installed and path.resolve() == config_module.default_path().resolve():
        p.say(_("The service applies it by itself within a few seconds."))
    elif installed:
        pass  # another file: the service reads the one it was installed with
    else:
        p.say("")
        p.say(_("Ekos Notifier isn't set to run by itself yet."))
        try:
            want = p.yes(
                _("Start it now, and with your session from now on (systemd user service)?")
            )
        except Abort:
            want = False
        if want:
            (install_service or _install_service(config_path))()
    p.say("")
    p.say(_("Next:"))
    p.say("  ekos-notifier doctor     " + _("with KStars and Ekos running: checks everything"))
    p.say("  ekos-notifier simulate   " + _("a sample night: the alerts you'd get"))
    return 0


def _check_elsewhere(text: str) -> config_module.Config:
    with tempfile.TemporaryDirectory() as d:
        return check_text(text, Path(d) / "config.yaml")


def _install_service(config_path: str | None) -> Callable[[], int]:
    def install() -> int:
        import argparse

        from ekos_notifier.cli import cmd_install_service

        return cmd_install_service(
            argparse.Namespace(uninstall=False, no_start=False, config=config_path)
        )

    return install
