"""The config file (``~/.config/ekos-notifier/config.yaml``; see config.example.yaml).

Every key is optional. A missing file means record-only mode. Only the keys the code uses so far
are read; the rest of the example file is kept in ``raw`` for the features still to come.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    """A config problem, worded for the user."""


@dataclass
class Config:
    path: Path | None = None
    notify: list[str] = field(default_factory=list)
    notify_expire_minutes: float = 10.0
    guiding_recovery_minutes: float = 5.0
    guiding_poor_minutes: float = 10.0
    digest_enabled: bool = True
    digest_interval_minutes: float = 60.0
    digest_align_to_clock: bool = True
    raw: dict[str, Any] = field(default_factory=dict)


def default_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "ekos-notifier" / "config.yaml"


def load(path: str | Path | None = None) -> Config:
    """Read the config. A missing default file is fine; a missing explicit one is an error."""
    explicit = path is not None
    path = Path(path) if explicit else default_path()
    if not path.exists():
        if explicit:
            raise ConfigError(f"{path}: no such file")
        return Config()
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: not valid YAML: {exc}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected keys like 'notify:', found {type(raw).__name__}")

    notify = raw.get("notify") or []
    if isinstance(notify, str):
        notify = [notify]
    if not isinstance(notify, list) or not all(isinstance(u, str) for u in notify):
        raise ConfigError(f"{path}: 'notify' must be a list of Apprise URLs (quoted strings)")
    expire = _minutes(path, "notify_expire_minutes", raw.get("notify_expire_minutes", 10))
    alerts = raw.get("alerts") or {}
    if not isinstance(alerts, dict):
        raise ConfigError(f"{path}: 'alerts' must be a section of settings (key: value)")
    guiding = _minutes(
        path, "alerts.guiding_recovery_minutes", alerts.get("guiding_recovery_minutes", 5)
    )
    poor = _minutes(path, "alerts.guiding_poor_minutes", alerts.get("guiding_poor_minutes", 10))
    # Before the rename (Town Crier) the section was called "crier:"; configs copied then keep it.
    digest = raw.get("digest", raw.get("crier")) or {}
    if not isinstance(digest, dict):
        raise ConfigError(f"{path}: 'digest' must be a section of settings (key: value)")
    interval = _minutes(path, "digest.interval_minutes", digest.get("interval_minutes", 60))
    if interval == 0:
        raise ConfigError(f"{path}: 'digest.interval_minutes' must be more than 0")
    return Config(
        path=path,
        notify=[u.strip() for u in notify if u.strip()],
        notify_expire_minutes=expire,
        guiding_recovery_minutes=guiding,
        guiding_poor_minutes=poor,
        digest_enabled=_bool(path, "digest.enabled", digest.get("enabled", True)),
        digest_interval_minutes=interval,
        digest_align_to_clock=_bool(
            path, "digest.align_to_clock", digest.get("align_to_clock", True)
        ),
        raw=raw,
    )


def _bool(path: Path, key: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{path}: '{key}' must be true or false")
    return value


def _minutes(path: Path, key: str, value: object) -> float:
    return _number(path, key, value, "minutes")


def _number(path: Path, key: str, value: object, unit: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ConfigError(f"{path}: '{key}' must be a number of {unit}, 0 or more")
    return float(value)
