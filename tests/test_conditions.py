import gzip
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ekos_notifier.conditions import CoolerRule, DewRule
from ekos_notifier.config import ConfigError, load
from ekos_notifier.events import Event
from ekos_notifier.indi.decode import IndiDecoder
from ekos_notifier.indi.presets import load_builtin

FIXTURES = Path(__file__).parent / "fixtures/indi"
# Recorded from libindi 2.2.5's simulators: the CCD Simulator cooling from 0 to −10 °C (Busy,
# then Ok); the Weather Simulator at 15 °C with humidity 75 % → 92 % → 60 %.
COOLING = FIXTURES / "ccd-simulator-cooling-libindi-2.2.5.xml.gz"
HUMIDITY = FIXTURES / "weather-simulator-humidity-libindi-2.2.5.xml.gz"
CAMERA, WEATHER = "CCD Simulator", "Weather Simulator"


def decoded(path: Path) -> list[Event]:
    root = ET.fromstring("<r>" + gzip.decompress(path.read_bytes()).decode() + "</r>")
    decoder, out, last = IndiDecoder(load_builtin()), [], 0.0
    for el in root:
        if stamp := el.get("timestamp"):
            last = datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc).timestamp()
        out += decoder.feed(last, el)
    return out


def ev(ts, kind, sid="s1", **data):
    return Event(ts, kind, data, "test", sid)


def metric(ts, name, value, device):
    return ev(ts, "metric", metric=name, value=value, device=device)


def timeline(rule, events, until=None, step=1.0):
    """Feed events with the clock ticking every ``step`` seconds, like the service."""
    out, now = [], None
    for e in sorted(events, key=lambda e: e.ts):
        if now is not None:
            while now + step < e.ts:
                now += step
                out += rule.tick(now)
        out += rule.handle(e)
        now = e.ts
    while until is not None and now is not None and now + step <= until:
        now += step
        out += rule.tick(now)
    return out


def rules_of(alerts):
    return [a.data["rule"] for a in alerts]


def start(ts=0.0):
    return ev(ts, "session.started", night="2026-11-17")


# -- dew ---------------------------------------------------------------------------------------


def test_recorded_humidity_rise_raises_dew_risk_and_clears():
    events = decoded(HUMIDITY)
    rule = DewRule(sustain=5, clear_after=5)
    alerts = timeline(rule, [start(events[0].ts - 1), *events], until=events[-1].ts + 10)
    assert rules_of(alerts) == ["dew_risk", "dew_risk_cleared"]
    risk = alerts[0].data
    assert risk["priority"] == "high"
    assert "Dew margin 1.3 °C (ambient 15.0 °C, dew point 13.7 °C), RH 92%." in risk["body"]


def environment(ts, temp, dew, rh=None, device=WEATHER):
    out = [metric(ts, "env.temperature", temp, device), metric(ts, "env.dewpoint", dew, device)]
    if rh is not None:
        out.append(metric(ts, "env.humidity", rh, device))
    return out


def heater(ts, label, on, duty, output="DEW_CHANNEL_1"):
    data = {"device": "Box", "output": output, "kind": "dew", "label": label}
    return Event(ts, "power.output", {**data, "on": on, "current": None, "duty": duty}, "indi")


def test_dew_risk_names_heaters_that_are_off():
    events = [
        start(),
        heater(1, "Main scope", True, 50),
        heater(1, "Guide scope", False, 30, "DEW_CHANNEL_2"),
        *environment(10, 8.0, 7.0),
    ]
    [alert] = timeline(DewRule(), events, until=400)
    assert "Dew heaters: Guide scope off, Main scope 50%." in alert.data["body"]
    assert "Turn on “Guide scope”" in alert.data["body"]


def test_brief_humidity_spike_is_not_dew_risk():
    events = [start(), *environment(10, 10.0, 2.0, 95), *environment(100, 10.0, 2.0, 70)]
    assert timeline(DewRule(), events, until=1000) == []


def test_dew_risk_once_until_clearly_dry():
    rule = DewRule()
    alerts = timeline(rule, [start(), *environment(10, 8.0, 7.0)], until=400)
    # 2.5 °C margin: no longer at risk, but not 1 °C clear of the limit either.
    alerts += timeline(rule, [*environment(500, 8.0, 5.5), *environment(800, 8.0, 7.0)], until=1300)
    assert rules_of(alerts) == ["dew_risk"]
    alerts = timeline(rule, environment(1400, 8.0, 4.0), until=2100)
    assert rules_of(alerts) == ["dew_risk_cleared"]
    assert alerts[0].data["priority"] == "normal" and not alerts[0].data["immediate"]


def test_smallest_margin_of_several_sensors_counts():
    events = [
        start(),
        *environment(10, 12.0, 4.0, device="Weather"),
        *environment(10, 9.0, 8.0, device="Box"),
    ]
    [alert] = timeline(DewRule(), events, until=400)
    assert "ambient 9.0 °C" in alert.data["body"]


def test_no_dew_alerts_outside_a_session_or_when_off():
    assert timeline(DewRule(), environment(10, 8.0, 7.5, 97), until=1000) == []
    assert timeline(DewRule(0), [start(), *environment(10, 8.0, 7.5, 97)], until=1000) == []


