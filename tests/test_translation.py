import asyncio
import glob
import os
import re
import struct
from pathlib import Path

import pytest

from ekos_notifier import simulate
from ekos_notifier.config import Config
from ekos_notifier.ekos import translation
from ekos_notifier.ekos.adapter import EkosAdapter
from ekos_notifier.ekos.bus import KStarsProcess
from ekos_notifier.events import Event
from ekos_notifier.service import build_rules

HEADER = "Content-Type: text/plain; charset=UTF-8\nPlural-Forms: nplurals=2; plural=(n != 1);\n"


def write_mo(path: Path, entries: dict) -> Path:
    """A gettext catalog: {msgid: msgstr}, or {(msgid, plural): (form0, form1)}."""
    table = {"": HEADER}
    for key, value in entries.items():
        if isinstance(key, tuple):
            table["\0".join(key)] = "\0".join(value)
        else:
            table[key] = value
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
    body = b"".join(struct.pack("ii", *x) for x in orig + trans)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(head + body + blob)
    return path


def translator(tmp_path, entries, language="xx"):
    return translation.load(write_mo(tmp_path / "kstars.mo", entries), language)


# -- matching -------------------------------------------------------------------------------


def test_lines_come_back_in_english_with_their_values(tmp_path):
    bt = translator(
        tmp_path,
        {
            "Autofocus failed.": "Die automatische Fokussierung ist fehlgeschlagen.",
            "Guiding deviation %1 is now lower than limit value of %2 arcsecs.": (
                "Grenzwert %2 unterschritten: Abweichung %1."  # the order swapped
            ),
            "Job '%1' is now approaching: '%1'.": "Auftrag '%1' kommt: '%1'.",  # repeated
            "OK": "Gut",  # too short to match on
        },
    )
    assert bt.english("Die automatische Fokussierung ist fehlgeschlagen.") == "Autofocus failed."
    assert bt.english("Grenzwert 1.50 unterschritten: Abweichung 0.82.") == (
        "Guiding deviation 0.82 is now lower than limit value of 1.50 arcsecs."
    )
    assert bt.english("Auftrag 'M 31' kommt: 'M 31'.") == "Job 'M 31' is now approaching: 'M 31'."
    assert bt.english("Auftrag 'M 31' kommt: 'M 42'.") == "Auftrag 'M 31' kommt: 'M 42'."
    assert bt.english("Gut") == "Gut"
    assert bt.english("Not a translation.") == "Not a translation."


def test_the_camera_prefix_is_kept_and_the_most_specific_translation_wins(tmp_path):
    bt = translator(
        tmp_path,
        {
            "Autofocus failed.": "Fokus fehlgeschlagen.",
            "%1 failed.": "%1 fehlgeschlagen.",
            "Capture failed.": "Aufnahme fehlgeschlagen.",
        },
    )
    assert (
        bt.english("[CCD Simulator] Fokus fehlgeschlagen.") == "[CCD Simulator] Autofocus failed."
    )
    assert bt.english("Aufnahme fehlgeschlagen.") == "Capture failed."
    assert bt.english("Ausrichtung fehlgeschlagen.") == "Ausrichtung failed."


def test_plural_entries(tmp_path):
    bt = translator(tmp_path, {("%1 frame left", "%1 frames left"): ("%1 Bild übrig", "%1 Bilder")})
    assert bt.english("3 Bilder") == "3 frame left"  # KStars's source string, as matched
    assert len(bt) == 2


# -- the running KStars's language ----------------------------------------------------------


def kstars(root, **env):
    return KStarsProcess(1, {"HOME": "/home/me", **env}, root)


def test_languages_from_the_environment_and_the_override(tmp_path):
    assert translation.languages(kstars(tmp_path, LANG="de_DE.UTF-8")) == ["de_DE", "de"]
    assert translation.languages(kstars(tmp_path, LANGUAGE="fr:de", LANG="fr_FR.UTF-8")) == [
        "fr",
        "de",
    ]
    assert translation.languages(kstars(tmp_path, LANGUAGE="fr", LANG="C")) == []
    assert translation.languages(kstars(tmp_path, LC_ALL="pt_BR.UTF-8", LANG="de_DE")) == [
        "pt_BR",
        "pt",
    ]
    rc = tmp_path / "home/me/.config/klanguageoverridesrc"
    rc.parent.mkdir(parents=True)
    rc.write_text("[Language]\nkstars=it\n")
    assert translation.languages(kstars(tmp_path, LANG="de_DE.UTF-8")) == ["it"]
    rc.write_text("[Language]\nkstars=@ByteArray(de)\n")  # as Help > Switch Language writes it
    assert translation.languages(kstars(tmp_path, LANG="en_US.UTF-8")) == ["de"]


