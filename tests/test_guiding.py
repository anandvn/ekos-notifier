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


def test_ekos_aborting_capture_because_guiding_stopped_still_alerts():
    # Seen live 2026-11-17 (manual capture, PHD2): in the same second, guiding aborted and
    # Capture logged "Autoguiding stopped. Aborting..." and went to aborted.
    events = guiding_night() + [
        ev(100, "guide.state", state="aborted"),
        ev(100, "capture.state", state="aborted"),
        ev(100, "phd2.state", state="stopped"),
    ]
    got = run(events, until=600)
    assert rules_of(got) == [(400, "guiding_lost")]
    assert "capture: aborted" in got[0][2].data["body"]


def test_you_stopping_capture_first_is_not_a_failure():
    events = guiding_night() + [
        ev(100, "capture.state", state="aborted"),
        ev(160, "guide.state", state="aborted"),
    ]
    assert run(events, until=1500) == []


def test_mount_not_tracking_alerts_and_says_so():
    # Seen live: the simulator mount wasn't tracking, so PHD2 kept losing the star.
    events = guiding_night() + [
        ev(90, "mount.state", state="idle"),
        ev(100, "guide.state", state="reacquire"),
        ev(100, "phd2.state", state="lost_lock"),
        ev(100, "phd2.star_lost", status="Star lost - low mass"),
    ]
    got = run(events, until=600)
    assert rules_of(got) == [(400, "guiding_lost")]
    assert "mount: not tracking" in got[0][2].data["body"]
    assert "PHD2: Star lost - low mass." in got[0][2].data["body"]


RA_WARNING = (
    "PHD2 is not able to make sufficient corrections in RA.  Check for cable snags, try "
    "re-doing your calibration, and check for problems with the mount mechanics."
)


def phd2_alerts(events):
    rules = Rules()
    out = []
    for e in events:
        out += rules.handle(e)
    return [a for a in out if a.kind == "alert" and a.data["rule"] == "phd2_warning"]


def test_phd2_warnings_sent_once_per_message_with_guide_rms():
    # Seen live: this warning repeated every ~30 s while guiding was poor.
    events = guiding_night() + [ev(70, "guide.rms", ra=8.28, dec=0.62)]
    events += [
        ev(100 + 30 * i, "phd2.alert", message=RA_WARNING, type="warning") for i in range(10)
    ]
    events += [ev(500, "phd2.alert", message="INDI camera disconnected", type="warning")]
    got = phd2_alerts(events)
    assert [a.data["body"].splitlines()[0] for a in got] == [RA_WARNING, "INDI camera disconnected"]
    assert got[0].data["priority"] == "high" and got[0].data["title"] == "PHD2 warning"
    assert "Guide RMS now: RA 8.28″, DEC 0.62″." in got[0].data["body"]


def test_phd2_advice_info_and_setup_are_not_sent():
    advice = (
        "For best results, use a Dark Library or a Bad-pixel Map while guiding. This will help "
        "prevent PHD from locking on to a hot pixel."
    )
    events = [ev(0, "phd2.alert", message=RA_WARNING, type="warning")]  # before the session
    events += guiding_night() + [
        ev(100, "phd2.alert", message=advice, type="warning"),
        ev(110, "phd2.alert", message="Calibration complete", type="info"),
    ]
    assert phd2_alerts(events) == []


def test_new_session_sends_the_same_warning_again():
    events = guiding_night() + [
        ev(100, "phd2.alert", message=RA_WARNING, type="error"),
        ev(200, "session.closed", night="2026-11-17", state="closed", reason="user"),
        ev(300, "session.started", night="2026-11-17", reason="light_frame"),
        ev(400, "phd2.alert", message=RA_WARNING, type="error"),
    ]
    got = phd2_alerts(events)
    assert len(got) == 2 and got[0].data["title"] == "PHD2 error"


def test_rules_resumed_after_a_service_restart_still_alert():
    # Seen live: after a restart mid-session no session.started arrives; without resume() the
    # guiding rule thought no session was active and stayed silent.
    rules = Rules(guiding_recovery=60)
    rules.resume("s1")
    out = []
    for e in guiding_night()[1:] + [ev(100, "guide.state", state="aborted")]:
        out += rules.handle(e)
    out += rules.tick(T + 200)
    assert [a.data["rule"] for a in out if a.kind == "alert"] == ["guiding_lost"]


def test_phd2_warnings_at_the_end_of_the_run_are_not_sent():
    # The Scheduler's shutdown disconnects INDI; PHD2 then warns about the camera and mount.
    events = guiding_night() + [
        ev(100, "capture.state", state="complete"),
        ev(102, "phd2.alert", message="INDI camera disconnected", type="warning"),
        ev(102, "indi.state", state="idle"),
        ev(102, "phd2.alert", message="INDI mount was disconnected", type="warning"),
    ]
    assert phd2_alerts(events) == []


def test_a_restart_does_not_resend_phd2_warnings(tmp_path):
    # Seen live: after a restart the RA warning was sent a second time in the same session.
    from ekos_notifier.store import Store

    first = phd2_alerts(
        guiding_night() + [ev(100, "phd2.alert", message=RA_WARNING, type="warning")]
    )
    store = Store(tmp_path / "db.sqlite")
    store.append(guiding_night()[:1] + first)
    rules = Rules()
    rules.resume("s1", store.session_alerts("s1"))
    store.close()
    again = rules.handle(ev(200, "phd2.alert", message=RA_WARNING, type="warning"))
    assert again == []
    new = rules.handle(ev(210, "phd2.alert", message="INDI camera disconnected", type="warning"))
    assert [a.data["message"] for a in new] == ["INDI camera disconnected"]
