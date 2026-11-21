import ast
import re
from pathlib import Path

import pytest

from ekos_notifier.config import ConfigError, load
from ekos_notifier.events import Event
from ekos_notifier.messages import ALERTS, MessageSettings, SettingsError
from ekos_notifier.notify import should_send

SRC = Path(__file__).parent.parent / "src/ekos_notifier"
DOCS = Path(__file__).parent.parent / "docs"


def title_of(call):
    node = call.args[4] if len(call.args) > 4 else None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):  # _("…").format()
        node = node.func.value
    if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
        return node.args[0].value
    return None


def raised_in_source():
    """(rule, priority, immediate, title, file) for every alert call with literal arguments, and
    every rule name the source mentions as a string. The title is None unless it's one literal
    string (``_("Weather alert")``, or ``_("Power output back: {output}").format(...)``)."""
    calls, names = [], set()
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                names.add(node.value)
            if not isinstance(node, ast.Call):
                continue
            func = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", "")
            )
            if not func.endswith("_alert") or len(node.args) < 4:
                continue
            rule, priority = node.args[2], node.args[3]
            if isinstance(rule, ast.Constant) and isinstance(priority, ast.Constant):
                immediate = any(
                    k.arg == "immediate" and isinstance(k.value, ast.Constant) and k.value.value
                    for k in node.keywords
                )
                calls.append((rule.value, priority.value, immediate, title_of(node), path.name))
    return calls, names


def test_registry_matches_every_alert_the_code_raises():
    calls, names = raised_in_source()
    assert calls, "found no alert calls"
    for rule, priority, immediate, _title, where in calls:
        assert rule in ALERTS, f"{where}: '{rule}' missing from messages.ALERTS"
        alert = ALERTS[rule]
        assert (alert.priority, alert.immediate) == (priority, immediate), f"{where}: {rule}"
    # Rules raised with a computed name are still named somewhere in the source.
    for rule in ALERTS:
        assert rule in names, f"'{rule}' is in the registry but the code never raises it"
    assert {r for r, *_ in calls} <= set(ALERTS)


# docs/alerts.md, the Alerts reference: each section, and what an alert listed in it must be.
SECTIONS = {
    "Emergency": lambda a: a.priority == "emergency",
    "High": lambda a: a.priority == "high",
    "Normal, sent at once": lambda a: a.priority == "normal" and a.immediate and not a.clears,
    "All-clears": lambda a: a.clears is not None,
    "Quiet": lambda a: a.priority == "quiet",
}


def documented_alerts():
    """name -> (section, title, default) for every row of the Alerts reference."""
    rows, section = [], None
    for line in (DOCS / "alerts.md").read_text().splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
        elif m := re.match(r"^\| ([^|]+?) \| `([a-z_0-9]+)` \| ([a-z]+) \|", line):
            rows.append((m[2], (section, m[1], m[3])))
    documented = dict(rows)
    assert len(rows) == len(documented), "an alert is listed twice in docs/alerts.md"
    return documented


def test_the_alerts_reference_matches_the_registry_and_the_code():
    """docs/alerts.md lists every alert, in the section for its priority, with its default
    and the title the code sends it with (up to its first variable part)."""
    documented = documented_alerts()
    assert set(documented) == set(ALERTS), (
        f"docs/alerts.md is missing {sorted(set(ALERTS) - set(documented))}, "
        f"and lists unknown {sorted(set(documented) - set(ALERTS))}"
    )
    for name, (section, _title, default) in documented.items():
        alert = ALERTS[name]
        assert section in SECTIONS, f"docs/alerts.md: {name} is under an unknown '## {section}'"
        assert SECTIONS[section](alert), f"docs/alerts.md: {name} doesn't belong under {section}"
        assert default == alert.default, f"docs/alerts.md: {name}'s default is {alert.default}"
    calls, _names = raised_in_source()
    for rule, _priority, _immediate, sent, where in calls:
        if sent is None:
            continue
        title = documented[rule][1]
        assert sent == title or sent.startswith(title + ":"), (
            f"{where} sends {rule} as '{sent}', docs/alerts.md calls it '{title}'"
        )


def alert(rule, priority, immediate=False):
    data = {"rule": rule, "priority": priority, "title": rule, "body": "", "immediate": immediate}
    return Event(0, "alert", data, "rules", "s1")


def applied(settings, *alerts):
    return [a.data for a in MessageSettings.parse(settings).apply(alerts)]


def test_defaults_leave_alerts_as_raised():
    a = alert("dew_risk", "high")
    assert MessageSettings().apply([a]) == [a]


