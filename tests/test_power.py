import gzip
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ekos_notifier.config import ConfigError, load
from ekos_notifier.digest import DigestRule
from ekos_notifier.events import Event
from ekos_notifier.indi.decode import IndiDecoder
from ekos_notifier.indi.presets import load_builtin
from ekos_notifier.power import DEAD_AFTER, FAULT_HOLD, PowerRule
from ekos_notifier.rules import Rules

# Recorded from a test driver built on libindi 2.2.5's INDI::PowerInterface: labels set, four DC
# outputs and a dew heater on, the input sagging 12.6 → 11.9 → 11.3 → 12.8 V, then the "Mount"
# output tripping off.
FIXTURE = Path(__file__).parent / "fixtures/indi/power-simulator-libindi-2.2.5.xml.gz"
BOX = "Power Simulator"


def recorded() -> list[tuple[float, ET.Element]]:
    root = ET.fromstring("<r>" + gzip.decompress(FIXTURE.read_bytes()).decode() + "</r>")
    out, last = [], 0.0
    for el in root:
        stamp = el.get("timestamp")
        if stamp:
            last = datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc).timestamp()
        out.append((last, el))
    return out


def decoded() -> list[Event]:
    decoder = IndiDecoder(load_builtin())
    return [e for ts, el in recorded() for e in decoder.feed(ts, el)]


def ev(ts, kind, sid="s1", **data):
    return Event(ts, kind, data, "test", sid)


def volts(ts, v, device=BOX):
    return ev(ts, "metric", metric="power.voltage", value=v, device=device)


def output(ts, on, current, label="Camera", output="POWER_CHANNEL_1"):
    data = {"device": BOX, "output": output, "kind": "dc", "label": label}
    return Event(ts, "power.output", {**data, "on": on, "current": current, "duty": None}, "indi")


def run(rule, events, until=None):
    out = []
    for e in events:
        out += rule.handle(e)
    if until is not None:
        out += rule.tick(until)
    return out


def rules_of(alerts):
    return [a.data["rule"] for a in alerts]


START = ev(0, "session.started", night="2026-11-17")


# -- decoder, on what libindi really sends -------------------------------------------------------


def test_power_box_recognised_from_the_recording():
    events = decoded()
    devices = [e for e in events if e.kind == "indi.device"]
    assert "power" in devices[0].data["interfaces"]
    caps = [e for e in events if e.kind == "indi.capabilities"][-1].data["metrics"]
    for metric in ("power.voltage", "power.current", "power.watts", "power.outputs"):
        assert caps[metric] == [BOX]
    voltages = [e.data["value"] for e in events if e.data.get("metric") == "power.voltage"]
    assert voltages == [12.6, 11.9, 11.3, 12.8]


def test_outputs_carry_the_boxs_own_labels_and_currents():
    outputs = [e.data for e in decoded() if e.kind == "power.output" and e.data["kind"] == "dc"]
    labels = {d["output"]: d["label"] for d in outputs}
    assert labels == {
        "POWER_CHANNEL_1": "Camera",
        "POWER_CHANNEL_2": "Mount",
        "POWER_CHANNEL_3": "Focuser",
        "POWER_CHANNEL_4": "USB hub",
    }
    camera = [d for d in outputs if d["label"] == "Camera" and d["on"]]
    assert camera[-1]["current"] == pytest.approx(1.2, abs=0.05)
    mount = [d for d in outputs if d["label"] == "Mount"]
    assert (mount[-1]["on"], mount[-1]["current"]) == (False, 0.0)  # tripped
    dew = [e.data for e in decoded() if e.kind == "power.output" and e.data["kind"] == "dew"]
    assert dew[-1]["label"] == "Main scope" and dew[-1]["duty"] == 50


def test_output_currents_are_deadbanded():
    outputs = [e for e in decoded() if e.kind == "power.output"]
    # The driver sends every output's current each second for ~30 s; jitter of 0.02 A isn't news.
    assert len(outputs) < 60


