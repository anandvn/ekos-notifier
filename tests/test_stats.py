from ekos_notifier.events import Event
from ekos_notifier.rules import Rules
from ekos_notifier.stats import SessionStats


def ev(ts, kind, sid="s1", **data):
    return Event(ts, kind, data, "ekos", sid)


def metric(ts, name, value, device="Box"):
    return ev(ts, "metric", metric=name, value=value, device=device)


def light(ts, hfr):
    return ev(ts, "capture.frame", type="light", filter="Ha", exposure=300.0, hfr=hfr)


def test_summary_has_the_nights_numbers():
    events = [
        ev(0, "session.started", night="2026-11-17", reason="scheduler"),
        ev(1, "capture.camera", camera="QHY"),
        light(300, 2.1),
        light(600, 2.5),
        light(900, -1),  # not measured
        light(1200, 2.3),
        ev(310, "guide.rms", ra=0.3, dec=0.4),
        ev(320, "guide.rms", ra=0.6, dec=0.8),
        ev(330, "guide.rms", ra=0.6, dec=0.8),
        metric(10, "env.temperature", 12.4),
        metric(20, "env.temperature", 8.1),
        metric(10, "env.humidity", 61.0),
        metric(20, "env.humidity", 88.4),
        metric(30, "camera.temperature", -10.0, device="QHY"),
        metric(31, "camera.temperature", -12.0, device="Guider"),
        metric(40, "power.voltage", 13.1),
        metric(50, "power.voltage", 12.46),
        ev(60, "alert", sid="s1", rule="guiding_lost", priority="high", title="Guiding lost"),
        ev(1300, "session.closed", night="2026-11-17", state="closed", reason="user"),
    ]
    rules = Rules()
    out = []
    for e in events:
        if e.kind == "alert":
            rules._note_alerts([e, e])  # raised by the rules, twice
            continue
        out += rules.handle(e)
    [summary] = [a for a in out if a.data["rule"] == "session_summary"]
    lines = summary.data["body"].splitlines()
    assert "Median HFR 2.30, guide RMS 1.00″." in lines
    assert "Ambient 8.1 to 12.4 °C, RH 61 to 88%, camera −10.0 °C, lowest battery 12.46 V." in lines
    assert "Problems: Guiding lost ×2." in lines


def test_after_a_restart_the_stored_frames_count():
    stats = SessionStats()
    stats.frames(
        [
            {"type": "light", "hfr": 2.0, "guide_rms": 0.5},
            {"type": "flat", "hfr": 9.0, "guide_rms": None},
            {"type": "light", "hfr": None, "guide_rms": 0.7},
        ]
    )
    assert stats.lines() == ["Median HFR 2.00, guide RMS 0.60″."]


def test_settings_off_alerts_are_not_problems():
    stats = SessionStats()
    stats.alerts(
        [Event(0, "alert", {"rule": "dew_risk", "priority": "high", "title": "Dew", "off": True})]
    )
    assert stats.lines() == []
