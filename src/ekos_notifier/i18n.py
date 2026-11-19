"""Ekos Notifier's own messages in the user's language (docs/design.md, "Message wording").

Messages are written in English and marked for translation where they're made::

    _("Battery low: {volts} V").format(volts=...)
    ngettext("{n} frame", "{n} frames", n).format(n=n)
    N_("the night ended")  # a table entry: marked here, translated where it's used with _()

Values go in as named placeholders (``{volts}``), never pasted into the sentence, so a
translation can put them where its grammar wants. ``tools/update_pot.py`` collects the marked
strings into ``locale/ekos-notifier.pot``; a translation is ``locale/<language>.po`` (de.po,
pt_BR.po), the gettext format Poedit and Weblate edit. Catalogs ship as text and are read at
startup, so there's no build step.

The language is the config's ``language:`` (``auto``, the default: the service's own locale,
``LANGUAGE``/``LC_ALL``/``LC_MESSAGES``/``LANG``). One language at a time, set with ``use``.
Messages are translated when they're made: the session record keeps what was sent.
"""

from __future__ import annotations

import ast
import gettext
import io
import logging
import os
import struct
from pathlib import Path

log = logging.getLogger(__name__)

LOCALE_DIR = Path(__file__).parent / "locale"
_ENGLISH = gettext.NullTranslations()
_current: gettext.NullTranslations = _ENGLISH
_language = "en"


def _(message: str) -> str:
    return _current.gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    return _current.ngettext(singular, plural, n)


def N_(message: str) -> str:  # noqa: N802 - the gettext convention
    """Marks a string for translation without translating it (it's translated where used)."""
    return message


def language() -> str:
    """The language messages are in now ("en" when untranslated)."""
    return _language


# Words from Ekos and our own records that go into messages: Ekos's states (ekos.states, as
# messages show them: "in progress"), frame types. Anything else goes in as it is.
WORDS = {
    "idle": N_("idle"),
    "in_progress": N_("in progress"),
    "capturing": N_("capturing"),
    "pause_planned": N_("pause planned"),
    "paused": N_("paused"),
    "suspended": N_("suspended"),
    "aborted": N_("aborted"),
    "waiting": N_("waiting"),
    "image_received": N_("image received"),
    "dithering": N_("dithering"),
    "focusing": N_("focusing"),
    "filter_focus": N_("filter focus"),
    "changing_filter": N_("changing filter"),
    "guider_drift": N_("guider drift"),
    "setting_temperature": N_("setting temperature"),
    "setting_rotator": N_("setting rotator"),
    "aligning": N_("aligning"),
    "calibrating": N_("calibrating"),
    "meridian_flip": N_("meridian flip"),
    "complete": N_("complete"),
    "connected": N_("connected"),
    "disconnected": N_("disconnected"),
    "capture": N_("capture"),
    "looping": N_("looping"),
    "dark": N_("dark"),
    "subframe": N_("subframe"),
    "star_select": N_("star select"),
    "calibration_error": N_("calibration error"),
    "calibration_success": N_("calibration success"),
    "guiding": N_("guiding"),
    "reacquire": N_("reacquire"),
    "manual_dithering": N_("manual dithering"),
    "dithering_error": N_("dithering error"),
    "dithering_success": N_("dithering success"),
    "dithering_settle": N_("dithering settle"),
    "failed": N_("failed"),
    "framing": N_("framing"),
    "successful": N_("successful"),
    "syncing": N_("syncing"),
    "slewing": N_("slewing"),
    "rotating": N_("rotating"),
    "moving": N_("moving"),
    "tracking": N_("tracking"),
    "parking": N_("parking"),
    "parked": N_("parked"),
    "error": N_("error"),
    "unknown": N_("unknown"),
    "unparking": N_("unparking"),
    "unparked": N_("unparked"),
    "startup": N_("startup"),
    "running": N_("running"),
    "shutdown": N_("shutdown"),
    "loading": N_("loading"),
    "west": N_("west"),
    "east": N_("east"),
    "light": N_("light"),
    "bias": N_("bias"),
    "flat": N_("flat"),
    "pending": N_("pending"),
    "success": N_("success"),
    "dark flat": N_("dark flat"),
    "no filter": N_("no filter"),
}


def word(name: str) -> str:
    """A state or frame type from Ekos in the messages' language."""
    return _(WORDS[name]) if name in WORDS else name.replace("_", " ")


# -- choosing the language ------------------------------------------------------------------


def _codes(value: str) -> list[str]:
    """Catalog names to try for a locale value: de_DE.UTF-8@euro -> de_DE, de."""
    out: list[str] = []
    for item in value.split(":"):
        base = item.split(".")[0].split("@")[0].strip()
        if not base or base in ("C", "POSIX"):
            continue
        for code in (base, base.split("_")[0]):
            if code not in out:
                out.append(code)
    return out


