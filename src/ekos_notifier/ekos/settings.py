"""Which guider Ekos uses, read from the running KStars's own files. Observe-only.

Two places say, and Ekos Notifier reads both (verified on KStars 3.8.5, 2026-11-17):

- **The Ekos profile.** Each equipment profile has a guider: ``userdb.sqlite`` table ``profile``,
  columns ``guidertype`` (0 internal, 1 PHD2, 2 lin_guider), ``guiderhost``, ``guiderport``. The
  selected profile is ``kstarsrc`` ``[Ekos] profile`` (default "Simulators"), written when Ekos
  starts.
- **The Guide module's setting**, ``kstarsrc`` ``[Guide] GuiderType``, ``PHD2Host``,
  ``PHD2Port`` (defaults 0, localhost, 4400). Ekos copies the profile's guider into it when it
  starts (internal included), and the user can change it in Guide → Options; the Guide module
  uses this one.

KDE drops a setting equal to its default from the file, so a missing key means the default.
Before Ekos starts the two can differ (a profile was picked but not started). "Not PHD2" is
certain only when both say so; if either says PHD2, it's PHD2.

Files are found the way KStars finds them: through the running process's own ``XDG_CONFIG_*``
and ``XDG_DATA_HOME``, inside its filesystem root (``/proc/<pid>/root``), so a Flatpak's sandbox
paths work. The answer is ``unknown`` when anything is missing or unreadable, or several KStars
processes disagree.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from ekos_notifier.ekos.bus import KStarsProcess, kstars_processes

log = logging.getLogger(__name__)

GUIDERS = {0: "internal", 1: "phd2", 2: "lin_guider"}
DEFAULT_GUIDER = 0
PHD2_HOST = "localhost"
PHD2_PORT = 4400
DEFAULT_PROFILE = "Simulators"
_GROUP = re.compile(r"^\[(.*)\]\s*$")
_KEY_FLAGS = re.compile(r"\[\$[^\]]*\]$")  # Key[$e], Key[$i]


@dataclass(frozen=True)
class GuiderSettings:
    guider: str  # internal | phd2 | lin_guider | unknown
    phd2_host: str = PHD2_HOST
    phd2_port: int = PHD2_PORT
    profile: str | None = None  # the selected Ekos profile

    @property
    def uses_phd2(self) -> bool | None:
        """True or False when certain, None when unknown."""
        return None if self.guider == "unknown" else self.guider == "phd2"


UNKNOWN = GuiderSettings("unknown")


def parse_kconfig(text: str) -> dict[str, dict[str, str]]:
    """The groups and keys of a KConfig file. Later lines win; key flags are dropped."""
    groups: dict[str, dict[str, str]] = {}
    group = groups.setdefault("", {})
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _GROUP.match(line)
        if m:
            group = groups.setdefault(m.group(1), {})
            continue
        key, sep, value = line.partition("=")
        if sep:
            key = _KEY_FLAGS.sub("", key.strip())
            if not key.endswith("]"):  # localized keys (Key[de]) aren't ours
                group[key] = value.strip()
    return groups


def _inside(root: Path, path: str) -> Path:
    return root / PurePosixPath(path).relative_to("/")


def config_files(k: KStarsProcess) -> tuple[list[Path], Path]:
    """System files (least important first) and the user file, as seen from outside."""
    env = k.environ
    home = env.get("XDG_CONFIG_HOME") or (env.get("HOME", "") + "/.config")
    dirs = [d for d in (env.get("XDG_CONFIG_DIRS") or "/etc/xdg").split(":") if d.startswith("/")]
    system = [_inside(k.root, d) / "kstarsrc" for d in reversed(dirs)]
    return system, _inside(k.root, home) / "kstarsrc"


def userdb_file(k: KStarsProcess) -> Path:
    env = k.environ
    data = env.get("XDG_DATA_HOME") or (env.get("HOME", "") + "/.local/share")
    return _inside(k.root, data) / "kstars" / "userdb.sqlite"


def read_kstarsrc(k: KStarsProcess) -> dict[str, dict[str, str]] | None:
    """The merged ``Ekos`` and ``Guide`` groups, or None if the user file can't be read."""
    system, user = config_files(k)
    merged: dict[str, dict[str, str]] = {"Ekos": {}, "Guide": {}}

    def merge(path: Path) -> None:
        groups = parse_kconfig(path.read_text(errors="replace"))
        for name, keys in merged.items():
            keys.update(groups.get(name, {}))

    for path in system:
        try:
            merge(path)
        except OSError:
            pass  # most system files don't exist
    try:
        merge(user)
    except OSError as exc:
        log.debug("KStars config %s unreadable: %s", user, exc)
        return None
    return merged