def test_the_catalog_is_found_in_a_flatpak_or_natively(tmp_path):
    flatpak = tmp_path / "flatpak"
    mo = write_mo(flatpak / "app/share/locale/de/LC_MESSAGES/kstars.mo", {"Abort": "Abbruch"})
    assert translation.find_catalog(kstars(flatpak, LANG="de_DE.UTF-8")) == ("de", mo)
    native = tmp_path / "native"
    mo = write_mo(native / "usr/share/locale/pt_BR/LC_MESSAGES/kstars.mo", {"Abort": "Abortar"})
    assert translation.find_catalog(kstars(native, LANG="pt_BR.UTF-8")) == ("pt_BR", mo)
    assert translation.find_catalog(kstars(native, LANG="en_GB.UTF-8")) is None
    assert translation.find_catalog(kstars(native, LANG="sv_SE.UTF-8")) is None  # no catalog


# -- the adapter ----------------------------------------------------------------------------


def test_the_adapter_hands_rules_english_and_keeps_the_original(tmp_path):
    bt = translator(tmp_path, {"Autofocus failed.": "Fokus fehlgeschlagen."}, "de")
    out = []
    adapter = EkosAdapter(out.append, language=lambda: bt)
    asyncio.run(adapter.pick_language())
    adapter._emit(Event(1.0, "ekos.log", {"module": "Focus", "text": "Fokus fehlgeschlagen."}))
    adapter._emit(Event(2.0, "ekos.log", {"module": "Focus", "text": "Autofocus complete."}))
    adapter._emit(Event(3.0, "focus.state", {"state": "failed"}))
    assert [e.data for e in out] == [
        {"module": "Focus", "text": "Autofocus failed.", "original": "Fokus fehlgeschlagen."},
        {"module": "Focus", "text": "Autofocus complete."},
        {"state": "failed"},
    ]


# -- a whole night in another language ------------------------------------------------------

NUMBER = re.compile(r"\d+(?:\.\d+)?")
PREFIX = re.compile(r"^\[[^\]]*\]\s*")


def numbered(text: str) -> str:
    """The text with its numbers as placeholders: %1, %2 …"""
    pieces = NUMBER.split(text)
    return "".join(p + (f"%{i}" if i < len(pieces) else "") for i, p in enumerate(pieces, 1))


def foreign(english: str) -> str:
    """A made-up language: different text, the same placeholders, in reverse order."""
    pieces = re.split(r"(%\d+)", english)
    return "»" + "".join(p if p.startswith("%") else p.swapcase() for p in reversed(pieces)) + "«"


class Foreign:
    """English Ekos log lines in the made-up language, with the catalog that translates them
    (each line's numbers become its placeholders, as KStars's strings have them)."""

    def __init__(self) -> None:
        self.catalog: dict[str, str] = {}

    def line(self, text: str) -> str:
        prefix = PREFIX.match(text)
        body = text[prefix.end() :] if prefix else text
        template = numbered(body)
        self.catalog[template] = translated = foreign(template)
        for i, value in enumerate(NUMBER.findall(body), start=1):
            translated = translated.replace(f"%{i}", value, 1)
        return (prefix.group(0) if prefix else "") + translated

    def events(self, events):
        return [
            Event(
                e.ts, e.kind, {**e.data, "text": self.line(e.data["text"])}, e.source, e.session_id
            )
            if e.kind == "ekos.log"
            else e
            for e in events
        ]


def back(events, bt):
    return [
        Event(e.ts, e.kind, {**e.data, "text": bt.english(e.data["text"])}, e.source, e.session_id)
        if e.kind == "ekos.log"
        else e
        for e in events
    ]


def same_in_another_language(tmp_path, events, outcome, *, bites=True):
    """``outcome(events)`` is the same with the log in another language, read back through its
    catalog; and (``bites``) different without that, so the scenario really reads the log."""
    english = outcome(events)
    f = Foreign()
    raw = f.events(events)
    if bites:
        assert outcome(raw) != english, "this scenario doesn't depend on Ekos's wording"
    assert outcome(back(raw, translator(tmp_path, f.catalog))) == english


def alert_texts(alerts):
    return [(a.data["rule"], a.data["title"], a.data["body"]) for a in alerts]


