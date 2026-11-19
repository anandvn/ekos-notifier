"""The config file (``~/.config/ekos-notifier/config.yaml``; see config.example.yaml).

Every key is optional. A missing file means record-only mode. Only the keys the code uses so far
are read; the rest of the example file is kept in ``raw`` for the features still to come.
"""

from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ekos_notifier.i18n import N_, _
from ekos_notifier.indi.decode import Mapping
from ekos_notifier.messages import MessageSettings, SettingsError
from ekos_notifier.power import BATTERIES


class ConfigError(Exception):
    """A config problem, worded for the user."""


@dataclass
class Config:
    path: Path | None = None
    notify: list[str] = field(default_factory=list)
    notify_expire_minutes: float = 10.0
    language: str = "auto"  # messages' language: auto (the system's), en, de, pt_BR …
    guiding_recovery_minutes: float = 5.0
    guiding_poor_minutes: float = 10.0
    twilight_warning_minutes: float = 45.0
    target_low_warning_minutes: float = 30.0
    dew_margin_c: float = 2.0
    cooler_tolerance_c: float = 2.0
    disk_low_gb: float = 10.0
    clouds_drop_percent: float = 50.0
    digest_enabled: bool = True
    digest_interval_minutes: float = 60.0
    digest_align_to_clock: bool = True
    battery: str = "agm_12v"
    low_volts: float | None = 12.0  # None: battery alerts off (mains)
    critical_volts: float | None = 11.5
    indi_host: str | None = None
    indi_port: int | None = None
    indi_mappings: list[Mapping] = field(default_factory=list)
    messages: MessageSettings = field(default_factory=MessageSettings)
    raw: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)  # settings that are ignored, or overridden


# Every setting the file can hold, by section (None: a value, not a section of settings).
# `messages:` is checked against the alert registry instead. Keep in step with
# config.example.yaml (a test checks the example raises no warnings).
_DIGEST = {"enabled", "interval_minutes", "align_to_clock"}
KNOWN: dict[str, set[str] | None] = {
    "notify": None,
    "notify_expire_minutes": None,
    "language": None,
    "digest": _DIGEST,
    "crier": _DIGEST,  # the digest's name before the rename
    "heartbeat": {"url", "grace_minutes"},
    "indi": {"host", "port", "metrics"},
    "power": {"battery", "low_volts", "critical_volts"},
    "messages": None,
    "alerts": {
        "dew_margin_c",
        "guiding_recovery_minutes",
        "guiding_poor_minutes",
        "cooler_tolerance_c",
        "disk_low_gb",
        "clouds_drop_percent",
        "twilight_warning_minutes",
        "target_low_warning_minutes",
    },
    "sync": {"dest", "include_fits", "auto_when_reachable"},
    "gui": {"port", "night_mode"},
    "units": {"temperature", "guide_error", "clock"},
    "privacy": {"round_site_coordinates"},
}
WHY_UNUSED = {
    "power.preset": N_("power boxes are recognised automatically"),
    "style": N_("message styles were dropped: messages are in plain English"),
}


def _not_a_setting(name: str, choices: Any) -> str:
    close = difflib.get_close_matches(name, list(choices), n=1)
    if close:
        return _("not a setting (did you mean '{name}'?)").format(name=close[0])
    return _("not a setting")


def _warnings(raw: dict[str, Any], battery: str, low: Any, critical: Any) -> list[str]:
    """Settings the file has that do nothing, and battery levels that replace the preset's."""
    out = []
    for key, value in raw.items():
        key = str(key)
        if key in WHY_UNUSED:
            out.append(
                _("'{name}' isn't used ({why}); it's ignored").format(
                    name=key, why=_(WHY_UNUSED[key])
                )
            )
            continue
        if key not in KNOWN:
            close = difflib.get_close_matches(key, list(KNOWN), n=1)
            if close:
                text = _("'{name}' isn't a setting (did you mean '{close}'?); it's ignored")
            else:
                text = _("'{name}' isn't a setting; it's ignored")
            out.append(text.format(name=key, close=close[0] if close else ""))
            continue
        section = KNOWN[key]
        if section is None or not isinstance(value, dict):
            continue
        for sub in value:
            name = f"{key}.{sub}"
            if sub not in section:
                why = (
                    _(WHY_UNUSED[name]) if name in WHY_UNUSED else _not_a_setting(str(sub), section)
                )
                out.append(
                    _("'{name}' isn't used ({why}); it's ignored").format(name=name, why=why)
                )
    language = raw.get("language")
    if (
        isinstance(language, str)
        and language not in ("auto", "en")
        and not language.startswith("en_")
    ):
        from ekos_notifier import i18n

        have = i18n.available()
        if language not in have and language.split("_")[0] not in have:
            out.append(
                _(
                    "'language: {language}' has no translation yet, so messages are in English"
                    " (translations: {languages})"
                ).format(language=language, languages=", ".join(["en", *have]))
            )
    power = raw.get("power") if isinstance(raw.get("power"), dict) else {}
    if battery == "mains":
        for name in ("low_volts", "critical_volts"):
            if power.get(name) is not None:
                out.append(
                    _("'power.{name}' is ignored with battery: mains (no voltage alerts)").format(
                        name=name
                    )
                )
    elif battery in BATTERIES:
        preset_low, preset_critical = BATTERIES[battery]
        for name, value, preset in (
            ("low_volts", low, preset_low),
            ("critical_volts", critical, preset_critical),
        ):
            if power.get(name) is not None and value != preset:
                out.append(
                    _(
                        "'power.{name}: {value}' replaces {battery}'s {preset} V; delete it to use"
                        " the preset's level"
                    ).format(name=name, value=f"{value:g}", battery=battery, preset=f"{preset:g}")
                )
    return out