def test_recorded_night_raises_battery_and_output_alerts():
    rule = PowerRule(sustain=2.0)
    events = decoded()
    t0 = events[0].ts
    alerts = run(rule, [ev(t0 - 1, "session.started", night="n"), *events])
    alerts += rule.tick(events[-1].ts + FAULT_HOLD)
    assert rules_of(alerts) == [
        "battery_low",
        "battery_critical",
        "battery_ok",
        "power_output_off",
    ]
    low, critical, ok, off = alerts
    assert low.data["priority"] == "high" and "11.90 V" in low.data["body"]
    assert critical.data["priority"] == "emergency" and "11.30 V" in critical.data["body"]
    assert off.data["title"] == "Power output off: Mount"
    assert "drawing 0.4" in off.data["body"]


# -- battery -----------------------------------------------------------------------------------


def test_battery_critical_is_sent_outside_a_session():
    rule = PowerRule()
    alerts = run(rule, [volts(0, 11.2)], until=61)
    assert rules_of(alerts) == ["battery_critical"]
    assert alerts[0].session_id is None


def test_battery_low_waits_for_a_session():
    rule = PowerRule()
    assert run(rule, [volts(0, 11.9)], until=120) == []
    alerts = run(rule, [START], until=121)
    assert rules_of(alerts) == ["battery_low"]


def test_a_dip_while_slewing_is_not_a_flat_battery():
    rule = PowerRule()
    alerts = run(rule, [START, volts(10, 11.2), volts(40, 12.5)], until=200)
    assert alerts == []


def test_battery_low_once_until_it_recovers():
    rule = PowerRule()
    alerts = run(rule, [START, volts(10, 11.9)], until=80)
    alerts += run(rule, [volts(100, 12.1), volts(110, 11.9)], until=300)  # 12.1: not recovered
    assert rules_of(alerts) == ["battery_low"]
    alerts = run(rule, [volts(400, 12.4)])  # a fresh battery
    assert rules_of(alerts) == ["battery_ok"]
    alerts = run(rule, [volts(500, 11.9)], until=600)
    assert rules_of(alerts) == ["battery_low"]


def test_mains_turns_battery_alerts_off():
    rule = PowerRule(None, None)
    assert run(rule, [START, volts(10, 10.0)], until=500) == []


def test_resume_does_not_repeat_a_battery_alert():
    rule = PowerRule()
    sent = ev(5, "alert", rule="battery_low", device=BOX)
    rule.resume("s1", [sent])
    assert run(rule, [volts(10, 11.9)], until=200) == []


# -- outputs ----------------------------------------------------------------------------------


def test_output_with_no_current_while_on():
    rule = PowerRule()
    alerts = run(rule, [START, output(10, True, 1.2), output(20, True, 0.0)])
    alerts += rule.tick(20 + DEAD_AFTER)
    alerts += rule.tick(20 + DEAD_AFTER + FAULT_HOLD)
    assert rules_of(alerts) == ["power_output_dead"]
    assert alerts[0].data["title"] == "No current on Camera"
    assert "was drawing 1.20 A" in alerts[0].data["body"]
    alerts = run(rule, [output(200, True, 1.2)])
    assert rules_of(alerts) == ["power_output_back"]


def test_output_off_that_comes_straight_back_is_not_sent():
    rule = PowerRule()
    alerts = run(
        rule, [START, output(10, True, 1.2), output(20, False, 0.0), output(25, True, 1.1)]
    )
    assert run(rule, [], until=100) == [] and alerts == []


def test_outputs_turning_off_as_ekos_shuts_down_are_not_faults():
    rule = PowerRule()
    events = [
        START,
        output(10, True, 1.2),
        ev(20, "ekos.log", module="Ekos", text="Disconnecting INDI devices..."),
        output(22, False, 0.0),
    ]
    assert run(rule, events, until=100) == []


