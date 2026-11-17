from datetime import datetime
from pathlib import Path

import pytest

from ekos_notifier import replay
from ekos_notifier.config import ConfigError, load
from ekos_notifier.events import Event
from ekos_notifier.pipeline import Pipeline, simulate
from ekos_notifier.rules import Rules

FIXTURE = Path(__file__).parent / "fixtures/replay/sim-m31-scheduler-kstars-3.8.5-flatpak.ndjson.gz"
T = datetime(2026, 11, 17, 22, 0).timestamp()


def ev(offset, kind, **data):
    return Event(T + offset, kind, data, "ekos", "s1")


def guiding_night():
    """A Scheduler session, guiding and capturing lights."""
    return [
        ev(0, "session.started", night="2026-11-17", reason="scheduler"),
        ev(1, "mount.state", state="tracking"),
        ev(2, "guide.state", state="guiding"),
        ev(3, "phd2.state", state="guiding"),
        ev(4, "capture.state", state="capturing"),
        ev(60, "capture.frame", type="light", filter="Ha", exposure=60.0),
    ]


def run(events, until, step=10, recovery=300):
    """Feed events and tick every ``step`` s up to ``until``; return (time offset, rule)."""
    rules = Rules(guiding_recovery=recovery)
    out = []
    events = sorted(events, key=lambda e: e.ts)
    t = 0
    for e in events:
        while T + t < e.ts:
            out += rules.tick(T + t)
            t += step
        out += rules.handle(e)
    while t <= until:
        out += rules.tick(T + t)
        t += step
    return [
        (round(a.ts - T), a.data["rule"], a)
        for a in out
        if a.kind == "alert" and a.data["rule"].startswith("guiding")
    ]


def rules_of(got):
    return [(t, r) for t, r, _ in got]


def test_star_lost_alerts_after_five_minutes_then_recovers():
    events = guiding_night() + [
        ev(100, "phd2.state", state="lost_lock"),
        ev(100, "phd2.star_lost", status="Star lost - low SNR", snr=2.1),
        ev(101, "guide.state", state="aborted"),
        ev(102, "capture.state", state="suspended"),  # Ekos waits for guiding: not a pause
        ev(1000, "phd2.state", state="guiding"),
        ev(1000, "guide.state", state="guiding"),
    ]
    got = run(events, until=1200)
    assert rules_of(got) == [(400, "guiding_lost"), (1000, "guiding_recovered")]
    lost, recovered = got[0][2], got[1][2]
    assert lost.data["priority"] == "high" and lost.data["title"] == "Guiding lost"
    body = lost.data["body"]
    assert "PHD2: Star lost - low SNR." in body and "capture: suspended" in body
    assert recovered.data["immediate"] and "15 min after it stopped" in recovered.data["body"]


def test_a_blip_that_recovers_is_silent():
    events = guiding_night() + [
        ev(100, "guide.state", state="reacquire"),
        ev(220, "guide.state", state="guiding"),
    ]
    assert run(events, until=1200) == []


def test_retrying_calibration_does_not_reset_the_clock():
    events = guiding_night() + [ev(100, "guide.state", state="aborted")]
    for t in range(130, 500, 60):  # Ekos restarts guiding; calibration keeps failing
        events += [
            ev(t, "guide.state", state="looping"),
            ev(t + 10, "guide.state", state="calibrating"),
            ev(t + 40, "guide.state", state="calibration_error"),
        ]
    assert rules_of(run(events, until=600)) == [(400, "guiding_lost")]


def test_clock_restarts_after_a_meridian_flip():
    events = guiding_night() + [
        ev(100, "capture.state", state="meridian_flip"),
        ev(100, "mount.state", state="slewing"),
        ev(101, "guide.state", state="aborted"),
        ev(280, "mount.state", state="tracking"),
        ev(281, "capture.state", state="aligning"),
        ev(400, "capture.state", state="capturing"),  # resumes, but guiding never came back
    ]
    assert rules_of(run(events, until=1000)) == [(700, "guiding_lost")]


def test_wall_flats_with_tracking_off_are_not_a_failure():
    events = guiding_night() + [
        ev(100, "capture.state", state="idle"),
        ev(100, "mount.state", state="slewing"),
        ev(100, "guide.state", state="aborted"),
        ev(106, "mount.state", state="idle"),  # tracking off at the wall
        ev(110, "capture.state", state="capturing"),
    ]
    events += [ev(120 + 10 * i, "capture.frame", type="flat", filter="L") for i in range(60)]
    assert run(events, until=1500) == []


def test_calibration_frames_alone_pause_the_clock():
    events = guiding_night() + [
        ev(100, "guide.state", state="aborted"),
        ev(110, "capture.frame", type="dark", exposure=60.0),
    ]
    assert run(events, until=1500) == []


def test_run_ending_is_not_a_failure():
    events = guiding_night() + [
        ev(100, "capture.state", state="complete"),
        ev(101, "guide.state", state="aborted"),
    ]
    assert run(events, until=1500) == []
    events = guiding_night() + [
        ev(100, "guide.state", state="aborted"),
        ev(200, "session.closed", night="2026-11-17", state="closed", reason="scheduler_aborted"),
    ]
    assert run(events, until=1500) == []


def test_unguided_night_never_alerts():
    events = [
        ev(0, "session.started", night="2026-11-17", reason="light_frame"),
        ev(1, "mount.state", state="tracking"),
        ev(2, "guide.state", state="idle"),
        ev(3, "capture.state", state="capturing"),
    ]
    assert run(events, until=3000) == []


def test_phd2_alone_when_ekos_guide_state_is_unknown():
    events = [
        ev(0, "session.started", night="2026-11-17", reason="scheduler"),
        ev(1, "phd2.state", state="guiding", snapshot=True),
        ev(2, "capture.state", state="capturing"),
        ev(100, "phd2.state", state="stopped"),
        ev(
            101,
            "phd2.alert",
            message="INDI camera disconnected\nPlease reconnect it.",
            type="warning",
        ),
    ]
    got = run(events, until=600)
    assert rules_of(got) == [(400, "guiding_lost")]
    assert "PHD2 says: INDI camera disconnected" in got[0][2].data["body"]


def test_recovery_minutes_from_config(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text("alerts:\n  guiding_recovery_minutes: 2\n")
    assert load(f).guiding_recovery_minutes == 2.0
    f.write_text("alerts:\n  guiding_recovery_minutes: soon\n")
    with pytest.raises(ConfigError, match="guiding_recovery_minutes"):
        load(f)
    events = guiding_night() + [ev(100, "guide.state", state="aborted")]
    assert rules_of(run(events, until=600, recovery=120)) == [(220, "guiding_lost")]


def test_planned_stops_in_the_m31_night_are_silent():
    _, records = replay.read(FIXTURE)
    out = []
    simulate(replay.events(records), Pipeline(out.append))
    rules = [a.data["rule"] for a in out if a.kind == "alert"]
    assert "guiding_lost" not in rules and rules[0] == "session_started"