def default_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "ekos-notifier" / "config.yaml"


def example_text() -> str:
    """The commented example config: packaged in the wheel, at the top of a checkout."""
    from importlib import resources

    packaged = resources.files("ekos_notifier") / "config.example.yaml"
    if packaged.is_file():
        return packaged.read_text(encoding="utf-8")
    return (Path(__file__).resolve().parents[2] / "config.example.yaml").read_text(encoding="utf-8")


def init(path: str | Path | None = None) -> tuple[Path, bool]:
    """Write the example config where Ekos Notifier looks for it, readable only by you (it will
    hold notifier keys). Never overwrites: returns (path, written)."""
    path = Path(path) if path is not None else default_path()
    if path.exists():
        return path, False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(example_text().replace("# Copy to ~/.config/ekos-notifier/config.yaml. ", "# "))
    return path, True


def load(path: str | Path | None = None) -> Config:
    """Read the config. A missing default file is fine; a missing explicit one is an error."""
    explicit = path is not None
    path = Path(path) if explicit else default_path()
    if not path.exists():
        if explicit:
            raise ConfigError(_("{path}: no such file").format(path=path))
        return Config()
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(_("{path}: not valid YAML: {exc}").format(path=path, exc=exc)) from None
    if not isinstance(raw, dict):
        raise ConfigError(
            _("{path}: expected keys like 'notify:', found {name}").format(
                path=path, name=type(raw).__name__
            )
        )

    notify = raw.get("notify") or []
    if isinstance(notify, str):
        notify = [notify]
    if not isinstance(notify, list) or not all(isinstance(u, str) for u in notify):
        raise ConfigError(
            _("{path}: 'notify' must be a list of Apprise URLs (quoted strings)").format(path=path)
        )
    expire = _minutes(path, "notify_expire_minutes", raw.get("notify_expire_minutes", 10))
    language = raw.get("language", "auto")
    if language is None:
        language = "auto"
    if not isinstance(language, str) or not re.fullmatch(
        r"auto|[A-Za-z]{2,3}(_[A-Za-z]{2})?", language
    ):
        raise ConfigError(
            _("{path}: 'language' must be auto or a language code such as de or pt_BR").format(
                path=path
            )
        )
    alerts = raw.get("alerts") or {}
    if not isinstance(alerts, dict):
        raise ConfigError(
            _("{path}: 'alerts' must be a section of settings (key: value)").format(path=path)
        )
    guiding = _minutes(
        path, "alerts.guiding_recovery_minutes", alerts.get("guiding_recovery_minutes", 5)
    )
    poor = _minutes(path, "alerts.guiding_poor_minutes", alerts.get("guiding_poor_minutes", 10))
    # Before the rename (Town Crier) the section was called "crier:"; configs copied then keep it.
    digest = raw.get("digest", raw.get("crier")) or {}
    if not isinstance(digest, dict):
        raise ConfigError(
            _("{path}: 'digest' must be a section of settings (key: value)").format(path=path)
        )
    interval = _minutes(path, "digest.interval_minutes", digest.get("interval_minutes", 60))
    if interval == 0:
        raise ConfigError(
            _("{path}: 'digest.interval_minutes' must be more than 0").format(path=path)
        )
    indi = raw.get("indi") or {}
    if not isinstance(indi, dict):
        raise ConfigError(
            _("{path}: 'indi' must be a section of settings (key: value)").format(path=path)
        )
    host = indi.get("host") or None
    if host is not None and not isinstance(host, str):
        raise ConfigError(_("{path}: 'indi.host' must be a host name").format(path=path))
    port = indi.get("port") or None
    if port is not None and (isinstance(port, bool) or not isinstance(port, int) or port < 1):
        raise ConfigError(_("{path}: 'indi.port' must be a port number").format(path=path))
    mappings = []
    for metric, target in (indi.get("metrics") or {}).items():
        try:
            mappings.append(Mapping.parse(str(metric), str(target)))
        except ValueError as exc:
            raise ConfigError(_("{path}: indi.metrics: {exc}").format(path=path, exc=exc)) from None
    battery, low_volts, critical_volts = _power(path, raw.get("power"))
    messages = raw.get("messages") or {}
    if not isinstance(messages, dict):
        raise ConfigError(
            _("{path}: 'messages' must be a section of alert: level lines").format(path=path)
        )
    try:
        message_settings = MessageSettings.parse(messages)
    except SettingsError as exc:
        raise ConfigError(_("{path}: messages: {exc}").format(path=path, exc=exc)) from None
    return Config(
        path=path,
        notify=[u.strip() for u in notify if u.strip()],
        notify_expire_minutes=expire,
        language=language,
        guiding_recovery_minutes=guiding,
        guiding_poor_minutes=poor,
        twilight_warning_minutes=_minutes(
            path, "alerts.twilight_warning_minutes", alerts.get("twilight_warning_minutes", 45)
        ),
        target_low_warning_minutes=_minutes(
            path, "alerts.target_low_warning_minutes", alerts.get("target_low_warning_minutes", 30)
        ),
        dew_margin_c=_number(path, "alerts.dew_margin_c", alerts.get("dew_margin_c", 2.0), "°C"),
        cooler_tolerance_c=_number(
            path, "alerts.cooler_tolerance_c", alerts.get("cooler_tolerance_c", 2.0), "°C"
        ),
        disk_low_gb=_number(path, "alerts.disk_low_gb", alerts.get("disk_low_gb", 10), "GB"),
        clouds_drop_percent=_number(
            path, "alerts.clouds_drop_percent", alerts.get("clouds_drop_percent", 50), "percent"
        ),
        digest_enabled=_bool(path, "digest.enabled", digest.get("enabled", True)),
        digest_interval_minutes=interval,
        digest_align_to_clock=_bool(
            path, "digest.align_to_clock", digest.get("align_to_clock", True)
        ),
        battery=battery,
        low_volts=low_volts,
        critical_volts=critical_volts,
        indi_host=host,
        indi_port=port,
        indi_mappings=mappings,
        messages=message_settings,
        raw=raw,
        warnings=_warnings(raw, battery, low_volts, critical_volts),
    )