def test_a_whole_night_in_another_language_raises_the_same_alerts(tmp_path):
    def outcome(events):
        rules = build_rules(Config(), live=False)
        return alert_texts(simulate.run(simulate.Night("x", events), rules))

    # The sample night has nothing that hinges on the log's wording: this checks only that
    # translating every line back changes nothing.
    same_in_another_language(tmp_path, simulate.sample().events, outcome, bites=False)


def test_autofocus_failed_in_another_language(tmp_path):
    import test_capture_alerts as c

    events = c.START + c.failed_run_live(10) + c.failed_run_live(100) + c.failed_run_live(200)
    same_in_another_language(tmp_path, events, lambda e: c.alerts(e, until=300))


def test_capture_aborted_reason_in_another_language(tmp_path):
    import test_capture_alerts as c

    events = (
        c.START
        + c.exposing(10)
        + [
            c.log(100, "[CCD Simulator] Autofocus failed. Aborting exposure"),
            c.ev(100, "capture.state", state="aborted"),
        ]
    )
    same_in_another_language(tmp_path, events, lambda e: c.alerts(e, until=300))


def test_guiding_poor_in_another_language(tmp_path):
    import test_capture_alerts as c

    events = c.START + c.exposing(10) + c.flapping(20, 700)
    same_in_another_language(
        tmp_path, events, lambda e: c.alerts(e, until=800, rules=c.Rules(guiding_poor_window=600))
    )


def test_plate_solve_failed_in_another_language(tmp_path):
    import test_align as a

    events = [a.START, *a.failed_run(200, reason="Solver Failed.", end="aborted")]
    same_in_another_language(
        tmp_path,
        events,
        lambda e: alert_texts(a.timeline(a.PlateSolveRule(), e, until=261 + a.RETRY_GRACE)),
    )


def test_meridian_flip_failed_in_another_language(tmp_path):
    import test_disk_flip as d

    events = [*d.tracking(), d.mount_log(d.T0 + 700, "Meridian flip failed, pier side not changed")]
    same_in_another_language(tmp_path, events, lambda e: alert_texts(d.timeline(d.sky(), e)))


def test_your_own_ekos_stop_in_another_language(tmp_path):
    import test_session as s

    events = [
        s.ev(0, "capture.frame", type="light"),
        s.ev(100, "ekos.log", module="Ekos", text="Disconnecting INDI devices..."),
        s.ev(100, "indi.state", state="idle"),
        s.ev(102, "ekos.state", state="idle"),
    ]

    def outcome(events):
        m = s.manager(grace=600)
        out = s.feed(m, *events) + m.tick(s.EVENING + 700)
        return s.lifecycle(out)

    same_in_another_language(tmp_path, events, outcome)


def test_power_outputs_off_at_shutdown_in_another_language(tmp_path):
    import test_power as p

    events = [
        p.START,
        p.output(10, True, 1.2),
        p.ev(20, "ekos.log", module="Ekos", text="Disconnecting INDI devices..."),
        p.output(22, False, 0.0),
    ]
    same_in_another_language(
        tmp_path, events, lambda e: alert_texts(p.run(p.PowerRule(), e, until=100))
    )


# -- the real German catalog, when this computer has KStars's Flatpak -------------------------

GERMAN = glob.glob(
    os.path.expanduser(
        "~/.local/share/flatpak/app/org.kde.kstars/*/*/*/files/share/locale/de/LC_MESSAGES/kstars.mo"
    )
)


@pytest.mark.skipif(not GERMAN, reason="no KStars Flatpak with German translations here")
def test_kstars_german_lines_our_rules_read():
    import gettext

    bt = translation.load(Path(GERMAN[0]), "de")
    german = gettext.GNUTranslations(open(GERMAN[0], "rb"))  # noqa: SIM115
    for english in (
        "Autofocus failed.",
        "Restarting autofocus process...",
        "Solver Failed.",
        "Disconnecting INDI devices...",
        "Meridian flip inactive (parked)",
        "Warning: Dithering failed.",
    ):
        assert german.gettext(english) != english
        assert bt.english(german.gettext(english)) == english


# -- numbers in KStars's locale --------------------------------------------------------------

DE = {"numbers": {"decimal": ",", "group": "."}}
FR = {"numbers": {"decimal": ",", "group": "\u202f"}}
UNKNOWN = {"numbers": None}  # KStars's locale isn't known on this computer


