from datetime import datetime

import pytest

from ekos_notifier.config import ConfigError, load
from ekos_notifier.events import Event
from ekos_notifier.rules import Rules

T = datetime(2026, 11, 17, 22, 0).timestamp()


def ev(offset, kind, **data):
    return Event(T + offset, kind, data, "ekos", "s1")


def log(offset, text, module="Capture"):
    return ev(offset, "ekos.log", module=module, text=text)


START = [
    ev(0, "session.started", night="2026-11-17", reason="light_frame"),
    ev(1, "mount.state", state="tracking"),
    ev(2, "guide.state", state="guiding"),
]


def alerts(events, until, rules=None, step=10):
    rules = rules or Rules()
    out = []
    t = 0
    for e in sorted(events, key=lambda e: e.ts):
        while T + t < e.ts:
            out += rules.tick(T + t)
            t += step
        out += rules.handle(e)
    while t <= until:
        out += rules.tick(T + t)
        t += step
    return [(round(a.ts - T), a.data["rule"], a) for a in out if a.kind == "alert"]


def only(got, *rules):
    return [(t, r) for t, r, _ in got if r in rules]


def exposing(offset, seconds=60.0):
    return [
        ev(offset, "capture.state", state="capturing"),
        log(offset, f"[CCD Simulator] Capturing {seconds:.3f}-second H_Alpha image..."),
    ]


# -- Capture aborted ---------------------------------------------------------------------------


def test_capture_aborted_by_ekos_alerts_with_its_reason():
    # Seen live 2026-11-17: guiding stopped and Ekos aborted the capture in the same second.
    events = (
        START
        + exposing(10)
        + [
            ev(100, "guide.state", state="aborted"),
            log(100, "[CCD Simulator] Autoguiding stopped. Aborting..."),
            log(100, "[CCD Simulator] Camera capture stopped"),
            ev(100, "capture.state", state="aborted"),
        ]
    )
    got = alerts(events, until=300)
    assert only(got, "capture_aborted") == [(160, "capture_aborted")]
    body = [a for _, r, a in got if r == "capture_aborted"][0].data["body"]
    assert "Ekos: Autoguiding stopped." in body and "aborted at 22:01" in body


def test_autofocus_failure_reason():
    events = (
        START
        + exposing(10)
        + [
            log(100, "[CCD Simulator] Autofocus failed. Aborting exposure"),
            ev(100, "capture.state", state="aborted"),
        ]
    )
    body = [a for _, r, a in alerts(events, until=300) if r == "capture_aborted"][0].data["body"]
    assert "Ekos: Autofocus failed." in body


def test_an_abort_with_no_cause_is_yours_and_only_recorded():
    events = START + exposing(10) + [ev(100, "capture.state", state="aborted")]
    assert only(alerts(events, until=600), "capture_aborted") == []


def test_capture_restarting_within_the_grace_is_silent():
    events = (
        START
        + exposing(10)
        + [
            log(100, "[CCD Simulator] Autoguiding stopped. Aborting..."),
            ev(100, "capture.state", state="aborted"),
            ev(130, "capture.state", state="in_progress"),
        ]
    )
    assert only(alerts(events, until=600), "capture_aborted") == []


# -- Scheduler aborted -------------------------------------------------------------------------


def test_scheduler_aborted_alerts_but_stop_does_not():
    events = START + [
        ev(5, "scheduler.state", state="running"),
        log(100, "Job 'M 31' is aborted.", module="Scheduler"),
        ev(101, "scheduler.state", state="aborted"),
    ]
    got = alerts(events, until=200)
    assert only(got, "scheduler_aborted") == [(101, "scheduler_aborted")]
    assert "Scheduler: Job 'M 31' is aborted." in got[-1][2].data["body"]
    stopped = START + [
        ev(5, "scheduler.state", state="running"),
        ev(100, "scheduler.state", state="idle"),
    ]
    assert only(alerts(stopped, until=200), "scheduler_aborted") == []


# -- Capture stalled ---------------------------------------------------------------------------


def test_stalled_exposure_alerts_then_recovers():
    events = (
        START
        + exposing(10, 60.0)
        + [
            log(70, "[CCD Simulator] Download Time: 2.00 s, New Download Time Estimate: 2.00 s."),
            ev(70, "capture.frame", type="light", filter="Ha", exposure=60.0),
            *exposing(72, 60.0),  # this one never finishes
            ev(400, "capture.frame", type="light", filter="Ha", exposure=60.0),
        ]
    )
    got = alerts(events, until=500)
    # Limit 2 × (60 s + 2 s download) = 124 s from 72 s; the first tick after that is at 200.
    assert only(got, "capture_stalled", "capture_running") == [
        (200, "capture_stalled"),
        (400, "capture_running"),
    ]
    stalled = [a for _, r, a in got if r == "capture_stalled"][0].data["body"]
    assert "capturing a 60 s exposure" in stalled and "since 22:01" in stalled


def test_long_exposures_get_a_longer_limit():
    events = START + exposing(10, 300.0)
    assert only(alerts(events, until=600), "capture_stalled") == []  # limit 600 s
    assert only(alerts(events, until=620), "capture_stalled") == [(610, "capture_stalled")]