def test_held_output_alert_dropped_when_ekos_goes_away():
    rule = PowerRule()
    events = [
        START,
        output(10, True, 1.2),
        output(20, False, 0.0),
        ev(25, "ekos.state", state="idle"),
    ]
    assert run(rule, events, until=100) == []


def test_idle_output_switched_off_is_not_a_fault():
    rule = PowerRule()
    alerts = run(rule, [START, output(10, True, 0.0), output(20, False, 0.0)], until=100)
    assert alerts == []


def test_no_output_alerts_outside_a_session():
    rule = PowerRule()
    assert run(rule, [output(10, True, 1.2), output(20, False, 0.0)], until=100) == []


def test_box_disconnecting_forgets_its_outputs():
    rule = PowerRule()
    events = [
        START,
        output(10, True, 1.2),
        output(20, True, 0.0),
        ev(25, "indi.device", device=BOX, connected=False),
    ]
    assert run(rule, events, until=200) == []


# -- digest and rules --------------------------------------------------------------------------


def test_digest_shows_power_and_battery_ok_closes_the_problem():
    digest = DigestRule(min_session=0)
    for e in [
        START,
        volts(10, 12.4),
        volts(20, 11.9),
        volts(30, 12.5),
        ev(31, "metric", metric="power.current", value=4.2, device=BOX),
        ev(40, "capture.frame", type="light", filter="L", exposure=60.0),
    ]:
        digest.handle(e)
    digest.note_alerts([ev(25, "alert", rule="battery_low", priority="high", title="Battery low")])
    digest.note_alerts([ev(30, "alert", rule="battery_ok", priority="normal", title="Battery OK")])
    [out] = digest.tick(3600)
    assert "Power 12.5 V (low 11.9 V), 4.2 A" in out.data["body"]
    assert out.data["title"].endswith("All's well")


def test_rules_send_power_alerts():
    rules = Rules(power=PowerRule(sustain=5))
    out = []
    for e in [volts(0, 11.0)]:
        out += rules.handle(e)
    out += rules.tick(10)
    assert "battery_critical" in rules_of(out)


# -- config ------------------------------------------------------------------------------------


def write(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def test_config_battery_presets(tmp_path):
    assert (load(write(tmp_path, "{}")).low_volts, load(write(tmp_path, "{}")).critical_volts) == (
        12.0,
        11.5,
    )
    cfg = load(write(tmp_path, "power:\n  battery: lifepo4_12v\n"))
    assert (cfg.low_volts, cfg.critical_volts) == (12.8, 12.0)
    cfg = load(write(tmp_path, "power:\n  battery: lifepo4_12v\n  low_volts: 13.0\n"))
    assert (cfg.low_volts, cfg.critical_volts) == (13.0, 12.0)
    cfg = load(write(tmp_path, "power:\n  battery: mains\n  low_volts: 12\n"))
    assert (cfg.low_volts, cfg.critical_volts) == (None, None)
    cfg = load(write(tmp_path, "power:\n  battery: custom\n  critical_volts: 23\n"))
    assert (cfg.low_volts, cfg.critical_volts) == (None, 23.0)


@pytest.mark.parametrize(
    "text",
    [
        "power: 12\n",
        "power:\n  battery: nicad\n",
        "power:\n  battery: custom\n",
        "power:\n  low_volts: 11\n  critical_volts: 11.5\n",
        "power:\n  low_volts: lots\n",
    ],
)
def test_config_power_errors(tmp_path, text):
    with pytest.raises(ConfigError):
        load(write(tmp_path, text))


def test_indi_dump_recognises_the_box_and_lists_no_outputs_as_candidates():
    from ekos_notifier.indi import dump

    lines = dump.summary([el for _, el in recorded()], load_builtin())
    assert lines[0].startswith(f"{BOX}: indi_power_simulator (aux, power)")
    assert "power.outputs" in lines[1] and "power.voltage" in lines[1]
    assert not any("candidate" in line for line in lines)
