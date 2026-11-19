"""Ekos's log lines in English, whatever language KStars runs in.

Several rules read Ekos's log ("Autofocus failed.", "Solver Failed.", "Disconnecting INDI
devices..."), and KStars translates those lines at runtime (KDE's i18n). In German, for
instance, "Autofocus failed." arrives as "Die automatische Fokussierung ist fehlgeschlagen.", and
a rule looking for the English would silently never fire.

KStars ships its translations as gettext catalogs (``kstars.mo``, one per language), whose keys
are its English source strings. So a translated line can be turned back into the English one:
the catalog's translations become patterns (``%1``, ``%2`` … match anything), and a line that
matches one in full is rewritten as its English source with the same values put back. Lines
that match nothing (untranslated, or not from a translatable string) are left as they are.

The language is the running KStars's own: its app-specific override (Help > Switch Application
Language, ``klanguageoverridesrc``), else ``LANGUAGE``, ``LC_ALL``, ``LC_MESSAGES`` and ``LANG``
from its environment. The catalog is looked up inside the process's filesystem root, so a
Flatpak's (``/app/share/locale``) is found as well as a native one.
"""

from __future__ import annotations

import functools
import gettext
import logging
import re
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ekos_notifier.ekos.bus import KStarsProcess, kstars_processes
from ekos_notifier.ekos.settings import _inside, parse_kconfig
from ekos_notifier.events import Event

log = logging.getLogger(__name__)

DOMAIN = "kstars"
MIN_LITERAL = 4  # a translation with less fixed text than this is too vague to match on
_PLACEHOLDER = re.compile(r"%(\d+)")
_PREFIX = re.compile(r"^(\[[^\]]*\]\s*)")  # "[CCD Simulator] ", added outside the i18n string
# Where KStars keeps its catalogs on this computer, for replaying a recording made in a language
INSTALLED = (
    "~/.local/share/flatpak/app/org.kde.kstars/current/active/files/share/locale",
    "/var/lib/flatpak/app/org.kde.kstars/current/active/files/share/locale",
    "/usr/local/share/locale",
    "/usr/share/locale",
)

# A number as KStars may write it, in any locale: 10.000, 10,000, 1,200.000, 1 200,000. Matching
# only: what it means depends on the locale it was written in (``localized``, ``plain``).
NUMBER = r"-?\d(?:[\d.,\u00a0\u202f\u2019']*\d)?"


@dataclass(frozen=True)
class NumberFormat:
    decimal: str
    group: str

    def parse(self, text: str) -> float | None:
        """``text`` as written in this format, or None when it isn't (in German, "1.5": a
        grouping mark must separate groups of three digits)."""
        whole, point, decimals = text.partition(self.decimal)
        if self.group and self.group in whole:
            first, *rest = whole.lstrip("-").split(self.group)
            if not 1 <= len(first) <= 3 or any(len(g) != 3 for g in rest):
                return None
            whole = whole.replace(self.group, "")
        if not re.fullmatch(r"-?\d+", whole) or (point and not decimals.isdigit()):
            return None
        return float(f"{whole}.{decimals}" if point else whole)


C = NumberFormat(".", "")  # Qt's QString::number and arg() without %L
ENGLISH = NumberFormat(".", ",")  # what log lines with no "numbers" were written in


def localized(text: str, data: dict) -> float | None:
    """A number KStars wrote in its locale (Qt's %L: exposures, deviation limits, setpoints),
    read in the format the adapter found for it and put on the log line (``numbers``). None
    when that format isn't known on this computer, or the text isn't in it."""
    if "numbers" not in data:
        return ENGLISH.parse(text)
    fmt = data["numbers"]
    if not isinstance(fmt, dict):
        return None
    return NumberFormat(fmt.get("decimal", "."), fmt.get("group", "")).parse(text)


def plain(text: str) -> float | None:
    """A number KStars writes the same in every locale (guiding deviations, download times)."""
    return C.parse(text)