def test_waiting_states_and_ekos_gone_are_not_stalls():
    events = START + exposing(10) + [ev(20, "capture.state", state="guider_drift")]
    assert only(alerts(events, until=2000), "capture_stalled") == []
    events = START + exposing(10) + [ev(20, "kstars.vanished")]
    assert only(alerts(events, until=2000), "capture_stalled") == []


# -- Autofocus failed --------------------------------------------------------------------------


def focus_run(offset, *states):
    return [ev(offset + i, "focus.state", state=s) for i, s in enumerate(states)]


def test_two_failed_runs_alert_once_counting_ekos_retries_as_one():
    events = START + [
        ev(5, "focus.hfr", hfr=4.21, position=30000, in_autofocus=True),
        # Ekos's retry loop: two "aborted" restarts, then the last attempt aborted + failed.
        *focus_run(10, "in_progress", "aborted", "in_progress", "aborted", "in_progress"),
        *focus_run(20, "aborted", "failed"),
        *focus_run(100, "in_progress", "failed"),
        *focus_run(200, "in_progress", "failed"),  # still failing: no second alert
    ]
    got = alerts(events, until=300)
    assert only(got, "autofocus_failed") == [(101, "autofocus_failed")]
    body = got[-1][2].data["body"] if got[-1][1] == "autofocus_failed" else ""
    assert "failed 2 times in a row" in body and "Last HFR measured: 4.21." in body


def test_a_success_in_between_resets_the_count():
    events = START + [
        *focus_run(10, "in_progress", "failed"),
        *focus_run(100, "in_progress", "complete"),
        *focus_run(200, "in_progress", "failed"),
    ]
    assert only(alerts(events, until=300), "autofocus_failed") == []


# -- Guiding poor ------------------------------------------------------------------------------

EXCEEDED = (
    "Guiding deviation 3.4 exceeded limit value of 2 arcsecs for 3 consecutive samples, "
    "suspending exposure and waiting for guider up to 60 seconds."
)
STILL = "Guiding deviation 3.1 is still higher than limit value of 2 arcsecs."
LOWER = "Guiding deviation 1.2 is now lower than limit value of 2 arcsecs, resuming exposure."


def test_ekos_enforcing_its_limit_alerts_after_the_window():
    events = START + [ev(5, "guide.rms", ra=2.5, dec=1.0), log(100, EXCEEDED), log(400, STILL)]
    events += [log(800, LOWER)]
    got = alerts(events, until=900, rules=Rules(guiding_poor_window=600))
    assert only(got, "guiding_poor", "guiding_poor_cleared") == [
        (700, "guiding_poor"),
        (800, "guiding_poor_cleared"),
    ]
    body = [a for _, r, a in got if r == "guiding_poor"][0].data["body"]
    assert "holding exposures for 10 min" in body and "3.1″ is over its limit of 2″" in body


def test_rms_over_ekos_limit_when_ekos_does_not_enforce_it():
    rules = Rules(guiding_poor_window=600, deviation_limit=lambda: 1.5)
    events = START + [ev(10 + 30 * i, "guide.rms", ra=1.4, dec=0.9) for i in range(30)]
    events += [ev(1000 + 10 * i, "guide.rms", ra=0.8, dec=0.5) for i in range(20)]
    got = alerts(events, until=1300, rules=rules)
    assert only(got, "guiding_poor", "guiding_poor_cleared") == [
        (610, "guiding_poor"),
        (1120, "guiding_poor_cleared"),
    ]
    body = [a for _, r, a in got if r == "guiding_poor"][0].data["body"]
    assert "over 1.5″ (Ekos's guide deviation limit)" in body and "total 1.66″" in body


def test_rms_window_restarts_when_not_guiding_and_rule_can_be_off():
    events = START + [ev(10 + 30 * i, "guide.rms", ra=3.0, dec=0.0) for i in range(30)]
    events += [ev(300, "guide.state", state="aborted"), ev(310, "guide.state", state="guiding")]
    got = alerts(events, until=1000, rules=Rules(guiding_poor_window=600))
    # Guiding again at 310; the next sample over the limit (340) starts a fresh 10 minutes.
    assert only(got, "guiding_poor") == [(940, "guiding_poor")]
    assert (
        only(alerts(events, until=2000, rules=Rules(guiding_poor_window=0)), "guiding_poor") == []
    )


def test_guiding_poor_minutes_in_config(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text("alerts:\n  guiding_poor_minutes: 15\n")
    assert load(f).guiding_poor_minutes == 15.0
    f.write_text("alerts:\n  guiding_poor_minutes: -1\n")
    with pytest.raises(ConfigError, match="guiding_poor_minutes"):
        load(f)


def test_dithering_every_frame_does_not_reset_the_window():
    events = START + [ev(10 + 30 * i, "guide.rms", ra=3.0, dec=0.0) for i in range(30)]
    for t in range(60, 900, 60):
        events += [
            ev(t, "guide.state", state="dithering"),
            ev(t + 3, "guide.state", state="dithering_success"),
            ev(t + 5, "guide.state", state="guiding"),
        ]
    got = alerts(events, until=700, rules=Rules(guiding_poor_window=600))
    assert only(got, "guiding_poor") == [(610, "guiding_poor")]
