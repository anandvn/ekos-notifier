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


def flog(offset, text):
    return log(offset, f"[Focuser Simulator] {text}", module="Focus")


def failed_run_live(t):
    """Tonight's shape (KStars 3.8.5): failures only in the log, Ekos restarts by itself, and
    the run ends "aborted", never "failed"."""
    return [
        flog(t, "Autofocus starting..."),
        ev(t, "focus.state", state="in_progress"),
        flog(t + 7, "Failed to detect any stars. Reset frame and try again."),
        flog(t + 7, "Autofocus failed, moving back to initial focus position 44,893."),
        flog(t + 10, "Restarting autofocus process..."),
        ev(t + 10, "focus.state", state="in_progress"),
        flog(t + 11, "Failed to detect any stars. Aborting..."),
        flog(t + 11, "Autofocus failed, moving back to initial focus position 44,893."),
        ev(t + 15, "focus.state", state="aborted"),
    ]


def failed_run_older(t):
    """The earlier shape (open-questions): "aborted" for each attempt, then "Restarting…",
    and the last attempt "aborted" and "failed"."""
    out = []
    for i in range(3):
        out += [
            ev(t + 10 * i, "focus.state", state="in_progress"),
            flog(t + 10 * i + 5, "Autofocus failed, moving back to initial focus position 30,000."),
            ev(t + 10 * i + 6, "focus.state", state="aborted"),
        ]
        if i < 2:
            out.append(flog(t + 10 * i + 8, "Restarting autofocus process..."))
    return out + [ev(t + 27, "focus.state", state="failed")]


def test_two_failed_runs_alert_once_as_seen_live():
    events = START + [ev(5, "focus.hfr", hfr=4.21, position=30000, in_autofocus=True)]
    events += failed_run_live(10) + failed_run_live(100) + failed_run_live(200)
    got = alerts(events, until=300)
    assert only(got, "autofocus_failed") == [(130, "autofocus_failed")]  # ended 115, +10 s
    body = [a for _, r, a in got if r == "autofocus_failed"][0].data["body"]
    assert "failed 2 times in a row" in body and "Last HFR measured: 4.21." in body


def test_ekos_retry_loop_counts_once_in_the_older_shape():
    events = START + failed_run_older(10)
    assert only(alerts(events, until=200), "autofocus_failed") == []  # one run, not three
    events = START + failed_run_older(10) + failed_run_older(100)
    assert only(alerts(events, until=300), "autofocus_failed") == [(140, "autofocus_failed")]


def test_a_success_in_between_resets_the_count():
    events = (
        START
        + failed_run_live(10)
        + [
            ev(100, "focus.state", state="in_progress"),
            ev(130, "focus.state", state="complete"),
        ]
    )
    events += failed_run_live(200)
    assert only(alerts(events, until=300), "autofocus_failed") == []


def test_your_own_abort_is_not_a_failure():
    events = START + [
        ev(10, "focus.state", state="in_progress"),
        ev(20, "focus.state", state="aborted"),  # nothing failed: you pressed Stop
        ev(30, "focus.state", state="in_progress"),
        ev(40, "focus.state", state="aborted"),
    ]
    assert only(alerts(events, until=300), "autofocus_failed") == []


def test_capture_reported_aborted_while_idle_is_not_an_abort():
    # Seen live: a failed focus exposure made Ekos report Capture "aborted" while it was idle.
    events = START + [
        ev(5, "capture.state", state="idle"),
        log(100, "[CCD Simulator] Autofocus failed. Aborting exposure"),
        ev(100, "capture.state", state="aborted"),
    ]
    assert only(alerts(events, until=400), "capture_aborted") == []


# -- Guiding poor ------------------------------------------------------------------------------


def exceeded(dev, limit=0.5):
    return (
        f"[CCD Simulator] Guiding deviation {dev} exceeded limit value of {limit} arcsecs for 3 "
        "consecutive samples, suspending exposure and waiting for guider up to 60 seconds."
    )


def still(dev):
    return f"[CCD Simulator] Guiding deviation {dev} is still higher than limit value of 2 arcsecs."


def lower(dev):
    return (
        f"[CCD Simulator] Guiding deviation {dev} is now lower than limit value of 2 arcsecs, "
        "resuming exposure."
    )


def flapping(t0, until):
    """Seen live 2026-11-17 with a 0.5\u2033 limit: suspend, resume under 2\u2033, trip again
    within seconds, so no 60 s exposure ever finishes."""
    events = []
    for t in range(t0, until, 12):
        events += [
            log(t, exceeded(3.791)),
            ev(t, "capture.state", state="suspended"),
            log(t + 1, still(2.72)),
            log(t + 6, lower(1.856)),
            ev(t + 6, "capture.state", state="capturing"),
        ]
    return events


def test_ekos_suspending_over_and_over_alerts_until_a_frame_finishes():
    events = (
        START
        + exposing(10)
        + flapping(20, 700)
        + [
            ev(760, "capture.frame", type="light", filter="OIII", exposure=60.0),
        ]
    )
    got = alerts(events, until=800, rules=Rules(guiding_poor_window=600))
    assert only(got, "guiding_poor", "guiding_poor_cleared") == [
        (620, "guiding_poor"),
        (760, "guiding_poor_cleared"),
    ]
    body = [a for _, r, a in got if r == "guiding_poor"][0].data["body"]
    assert "no frame has finished since 22:00 (10 min)" in body
    assert "Its limit is 0.5\u2033; latest deviation" in body


def test_one_suspension_then_a_good_frame_is_silent():
    events = (
        START
        + exposing(10)
        + [
            log(20, exceeded(1.2)),
            log(25, lower(0.4)),
            ev(90, "capture.frame", type="light", filter="Ha", exposure=60.0),
        ]
    )
    events += flapping(100, 130)  # a short bad patch later, then frames again
    events += [ev(200, "capture.frame", type="light", filter="Ha", exposure=60.0)]
    got = alerts(events, until=2000, rules=Rules(guiding_poor_window=600))
    assert only(got, "guiding_poor") == []


def test_your_stop_while_suspended_is_silent():
    events = START + exposing(10) + flapping(20, 60) + [ev(70, "capture.state", state="aborted")]
    assert (
        only(alerts(events, until=2000, rules=Rules(guiding_poor_window=600)), "guiding_poor") == []
    )


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