def read_profile(path: Path, name: str) -> tuple[int, str | None, int | None] | None:
    """The profile's (guidertype, guiderhost, guiderport), or None if it can't be read."""
    try:
        con = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return None
    try:
        row = con.execute(
            "SELECT guidertype, guiderhost, guiderport FROM profile WHERE name = ?", (name,)
        ).fetchone()
    except sqlite3.Error as exc:
        log.debug("KStars profiles %s unreadable: %s", path, exc)
        return None
    finally:
        con.close()
    if row is None or not isinstance(row[0], int):
        return None
    return row[0], row[1], row[2]


def read_settings(k: KStarsProcess) -> GuiderSettings:
    rc = read_kstarsrc(k)
    if rc is None:
        return UNKNOWN
    profile_name = rc["Ekos"].get("profile") or DEFAULT_PROFILE
    profile = read_profile(userdb_file(k), profile_name)
    try:
        module = (
            GUIDERS.get(int(rc["Guide"].get("GuiderType", DEFAULT_GUIDER))),
            rc["Guide"].get("PHD2Host") or PHD2_HOST,
            int(rc["Guide"].get("PHD2Port", PHD2_PORT)),
        )
    except ValueError:
        return UNKNOWN
    if module[0] == "phd2":
        return GuiderSettings("phd2", module[1], module[2], profile_name)
    if profile is not None and GUIDERS.get(profile[0]) == "phd2":
        return GuiderSettings(
            "phd2", profile[1] or PHD2_HOST, profile[2] or PHD2_PORT, profile_name
        )
    if profile is None or module[0] is None or GUIDERS.get(profile[0]) is None:
        return UNKNOWN  # the profile is unreadable, or a guider we don't know
    if GUIDERS[profile[0]] != module[0]:
        return UNKNOWN  # internal vs lin_guider: they disagree, and neither is PHD2
    return GuiderSettings(module[0], profile=profile_name)


def guider_settings(proc: Path = Path("/proc"), uid: int | None = None) -> GuiderSettings:
    found = {read_settings(k) for k in kstars_processes(proc, uid)}
    if len(found) != 1:
        return UNKNOWN  # none running, or instances that disagree
    (settings,) = found
    return settings


def guide_deviation_limit(proc: Path = Path("/proc"), uid: int | None = None) -> float | None:
    """Ekos's default guide-deviation limit (``kstarsrc`` ``[Capture] GuideDeviation``, arcsec),
    from the running KStars; None if it can't be read. A job in a sequence can override it, and
    Ekos only enforces it when the job switches it on."""
    values = set()
    for k in kstars_processes(proc, uid):
        system, user = config_files(k)
        merged: dict[str, str] = {}
        for path in [*system, user]:
            try:
                merged.update(parse_kconfig(path.read_text(errors="replace")).get("Capture", {}))
            except OSError:
                continue
        try:
            values.add(float(merged.get("GuideDeviation", 2.0)))
        except ValueError:
            return None
    return values.pop() if len(values) == 1 else None


INDI_HOST = "localhost"
INDI_PORT = 7624


def indi_server(proc: Path = Path("/proc"), uid: int | None = None) -> tuple[str, int] | None:
    """The INDI server the active Ekos profile uses (``profile`` ``host``/``port``; empty means
    local mode: Ekos starts its own server on localhost:7624). None if it can't be read."""
    found = set()
    for k in kstars_processes(proc, uid):
        rc = read_kstarsrc(k)
        if rc is None:
            return None
        name = rc["Ekos"].get("profile") or DEFAULT_PROFILE
        try:
            con = sqlite3.connect(f"file:{quote(str(userdb_file(k)))}?mode=ro", uri=True, timeout=2)
            try:
                row = con.execute(
                    "SELECT host, port FROM profile WHERE name = ?", (name,)
                ).fetchone()
            finally:
                con.close()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        found.add((row[0] or INDI_HOST, int(row[1] or INDI_PORT)))
    return found.pop() if len(found) == 1 else None