@pytest.mark.parametrize(
    ("text", "data", "value"),
    [
        # Ten seconds and ten thousand seconds (KStars writes exposures with three decimals)
        ("10.000", {}, 10.0),  # a line with no format: English
        ("10,000.000", {}, 10000.0),
        ("10,000", DE, 10.0),
        ("10.000,000", DE, 10000.0),
        ("10\u202f000,000", FR, 10000.0),
        # What the format says decides, not the look: a bare 10,000 in English is ten thousand
        ("10,000", {}, 10000.0),
        ("10.000", DE, 10000.0),
        ("0,5", DE, 0.5),
        ("-12,5", DE, -12.5),
        ("-10", DE, -10.0),
        ("1.5", DE, None),  # not German
        ("10,000", UNKNOWN, None),  # never guessed
    ],
)
def test_localized_numbers_are_read_in_kstars_format(text, data, value):
    assert re.fullmatch(translation.NUMBER, text)
    assert translation.localized(text, data) == value


def test_plain_numbers_are_the_same_everywhere():
    assert translation.plain("0.820") == 0.82
    assert translation.plain("-1.5") == -1.5
    assert translation.plain("0,820") is None


def test_the_number_format_comes_from_kstars_locale():
    assert translation.numeric_locale({"LANG": "de_DE.UTF-8"}) == "de_DE.UTF-8"
    assert translation.numeric_locale({"LC_NUMERIC": "fr_FR.UTF-8", "LANG": "de_DE"}) == (
        "fr_FR.UTF-8"
    )
    assert translation.numeric_locale({"LC_ALL": "C", "LC_NUMERIC": "fr_FR"}) == "C"
    assert translation.numeric_locale({}) == "C"
    assert translation.number_format("C.UTF-8") == translation.C
    assert translation.number_format("xx_YY.UTF-8") is None  # no such locale here


@pytest.mark.skipif(
    "de_DE.utf8" not in os.popen("locale -a 2>/dev/null").read(), reason="no de_DE locale here"
)
def test_german_from_the_c_library():
    assert translation.number_format("de_DE.UTF-8") == translation.NumberFormat(",", ".")


def test_the_adapter_stamps_the_format_on_log_lines():
    line = Event(1.0, "ekos.log", {"module": "Capture", "text": "x"}, "ekos")
    de = translation.with_numbers(line, translation.NumberFormat(",", "."))
    assert de.data["numbers"] == {"decimal": ",", "group": "."}
    assert translation.with_numbers(line, translation.ENGLISH) is line
    assert translation.with_numbers(line, None).data["numbers"] is None
    assert translation.with_numbers(de, translation.ENGLISH) is de  # already stamped (replays)


def test_rules_read_each_number_as_kstars_writes_it():
    from ekos_notifier import capture, conditions, digest, guiding

    m = capture.EXPOSURE.search("[CCD Simulator] Capturing 10,000-second Luminance image...")
    assert translation.localized(m.group(1), DE) == 10.0
    assert capture.ROUTINE.search("Capturing 1.200,000-second Ha image...")
    m = digest.JOB_PLAN.search("Job requires 10,000-second Luminance images, has 0/10 frames")
    assert m and translation.localized(m.group(1), DE) == 10.0
    m = guiding.PoorGuidingRule.EXCEEDED.search(
        "Guiding deviation 1.234 exceeded limit value of 0,5 arcsecs"
    )
    # the deviation is written plainly, the limit in KStars's locale
    assert m and (translation.plain(m.group(1)), translation.localized(m.group(2), DE)) == (
        1.234,
        0.5,
    )
    m = conditions.SETPOINT.search("Setting temperature to -12,5 °C...")
    assert m and translation.localized(m.group(1), DE) == -12.5


def test_an_unknown_format_leaves_the_exposure_unread():
    from ekos_notifier import capture

    rule = capture.CaptureRule()
    text = "[CCD Simulator] Capturing 10,000-second Luminance image..."
    rule.on_event(Event(1.0, "ekos.log", {"module": "Capture", "text": text, **UNKNOWN}, "ekos"))
    assert rule._exposure == 0.0  # noqa: SLF001 - unknown, not 10 000
    rule.on_event(Event(2.0, "ekos.log", {"module": "Capture", "text": text, **DE}, "ekos"))
    assert rule._exposure == 10.0  # noqa: SLF001


# -- a recording made in another language ---------------------------------------------------


def test_a_recordings_language_is_found_among_the_installed_catalogs(tmp_path):
    write_mo(tmp_path / "de/LC_MESSAGES/kstars.mo", {"Solver Failed.": "Gescheitert."})
    found = translation.for_language("de_DE.UTF-8", dirs=(str(tmp_path),))
    assert found and found.language == "de" and found.english("Gescheitert.") == "Solver Failed."
    assert translation.for_language("fr", dirs=(str(tmp_path),)) is None
    assert translation.for_language("en", dirs=(str(tmp_path),)) is None
    assert translation.for_language(None, dirs=(str(tmp_path),)) is None


