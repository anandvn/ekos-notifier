from ekos_notifier.align import RETRY_GRACE, PlateSolveRule
from ekos_notifier.events import Event


def ev(ts, kind, sid="s1", **data):
    return Event(ts, kind, data, "ekos", sid)


def align(ts, state, **extra):
    return ev(ts, "align.state", state=state, **extra)


def log(ts, text, module="Align"):
    return ev(ts, "ekos.log", module=module, text=text)


def timeline(rule, events, until=None, step=10.0):
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


START = ev(0, "session.started", night="2026-11-17")


def failed_run(t, reason="Solver timed out after 60 seconds.", end="failed"):
    return [
        align(t, "in_progress"),
        log(t + 20, "Solver failed. Retrying without scale constraint."),
        log(t + 60, reason),
        align(t + 61, end),
    ]


def test_failed_solve_after_a_flip():
    events = [START, ev(100, "capture.state", state="meridian_flip"), *failed_run(200)]
    [alert] = timeline(PlateSolveRule(), events, until=261 + RETRY_GRACE)
    d = alert.data
    assert (d["rule"], d["priority"], d["title"]) == (
        "plate_solve_failed",
        "high",
        "Plate solve failed",
    )
    assert "couldn't solve the frame after the meridian flip (a run failed since" in d["body"]
    assert "Ekos: “Solver timed out after 60 seconds.”" in d["body"]


def test_failed_solve_after_the_scheduler_moves_on():
    events = [
        START,
        log(100, "Job 'NGC 7000' is slewing to target.", module="Scheduler"),
        *failed_run(150),
        *failed_run(300),
    ]
    [alert] = timeline(PlateSolveRule(), events, until=361 + RETRY_GRACE)
    assert "after slewing to NGC 7000 (2 runs failed since" in alert.data["body"]


def test_a_retry_that_solves_is_quiet_and_the_all_clear_follows_an_alert():
    rule = PlateSolveRule()
    events = [START, *failed_run(100), align(200, "in_progress"), align(230, "complete")]
    assert timeline(rule, events, until=1000) == []
    alerts = timeline(rule, [*failed_run(2000)], until=2061 + RETRY_GRACE)
    alerts += timeline(rule, [align(3000, "in_progress"), align(3030, "successful")])
    assert [a.data["rule"] for a in alerts] == ["plate_solve_failed", "plate_solve_ok"]
    assert alerts[1].data["immediate"]


def test_aborted_after_a_solver_failure_counts_but_a_plain_abort_does_not():
    rule = PlateSolveRule()
    abort = [START, align(100, "in_progress"), align(110, "aborted")]
    assert timeline(rule, abort, until=1000) == []
    failed = failed_run(2000, reason="Solver Failed.", end="aborted")
    [alert] = timeline(rule, failed, until=2061 + RETRY_GRACE)
    assert "Ekos: “Solver Failed.”" in alert.data["body"]


def test_once_per_streak_and_not_outside_a_session():
    rule = PlateSolveRule()
    events = [START, *failed_run(100), *failed_run(400), *failed_run(700)]
    assert len(timeline(rule, events, until=2000)) == 1
    outside = [align(10, "in_progress", sid=None), align(20, "failed", sid=None)]
    assert timeline(PlateSolveRule(), outside, until=1000) == []


def test_snapshot_reading_is_not_a_failure():
    events = [START, align(10, "failed", snapshot=True)]
    assert timeline(PlateSolveRule(), events, until=1000) == []


def test_resume_does_not_repeat_the_alert():
    rule = PlateSolveRule()
    rule.resume("s1", [ev(0, "alert", rule="plate_solve_failed")])
    assert timeline(rule, failed_run(100), until=1000) == []