def system_language(env: dict[str, str] | None = None) -> str:
    """The language this process's locale asks for, as gettext reads it."""
    env = dict(os.environ) if env is None else env
    lang = env.get("LC_ALL") or env.get("LC_MESSAGES") or env.get("LANG") or ""
    if env.get("LANGUAGE") and lang.split(".")[0] not in ("C", "POSIX", ""):
        return env["LANGUAGE"]
    return lang


def available() -> list[str]:
    """Languages with a catalog, besides English."""
    return sorted(p.stem for p in LOCALE_DIR.glob("*.po"))


def use(setting: str | None = "auto", directory: Path = LOCALE_DIR) -> str:
    """Translate messages into ``setting`` ("auto", "en", "de", "pt_BR" …) from now on; returns
    the language chosen ("en" when there's no catalog for it)."""
    global _current, _language
    wanted = system_language() if setting in (None, "", "auto") else str(setting)
    for code in _codes(wanted):
        if code == "en" or code.startswith("en_"):
            break
        path = directory / f"{code}.po"
        if path.is_file():
            try:
                _current = load(path)
            except (OSError, ValueError) as exc:
                log.warning("messages: can't read %s (%s); using English", path, exc)
                break
            _language = code
            return code
    _current, _language = _ENGLISH, "en"
    return "en"


# -- reading .po files ------------------------------------------------------------------------


def parse_po(text: str) -> dict:
    """A .po file's translations: {msgid: msgstr} and, for plurals, {(msgid, n): msgstr}; the
    header under "". Untranslated and fuzzy entries are left out (English is used for them)."""
    out: dict = {}
    entry: dict = {}  # msgctxt, msgid, msgid_plural, msgstr, and msgstr[n] as {n: text}
    flags: set[str] = set()
    field: tuple | None = None  # where a continuation line ("...") goes

    def finish() -> None:
        nonlocal entry, flags
        msgid = entry.get("msgid")
        if msgid is not None and "fuzzy" not in flags:
            key = f"{entry['msgctxt']}\x04{msgid}" if "msgctxt" in entry else msgid
            forms = entry.get("forms", {})
            if "msgid_plural" in entry:
                if forms and all(forms.values()):
                    for n, value in forms.items():
                        out[(key, n)] = value
            elif entry.get("msgstr"):
                out[key] = entry["msgstr"]
        entry, flags = {}, set()

    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        complete = "msgstr" in entry or "forms" in entry
        if not line:
            continue
        if line.startswith("#"):
            if complete:
                finish()
            if line.startswith("#,"):
                flags |= {f.strip() for f in line[2:].split(",")}
            continue
        try:
            if line.startswith('"'):
                if field is None:
                    raise ValueError("a string with no keyword")
                value = ast.literal_eval(line)
                if field[0] == "forms":
                    entry["forms"][field[1]] += value
                else:
                    entry[field[0]] += value
                continue
            keyword, _sep, rest = line.partition(" ")
            value = ast.literal_eval(rest.strip())
        except (ValueError, SyntaxError) as exc:
            raise ValueError(f"line {number}: {raw!r}") from exc
        if keyword in ("msgctxt", "msgid") and complete:
            finish()
        if keyword.startswith("msgstr[") and keyword.endswith("]"):
            n = int(keyword[7:-1])
            entry.setdefault("forms", {})[n] = value
            field = ("forms", n)
        elif keyword in ("msgctxt", "msgid", "msgid_plural", "msgstr"):
            entry[keyword] = value
            field = (keyword,)
        else:
            raise ValueError(f"line {number}: {raw!r}")
    finish()
    return out


def _mo(catalog: dict) -> bytes:
    """A .mo file's bytes for a parsed catalog, for gettext to read."""
    table: dict[str, str] = {}
    plurals: dict[str, dict[int, str]] = {}
    for key, value in catalog.items():
        if isinstance(key, tuple):
            plurals.setdefault(key[0], {})[key[1]] = value
        else:
            table[key] = value
    # plural entries: msgid\0msgid_plural -> forms joined by \0 (msgid_plural isn't needed to
    # look them up, but gettext keys plurals by the singular, which is what we store)
    for msgid, forms in plurals.items():
        table[msgid + "\0"] = "\0".join(forms[n] for n in sorted(forms))
    keys = sorted(table)
    ids = [k.encode() for k in keys]
    strs = [table[k].encode() for k in keys]
    n = len(keys)
    start = 7 * 4 + 16 * n
    blob, orig, trans = b"", [], []
    for s in ids:
        orig.append((len(s), start + len(blob)))
        blob += s + b"\0"
    for s in strs:
        trans.append((len(s), start + len(blob)))
        blob += s + b"\0"
    head = struct.pack("Iiiiiii", 0x950412DE, 0, n, 7 * 4, 7 * 4 + 8 * n, 0, 0)
    return head + b"".join(struct.pack("ii", *x) for x in orig + trans) + blob


def load(path: Path) -> gettext.GNUTranslations:
    """A .po file as gettext translations."""
    return gettext.GNUTranslations(io.BytesIO(_mo(parse_po(path.read_text(encoding="utf-8")))))