@functools.cache
def number_format(locale_name: str) -> NumberFormat | None:
    """The separators of a locale, from this computer's C library (``locale -k``); None when it
    doesn't have that locale. KStars's Qt carries its own locale data, so KStars can use a locale
    this computer hasn't generated."""
    base = locale_name.split(".")[0].split("@")[0]
    if base in ("", "C", "POSIX"):
        return C
    try:
        r = subprocess.run(
            ["locale", "-k", "LC_NUMERIC"],
            env={"LC_ALL": locale_name, "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode or "Cannot set" in r.stderr:
        return None
    values = dict(re.findall(r'^(decimal_point|thousands_sep)="(.*)"$', r.stdout, re.M))
    if not values.get("decimal_point"):
        return None
    return NumberFormat(values["decimal_point"], values.get("thousands_sep", ""))


def numeric_locale(env: dict[str, str]) -> str:
    """The locale KStars (Qt) writes numbers in: LC_ALL, else LC_NUMERIC, else LANG. Its language
    override (Help > Switch Application Language) changes the words, not the numbers."""
    for var in ("LC_ALL", "LC_NUMERIC", "LANG"):
        if env.get(var):
            return env[var]
    return "C"


def with_numbers(event: Event, fmt: NumberFormat | None) -> Event:
    """An Ekos log line carrying the number format it was written in (none needed for English:
    that's what a line without one means). ``fmt`` None: KStars's format isn't known here."""
    if event.kind != "ekos.log" or fmt == ENGLISH or "numbers" in event.data:
        return event
    numbers = None if fmt is None else {"decimal": fmt.decimal, "group": fmt.group}
    data = {**event.data, "numbers": numbers}
    return Event(event.ts, event.kind, data, event.source, event.session_id)


@dataclass(frozen=True)
class _Entry:
    anchor: str  # the longest fixed piece of the translation: a quick test before the regex
    pattern: re.Pattern[str]
    english: str
    weight: int  # fixed characters: the most specific match wins


def _compile(translated: str) -> re.Pattern[str] | None:
    parts, seen = [], set()
    pos = 0
    for m in _PLACEHOLDER.finditer(translated):
        parts.append(re.escape(translated[pos : m.start()]))
        n = m.group(1)
        parts.append(f"(?P=a{n})" if n in seen else f"(?P<a{n}>.+?)")
        seen.add(n)
        pos = m.end()
    parts.append(re.escape(translated[pos:]))
    try:
        return re.compile("".join(parts), re.S)
    except re.error:
        return None


class BackTranslator:
    """Turns lines in one language back into KStars's English source strings."""

    def __init__(self, language: str, catalog: dict) -> None:
        self.language = language
        self._entries: list[_Entry] = []
        for key, translated in catalog.items():
            english = key[0] if isinstance(key, tuple) else key  # plural forms: (msgid, n)
            if not isinstance(english, str) or not english or not isinstance(translated, str):
                continue
            translated = translated.strip()
            if not translated or translated == english.strip():
                continue
            fixed = _PLACEHOLDER.split(translated)[::2]
            anchor = max(fixed, key=len)
            if len(anchor.strip()) < MIN_LITERAL:
                continue
            pattern = _compile(translated)
            if pattern is not None:
                self._entries.append(_Entry(anchor, pattern, english.strip(), sum(map(len, fixed))))

    def __len__(self) -> int:
        return len(self._entries)

    def english(self, line: str) -> str:
        """The line in English, or the line as it is when it isn't a translation we know."""
        m = _PREFIX.match(line)
        prefix, text = (m.group(1), line[m.end() :]) if m else ("", line)
        text = text.strip()
        best: tuple[int, str] | None = None
        for e in self._entries:
            if e.anchor not in text or (best and e.weight <= best[0]):
                continue
            found = e.pattern.fullmatch(text)
            if found:
                values = found.groupdict()
                english = _PLACEHOLDER.sub(
                    lambda p, v=values: v.get(f"a{p.group(1)}", p.group(0)), e.english
                )
                best = (e.weight, english)
        return prefix + best[1] if best else line


def to_english(event: Event, translator: BackTranslator | None) -> Event:
    """An Ekos log line in English, with the line as KStars wrote it in ``original``."""
    if translator is None or event.kind != "ekos.log":
        return event
    text = str(event.data.get("text", ""))
    english = translator.english(text)
    if english == text:
        return event
    data = {**event.data, "text": english, "original": text}
    return Event(event.ts, event.kind, data, event.source, event.session_id)


# -- finding the running KStars's language and catalog ----------------------------------------


def _codes(value: str) -> list[str]:
    """Catalog folder names to try for a locale value: de_DE.UTF-8@euro -> de_DE, de."""
    out = []
    for item in value.split(":"):
        base = item.split(".")[0].split("@")[0].strip()
        if not base or base in ("C", "POSIX"):
            continue
        for code in (base, base.split("_")[0]):
            if code not in out:
                out.append(code)
    return out


def languages(k: KStarsProcess) -> list[str]:
    """The languages KStars uses, most preferred first (as catalog folder names)."""
    env = k.environ
    home = env.get("XDG_CONFIG_HOME") or (env.get("HOME", "") + "/.config")
    try:
        rc = parse_kconfig((_inside(k.root, home) / "klanguageoverridesrc").read_text())
        override = rc.get("Language", {}).get("kstars", "")
    except (OSError, ValueError):
        override = ""
    if m := re.fullmatch(r"@ByteArray\((.*)\)", override.strip()):
        override = m.group(1)  # how KStars writes it (QSettings): kstars=@ByteArray(de)
    if override:
        return _codes(override)
    if env.get("LANGUAGE") and env.get("LC_ALL", env.get("LANG", "")) not in ("C", "POSIX"):
        return _codes(env["LANGUAGE"])
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        if env.get(var):
            return _codes(env[var])
    return []


def catalog_dirs(k: KStarsProcess) -> list[Path]:
    data = k.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    dirs = [d for d in data.split(":") if d.startswith("/")]
    for extra in ("/app/share", "/usr/share"):  # Flatpak, native
        if extra not in dirs:
            dirs.append(extra)
    return [_inside(k.root, d) / "locale" for d in dirs]


def find_catalog(k: KStarsProcess) -> tuple[str, Path] | None:
    """(language, catalog path) for the running KStars; None when it runs in English (or its
    language has no catalog, so it shows English anyway)."""
    for code in languages(k):
        if code == "en" or code.startswith("en_"):
            return None
        for d in catalog_dirs(k):
            path = d / code / "LC_MESSAGES" / f"{DOMAIN}.mo"
            if path.is_file():
                return code, path
    return None


def load(path: Path, language: str) -> BackTranslator | None:
    try:
        with path.open("rb") as f:
            catalog = gettext.GNUTranslations(f)._catalog  # noqa: SLF001 - the raw table
    except (OSError, ValueError, UnicodeDecodeError, struct.error) as exc:
        log.warning("KStars's %s translations (%s) can't be read: %s", language, path, exc)
        return None
    return BackTranslator(language, catalog)


def for_language(language: str | None, dirs: tuple[str, ...] = INSTALLED) -> BackTranslator | None:
    """A back-translator for a recording's language, from the KStars installed here; None for
    English, or when this computer has no catalog for it."""
    for code in _codes(language or ""):
        if code == "en" or code.startswith("en_"):
            return None
        for d in dirs:
            path = Path(d).expanduser() / code / "LC_MESSAGES" / f"{DOMAIN}.mo"
            if path.is_file():
                return load(path, code)
    return None


def translated_prefixes(
    prefixes: tuple[str, ...], domain: str, dirs: tuple[str, ...]
) -> tuple[str, ...]:
    """``prefixes`` and the start of every translation, in each installed catalog of ``domain``,
    of a message beginning with one of them (up to its first placeholder). For matching a
    program's messages in whatever language it runs, without finding out which."""
    out = list(prefixes)
    for d in dirs:
        for path in sorted(Path(d).expanduser().glob(f"*/LC_MESSAGES/{domain}.mo")):
            try:
                with path.open("rb") as f:
                    catalog = gettext.GNUTranslations(f)._catalog  # noqa: SLF001
            except (OSError, ValueError, UnicodeDecodeError, struct.error):
                continue
            for key, translated in catalog.items():
                english = key[0] if isinstance(key, tuple) else key
                if (
                    isinstance(english, str)
                    and isinstance(translated, str)
                    and english.startswith(prefixes)
                ):
                    start = translated.split("%")[0].strip()
                    if len(start) >= MIN_LITERAL and start not in out:
                        out.append(start)
    return tuple(out)


def numbers_for_running_kstars(processes=kstars_processes) -> tuple[str, NumberFormat | None]:
    """(locale, its number format or None when unknown here) for the running KStars; English
    when there's none."""
    for k in processes():
        name = numeric_locale(k.environ)
        return name, number_format(name)
    return "", ENGLISH


def for_running_kstars(processes=kstars_processes) -> BackTranslator | None:
    """A back-translator for the running KStars, or None when it's in English."""
    for k in processes():
        found = find_catalog(k)
        if found:
            return load(found[1], found[0])
    return None
