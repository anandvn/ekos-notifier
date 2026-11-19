"""Ekos Notifier's own messages in other languages (ekos_notifier.i18n, tools/update_pot.py)."""

import ast
import importlib.util
from pathlib import Path

import pytest

from ekos_notifier import config, i18n, simulate
from ekos_notifier.config import Config
from ekos_notifier.service import build_rules

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "ekos_notifier"
FIXTURES = ROOT / "tests" / "fixtures" / "replay"

spec = importlib.util.spec_from_file_location("update_pot", ROOT / "tools" / "update_pot.py")
update_pot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update_pot)

GERMAN = """\
# German
msgid ""
msgstr ""
"Content-Type: text/plain; charset=UTF-8\\n"
"Plural-Forms: nplurals=2; plural=(n != 1);\\n"

#: ekos_notifier/align.py
#, python-brace-format
msgid "Ekos solved the frame at {time}."
msgstr "Ekos hat das Bild um {time} gelöst."

#, fuzzy
msgid "Plate solve failed"
msgstr "Lösung fehlgeschlagen"

msgid "{n} dither failed"
msgid_plural "{n} dithers failed"
msgstr[0] "{n} Dither fehlgeschlagen"
msgstr[1] ""
"{n} Dithers "
"fehlgeschlagen"

msgctxt "frame type"
msgid "dark"
msgstr "Dunkelbild"

msgid "Battery low"
msgstr ""
"""


@pytest.fixture
def german(tmp_path):
    (tmp_path / "de.po").write_text(GERMAN, encoding="utf-8")
    yield tmp_path
    i18n.use("en")


# -- reading catalogs and choosing the language ---------------------------------------------


def test_a_po_file_is_read_as_gettext_reads_it(german):
    assert i18n.use("de", german) == "de"
    assert i18n._("Ekos solved the frame at {time}.").format(time="22:00") == (
        "Ekos hat das Bild um 22:00 gelöst."
    )
    assert (
        i18n.ngettext("{n} dither failed", "{n} dithers failed", 1) == "{n} Dither fehlgeschlagen"
    )
    assert (
        i18n.ngettext("{n} dither failed", "{n} dithers failed", 3) == "{n} Dithers fehlgeschlagen"
    )
    assert i18n._("Plate solve failed") == "Plate solve failed"  # fuzzy: not trusted yet
    assert i18n._("Battery low") == "Battery low"  # untranslated: English
    assert i18n._("dark") == "dark"  # only the "frame type" context is translated
    assert i18n.language() == "de"


def test_the_language_follows_the_setting_or_the_system(german, monkeypatch):
    assert i18n.use("de_AT.UTF-8", german) == "de"  # the region falls back to the language
    assert i18n.use("en", german) == "en"
    assert i18n.use("fr", german) == "en"  # no catalog: English
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    assert i18n.use("auto", german) == "de"
    monkeypatch.setenv("LANGUAGE", "fr:de")
    assert i18n.use(None, german) == "de"
    monkeypatch.setenv("LC_ALL", "C")  # gettext ignores LANGUAGE under the C locale
    assert i18n.use("auto", german) == "en"


def test_a_broken_catalog_falls_back_to_english(tmp_path, caplog):
    (tmp_path / "de.po").write_text('msgid "x"\nnot a po line\n', encoding="utf-8")
    assert i18n.use("de", tmp_path) == "en"
    assert "can't read" in caplog.text


def test_ekos_words_are_translated_and_others_kept():
    assert i18n.word("in_progress") == "in progress"
    assert i18n.word("unparked") == "unparked"
    assert i18n.word("Luminance") == "Luminance"


# -- the template and the translations ------------------------------------------------------


def test_the_template_is_up_to_date():
    pot = (PACKAGE / "locale" / "ekos-notifier.pot").read_text(encoding="utf-8")
    assert pot == update_pot.pot(update_pot.extract()), "run tools/update_pot.py"


def test_every_translation_keeps_the_placeholders():
    found = update_pot.extract()
    for po in sorted((PACKAGE / "locale").glob("*.po")):
        assert update_pot.problems(po, found) == []


