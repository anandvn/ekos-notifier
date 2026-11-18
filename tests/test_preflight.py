import time

from ekos_notifier.conditions import CoolerRule, DewRule
from ekos_notifier.disk import DiskRule
from ekos_notifier.events import Event
from ekos_notifier.power import PowerRule
from ekos_notifier.rules import Rules

GB = 1e9


def ev(ts, kind, sid="s1", **data):
    return Event(ts, kind, data, "ekos", sid)


def metric(ts, name, value, device="Box"):
    return ev(ts, "metric", metric=name, value=value, device=device)


def started(ts=100):
    return ev(ts, "session.started", night="2026-11-17", reason="scheduler")


def body(rules, events):
    out = []
    for e in events:
        out += rules.handle(e)
    [alert] = [a for a in out if a.data.get("rule") == "session_started"]
    return alert.data["body"].splitlines()


def test_readings_when_all_is_well():
    rules = Rules(disk=DiskRule(free=lambda path: 212 * GB))
    lines = body(
        rules,
        [
            metric(0, "power.voltage", 13.12),
            metric(1, "env.temperature", 8.0),
            metric(2, "env.dewpoint", 1.8),
            metric(3, "env.humidity", 64.0),
            started(),
        ],
    )
    assert lines[1] == "Pre-flight: battery 13.1 V, dew margin 6.2 °C, RH 64%, 212 GB free."


def test_warnings_come_first_and_never_stop_the_session():
    rules = Rules(disk=DiskRule(free=lambda path: 4.2 * GB, home="/data"))
    lines = body(
        rules,
        [
            metric(0, "power.voltage", 11.84),
            metric(1, "env.temperature", 3.0),
            metric(2, "env.dewpoint", 1.6),
            started(),
        ],
    )
    assert lines[1] == (
        "Check: battery 11.84 V, at or under 12 V low; dew margin 1.4 °C, under 2 °C; "
        "4.2 GB free on /data, under 10 GB."
    )
    assert len(lines) == 2  # nothing passed


def test_nothing_known_says_nothing():
    assert body(Rules(), [started()]) == [
        f"Night of 2026-11-17: session started at {time.strftime('%H:%M', time.localtime(100))}"
        " because the Ekos Scheduler started."
    ]


def test_battery_levels_and_mains():
    rule = PowerRule()
    rule.handle(metric(0, "power.voltage", 11.2))
    assert rule.preflight(0) == [(False, "battery 11.20 V, at or under 11.5 V critical")]
    mains = PowerRule(None, None)
    mains.handle(metric(0, "power.voltage", 12.2))
    assert mains.preflight(0) == [(True, "power 12.2 V")]


def test_camera_against_its_setpoint():
    rule = CoolerRule()
    rule.handle(ev(0, "capture.camera", camera="QHY"))
    rule.handle(metric(1, "camera.temperature", 5.0, device="QHY"))
    assert rule.preflight(1) == [(True, "camera 5.0 °C")]
    rule.handle(ev(2, "ekos.log", module="Capture", text="Setting temperature to -10.00 °C..."))
    assert rule.preflight(2) == [(True, "camera cooling, 5.0 °C → −10.0 °C")]
    rule.handle(metric(3, "camera.temperature", -9.8, device="QHY"))
    assert rule.preflight(3) == [(True, "camera −9.8 °C (at setpoint)")]
    rule.handle(metric(4, "camera.temperature", -4.0, device="QHY"))
    assert rule.preflight(4) == [(False, "camera −4.0 °C, over its −10.0 °C setpoint")]


def test_dew_off_and_humidity():
    assert DewRule(margin=0).preflight(0) == []
    rule = DewRule()
    rule.handle(metric(0, "env.humidity", 93.0))
    assert rule.preflight(0) == [(False, "RH 93%, over 90%")]