def _power(path: Path, power: object) -> tuple[str, float | None, float | None]:
    """The battery preset, and its low and critical volts unless set explicitly."""
    power = power or {}
    if not isinstance(power, dict):
        raise ConfigError(
            _("{path}: 'power' must be a section of settings (key: value)").format(path=path)
        )
    battery = power.get("battery") or "agm_12v"
    if battery not in BATTERIES and battery != "custom":
        names = " | ".join([*BATTERIES, "custom"])
        raise ConfigError(
            _("{path}: 'power.battery' must be one of {names}").format(path=path, names=names)
        )
    low, critical = BATTERIES.get(battery, (None, None))
    if battery == "mains":
        return battery, None, None
    if power.get("low_volts") is not None:
        low = _number(path, "power.low_volts", power["low_volts"], "volts")
    if power.get("critical_volts") is not None:
        critical = _number(path, "power.critical_volts", power["critical_volts"], "volts")
    if battery == "custom" and low is None and critical is None:
        raise ConfigError(
            _("{path}: 'power.battery: custom' needs low_volts or critical_volts").format(path=path)
        )
    if low is not None and critical is not None and critical >= low:
        raise ConfigError(
            _("{path}: 'power.critical_volts' must be below 'power.low_volts'").format(path=path)
        )
    return battery, low, critical


def _bool(path: Path, key: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(_("{path}: '{key}' must be true or false").format(path=path, key=key))
    return value


def _minutes(path: Path, key: str, value: object) -> float:
    return _number(path, key, value, "minutes")


def _number(path: Path, key: str, value: object, unit: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ConfigError(
            _("{path}: '{key}' must be a number of {unit}, 0 or more").format(
                path=path, key=key, unit=unit
            )
        )
    return float(value)