def test_a_wrong_placeholder_is_caught(tmp_path):
    po = tmp_path / "de.po"
    po.write_text(
        GERMAN.replace("um {time} gelöst", "um {zeit} gelöst") + '\nmsgid "Gone"\nmsgstr "Weg"\n',
        encoding="utf-8",
    )
    found = update_pot.problems(po)
    assert any("{zeit}" in p or "'zeit'" in p for p in found)
    assert any("'Gone' isn't a message any more" in p for p in found)


def test_no_function_hides_the_translation_function():
    """``_`` assigned in a function (``_, x = …``) makes ``_("…")`` fail there at runtime."""
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports_it = any(
            isinstance(n, ast.ImportFrom) and any(a.name == "_" for a in n.names)
            for n in ast.walk(tree)
        )
        if not imports_it:
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                    continue
                if (
                    isinstance(node, ast.Name)
                    and node.id == "_"
                    and isinstance(node.ctx, ast.Store)
                ):
                    # a comprehension's own variable has its own scope; anything else doesn't
                    in_comprehension = any(
                        isinstance(c, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp))
                        and any(node is t for g in c.generators for t in ast.walk(g.target))
                        for c in ast.walk(fn)
                    )
                    assert in_comprehension, f"{path.name}:{node.lineno} assigns _ in {fn.name}()"


# -- every message is translatable ----------------------------------------------------------


def pseudo_catalog(directory):
    """A made-up language: every message in ⟪…⟫, its placeholders kept."""
    lines = [
        'msgid ""',
        'msgstr "Content-Type: text/plain; charset=UTF-8\\nPlural-Forms: nplurals=2; '
        'plural=(n != 1);\\n"',
        "",
    ]
    q = update_pot.quote
    for (msgid, plural), _where in update_pot.extract().items():
        lines.append(f"msgid {q(msgid)}")
        if plural is None:
            lines.append(f"msgstr {q('⟪' + msgid + '⟫')}")
        else:
            lines += [
                f"msgid_plural {q(plural)}",
                f"msgstr[0] {q('⟪' + msgid + '⟫')}",
                f"msgstr[1] {q('⟪' + plural + '⟫')}",
            ]
        lines.append("")
    (directory / "xx.po").write_text("\n".join(lines), encoding="utf-8")


@pytest.mark.parametrize(
    "night",
    [None, *sorted(p.name for p in FIXTURES.glob("*.ndjson.gz"))],
    ids=lambda n: n or "sample",
)
def test_every_message_of_a_night_is_translated(night, tmp_path):
    pseudo_catalog(tmp_path)
    replay = simulate.sample() if night is None else simulate.recording(FIXTURES / night)
    rules = build_rules(Config(), live=False)
    i18n.use("xx", tmp_path)
    try:
        alerts = simulate.run(replay, rules)
    finally:
        i18n.use("en")
    assert alerts
    for a in alerts:
        title, body = a.data["title"], a.data["body"]
        assert "⟪" in title, f"{a.data['rule']}: title {title!r} isn't translatable"
        # Every line carries translated words, except lines that are only data: the digest's
        # target and filter, and PHD2's own message (in PHD2's language).
        for line in body.split("\n"):
            data_only = a.data["rule"] in ("digest", "phd2_warning") and "⟪" not in line
            assert "⟪" in line or "⟫" in line or data_only or not line.strip(), (
                f"{a.data['rule']}: {line!r} isn't translatable"
            )


# -- the setting ----------------------------------------------------------------------------


def test_the_language_setting(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("language: de\n")
    cfg = config.load(path)
    assert cfg.language == "de"
    if "de" not in i18n.available():
        assert any("has no translation yet" in w for w in cfg.warnings)
    path.write_text("language: auto\n")
    assert config.load(path).language == "auto" and config.load(path).warnings == []
    path.write_text("language: German\n")
    with pytest.raises(config.ConfigError, match="language code"):
        config.load(path)


def test_rules_are_built_in_the_configs_language(german, monkeypatch):
    monkeypatch.setattr(i18n, "LOCALE_DIR", german)
    monkeypatch.setattr(
        i18n, "use", lambda setting="auto", directory=german, _use=i18n.use: _use(setting, german)
    )
    build_rules(Config(language="de"), live=False)
    assert i18n.language() == "de"
    build_rules(Config(language="en"), live=False)
    assert i18n.language() == "en"