def test_sensor_disconnecting_forgets_its_readings():
    caps = ev(20, "indi.capabilities", metrics={})
    assert timeline(DewRule(), [start(), *environment(10, 8.0, 7.5), caps], until=1000) == []


# -- cooler ------------------------------------------------------------------------------------


def test_recorded_cooldown_sets_the_setpoint_then_a_warm_camera_alerts():
    events = decoded(COOLING)
    cooler = [e.data["state"] for e in events if e.kind == "camera.cooler"]
    assert cooler == ["idle", "busy", "ok"]
    rule = CoolerRule()
    t = events[-1].ts
    warm = [metric(t + 10, "camera.temperature", -6.5, CAMERA)]
    alerts = timeline(rule, [start(events[0].ts - 1), *events, *warm], until=t + 10 + 600)
    assert rules_of(alerts) == ["cooler_cant_hold"]
    assert (
        "CCD Simulator is at −6.5 °C, 3.5 °C over its setpoint of −10.0 °C"
        in (alerts[0].data["body"])
    )
    alerts = timeline(rule, [metric(t + 700, "camera.temperature", -9.8, CAMERA)])
    assert rules_of(alerts) == ["cooler_ok"]
    assert alerts[0].data["immediate"]


def capture_log(ts, text):
    return ev(ts, "ekos.log", module="Capture", text=text)


def test_ekos_setpoint_never_reached():
    events = [
        start(),
        metric(1, "camera.temperature", 0.0, CAMERA),
        capture_log(5, "Setting temperature to -15.00 °C..."),
        metric(300, "camera.temperature", -8.0, CAMERA),
        metric(200, "env.temperature", 24.0, WEATHER),
    ]
    [alert] = timeline(CoolerRule(), events, until=5 + 1200)
    assert alert.data["rule"] == "cooler_cant_reach"
    assert "−8.0 °C, 20 min after the setpoint of −15.0 °C" in alert.data["body"]
    assert "Ambient 24.0 °C" in alert.data["body"]


def test_ekos_setpoint_reached_in_time_is_quiet():
    events = [
        start(),
        metric(1, "camera.temperature", 0.0, CAMERA),
        capture_log(5, "Setting temperature to -15.00 °C..."),
        metric(900, "camera.temperature", -15.0, CAMERA),
    ]
    assert timeline(CoolerRule(), events, until=3000) == []


def test_ekos_setpoint_goes_to_captures_camera():
    events = [
        start(),
        metric(1, "camera.temperature", 0.0, "Guide Cam"),
        metric(1, "camera.temperature", 0.0, "Main Cam"),
        ev(2, "capture.camera", camera="Main Cam"),
        capture_log(5, "Setting temperature to -10.00 °C..."),
    ]
    [alert] = timeline(CoolerRule(), events, until=1300)
    assert alert.data["device"] == "Main Cam"


def test_regulation_off_means_no_cooler_alerts():
    events = [
        start(),
        ev(1, "camera.cooler", sid=None, device=CAMERA, state="ok", temperature=-10.0),
        metric(2, "camera.temperature", -10.0, CAMERA),
        ev(3, "camera.cooler", sid=None, device=CAMERA, state="idle", temperature=-10.0),
        metric(10, "camera.temperature", 5.0, CAMERA),
    ]
    assert timeline(CoolerRule(), events, until=3000) == []


def test_a_new_driver_target_isnt_a_cooler_failure():
    events = [
        start(),
        metric(1, "camera.temperature", -10.0, CAMERA),
        ev(1, "camera.cooler", device=CAMERA, state="ok", temperature=-10.0),
        ev(10, "camera.cooler", device=CAMERA, state="busy", temperature=-10.0),
        metric(20, "camera.temperature", 0.0, CAMERA),  # warming up to a new target
    ]
    assert timeline(CoolerRule(), events, until=3000) == []


def test_resume_does_not_repeat_a_cooler_alert():
    rule = CoolerRule()
    rule.resume("s1", [ev(0, "alert", rule="cooler_cant_hold", device=CAMERA)])
    events = [
        metric(1, "camera.temperature", -10.0, CAMERA),
        ev(1, "camera.cooler", device=CAMERA, state="ok", temperature=-10.0),
        metric(10, "camera.temperature", -5.0, CAMERA),
    ]
    assert timeline(rule, events, until=1000) == []


def test_no_cooler_alerts_outside_a_session():
    events = [
        metric(1, "camera.temperature", -10.0, CAMERA),
        ev(1, "camera.cooler", device=CAMERA, state="ok", temperature=-10.0),
        metric(10, "camera.temperature", -5.0, CAMERA),
    ]
    assert timeline(CoolerRule(), events, until=1000) == []


# -- config ------------------------------------------------------------------------------------


def test_config_dew_and_cooler(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("alerts:\n  dew_margin_c: 3\n  cooler_tolerance_c: 0\n")
    cfg = load(path)
    assert (cfg.dew_margin_c, cfg.cooler_tolerance_c) == (3.0, 0.0)
    path.write_text("alerts:\n  dew_margin_c: wet\n")
    with pytest.raises(ConfigError):
        load(path)