def test_phd2_advice_is_recognised_in_its_translations(tmp_path):
    write_mo(
        tmp_path / "de_DE/LC_MESSAGES/phd2.mo",
        {"For best results, use a Dark Library": "Für beste Ergebnisse %s verwenden."},
    )
    write_mo(tmp_path / "fr/LC_MESSAGES/phd2.mo", {"Star lost": "Étoile perdue"})
    assert translation.translated_prefixes(("For best results",), "phd2", (str(tmp_path),)) == (
        "For best results",
        "Für beste Ergebnisse",
    )


# KStars 3.8.5 (Flatpak) in German on the simulators, recorded 2026-11-19: a failing autofocus
# (setup, before the session), a failing plate solve, a short guided sequence stopped by hand,
# PHD2 (also German) sending its dark-library tip, then the user's Disconnect. The German lines
# that matter and their English sources, from KStars's own catalog:
GERMAN_NIGHT = (
    Path(__file__).parent / "fixtures/replay/sim-german-failures-kstars-3.8.5-flatpak.ndjson.gz"
)
GERMAN_LINES = {
    "Autofocus failed, moving back to initial focus position %1.": (
        "Die automatische Fokussierung ist fehlgeschlagen, zurück zur ursprünglichen "
        "Fokus-Position %1."
    ),
    "Autofocus failed. Aborting exposure...": (
        "Die automatische Fokussierung ist fehlgeschlagen, die Belichtung wird abgebrochen ..."
    ),
    "Autoguiding aborted.": "Die automatische Nachführung wurde abgebrochen.",
    "Autoguiding stopped. Aborting...": (
        "Die automatische Nachführung wurde angehalten. Abbruch ..."
    ),
    "Disconnecting INDI devices...": "Verbindung zu INDI-Geräten wird getrennt ...",
    "Restarting autofocus process...": "Automatische Fokussierung neu starten ...",
    "Solver Failed.": "Die Lösung ist fehlgeschlagen.",
}
PHD2_TIP = "Für beste Ergebnisse während der Nachführung"


def german_night(monkeypatch, translator):
    from ekos_notifier import guiding

    monkeypatch.setattr(guiding, "phd2_advice", lambda: ("For best results", PHD2_TIP))
    monkeypatch.setattr(translation, "for_language", lambda language: translator)
    night = simulate.recording(GERMAN_NIGHT)
    return night, [
        (a.data["title"], a.data["body"])
        for a in simulate.run(night, build_rules(Config(), live=False))
    ]


def test_the_german_night_raises_what_an_english_one_would(monkeypatch, tmp_path):
    night, alerts = german_night(monkeypatch, translator(tmp_path, GERMAN_LINES, "de"))
    assert "KStars in 'de', read through its translations" in night.label
    assert [t for t, _ in alerts] == ["Starting session", "Plate solve failed", "Capture aborted"]
    body = dict(alerts)
    assert "“Solver Failed.”" in body["Plate solve failed"]
    assert "Autoguiding stopped." in body["Capture aborted"]
    # Your own Disconnect, understood: no session summary for a night you ended yourself.


def test_the_german_night_without_its_translations(monkeypatch):
    night, alerts = german_night(monkeypatch, None)
    assert "whose translations aren't installed here" in night.label
    titles = [t for t, _ in alerts]
    # Disconnecting wasn't understood: the night ends as if Ekos had been lost.
    assert "Session summary" in titles
    assert "Ekos: “Solver Failed.”" not in dict(alerts)["Plate solve failed"]


@pytest.mark.skipif(not GERMAN, reason="no KStars Flatpak with German translations here")
def test_the_german_night_through_kstars_own_catalog(monkeypatch):
    from ekos_notifier import guiding

    monkeypatch.setattr(guiding, "phd2_advice", lambda: ("For best results", PHD2_TIP))
    night = simulate.recording(GERMAN_NIGHT)
    alerts = simulate.run(night, build_rules(Config(), live=False))
    assert [a.data["title"] for a in alerts] == [
        "Starting session",
        "Plate solve failed",
        "Capture aborted",
    ]


def test_the_german_nights_exposures_are_ten_seconds():
    from ekos_notifier import capture

    night = simulate.recording(GERMAN_NIGHT)
    exposures = {
        translation.localized(m.group(1), e.data)
        for e in night.events
        if e.kind == "ekos.log" and (m := capture.EXPOSURE.search(e.data["text"]))
    }
    assert exposures == {10.0}  # "Capturing 10,000-second", written in de_DE