def test_levels():
    [off, held, quiet, up] = applied(
        {
            "phd2_warning": "off",
            "dew_risk": "digest",
            "target_low": "quiet",
            "disk_low": "emergency",
        },
        alert("phd2_warning", "high"),
        alert("dew_risk", "high"),
        alert("target_low", "normal", True),
        alert("disk_low", "high"),
    )
    assert off["off"] and not should_send(off)
    assert (held["priority"], held["immediate"], held["default_priority"]) == (
        "normal",
        False,
        "high",
    )
    assert not should_send(held)
    assert (quiet["priority"], quiet["immediate"]) == ("quiet", True) and should_send(quiet)
    assert up["priority"] == "emergency"


def test_all_clears_follow_their_problem_when_it_is_off_or_held():
    s = MessageSettings.parse(
        {"guiding_lost": "off", "battery_low": "digest", "disk_low": "normal"}
    )
    assert s.level("guiding_recovered") == "off"
    assert s.level("battery_ok") == "digest"
    assert s.level("disk_ok") == "normal"  # its own default; the problem is just lowered
    assert MessageSettings.parse({"battery_critical": "off"}).level("battery_ok") == "off"


def test_attention_digest_is_never_below_normal():
    [calm, attention] = applied(
        {"digest": "quiet"}, alert("digest", "quiet", True), alert("digest", "normal", True)
    )
    assert calm["priority"] == "quiet" and attention["priority"] == "normal"
    [off] = applied({"digest": "off"}, alert("digest", "normal", True))
    assert not should_send(off)


def test_bad_settings_are_explained():
    with pytest.raises(SettingsError, match="did you mean 'dew_risk'"):
        MessageSettings.parse({"dew_risks": "off"})
    with pytest.raises(SettingsError, match="must be one of off"):
        MessageSettings.parse({"dew_risk": "loud"})
    with pytest.raises(SettingsError):
        MessageSettings.parse({"digest": "digest"})
    assert MessageSettings.parse({"dew_risk": False}).level("dew_risk") == "off"  # YAML `off`


def test_lowered_emergencies_are_listed():
    s = MessageSettings.parse({"meridian_flip_overdue": "high"})
    assert s.lowered_emergencies() == ["meridian_flip_overdue"]


def test_config_messages_section(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("messages:\n  dew_risk: digest\n  phd2_warning: off\n")
    cfg = load(path)
    assert cfg.messages.level("dew_risk") == "digest"
    assert cfg.messages.level("phd2_warning") == "off"  # YAML reads a bare off as false
    path.write_text("messages:\n  nope: off\n")
    with pytest.raises(ConfigError, match="messages: unknown alert 'nope'"):
        load(path)
    path.write_text("messages: loud\n")
    with pytest.raises(ConfigError):
        load(path)


def test_every_alert_has_a_one_line_description():
    for name, a in ALERTS.items():
        assert a.what and "\n" not in a.what and len(a.what) < 60, name
        assert re.fullmatch(r"[a-z0-9_]+", name)


def test_settings_reach_the_whole_pipeline():
    from ekos_notifier import simulate
    from ekos_notifier.config import Config
    from ekos_notifier.service import build_rules

    cfg = Config(messages=MessageSettings.parse({"ekos_stopped_mount_unparked": "high"}))
    alerts = simulate.run(simulate.sample(), build_rules(cfg, live=False))
    emergency = next(a for a in alerts if a.data["rule"] == "ekos_stopped_mount_unparked")
    assert (emergency.data["priority"], emergency.data["default_priority"]) == ("high", "emergency")


def test_digest_lists_held_problems_but_not_ones_turned_off():
    from ekos_notifier.digest import DigestRule

    digest = DigestRule(min_session=0)
    digest.handle(Event(0, "session.started", {"night": "n"}, "session", "s1"))
    s = MessageSettings.parse({"dew_risk": "digest", "phd2_warning": "off"})
    digest.note_alerts(s.apply([alert("dew_risk", "high"), alert("phd2_warning", "high")]))
    [out] = digest.tick(3600)
    assert out.data["title"].endswith("Attention: dew_risk")
    assert "phd2_warning" not in out.data["body"]


def test_cli_config_alerts_and_check(tmp_path, capsys):
    from ekos_notifier.cli import main

    path = tmp_path / "config.yaml"
    path.write_text("messages:\n  dew_risk: digest\n  battery_critical: high\n")
    assert main(["config", "alerts", "--config", str(path)]) == 0
    out = capsys.readouterr().out
    assert re.search(r"dew_risk\s+high\s+digest\s+Dew margin", out)
    assert re.search(r"dew_risk_cleared\s+digest\s+Conditions", out)  # same as default: blank
    assert main(["config", "check", "--config", str(path)]) == 0
    out = capsys.readouterr().out
    assert "message dew_risk: digest" in out
    assert "note: battery_critical is an Emergency by default; you've set it to high" in out
