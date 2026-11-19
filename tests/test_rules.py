from datetime import datetime

from ekos_notifier import replay
from ekos_notifier.events import Event
from ekos_notifier.pipeline import Pipeline, simulate
from ekos_notifier.rules import Rules
from ekos_notifier.samples import SAMPLE_NIGHT

FIXTURE = SAMPLE_NIGHT
T = datetime(2026, 11, 16, 21, 0).timestamp()


def ev(offset, kind, sid="s1", **data):
    return Event(T + offset, kind, data, "ekos", sid)


def alerts(rules, *events, until=None):
    out = []
    for e in events:
        out += rules.handle(e)
    if until is not None:
        out += rules.tick(T + until)
    return [(a.data["rule"], a.data["priority"]) for a in out]


def test_unparked_emergency_after_grace():
    r = Rules(unparked_grace=120)
    got = alerts(r, ev(0, "mount.park", state="unparked"), ev(10, "ekos.state", state="idle"))
    assert got == []
    assert alerts(r, until=129) == []
    assert alerts(r, until=130) == [("ekos_stopped_mount_unparked", "emergency")]
    assert alerts(r, until=500) == []  # once


def test_mount_state_counts_when_no_park_status_was_seen():
    r = Rules(unparked_grace=60)
    got = alerts(r, ev(0, "mount.state", state="tracking"), ev(5, "kstars.vanished"), until=70)
    assert got == [("ekos_stopped_mount_unparked", "emergency")]


def test_no_emergency_when_parked_or_ekos_returns():
    r = Rules(unparked_grace=60)
    assert (
        alerts(r, ev(0, "mount.park", state="parked"), ev(5, "ekos.state", state="idle"), until=500)
        == []
    )
    r = Rules(unparked_grace=60)
    assert (
        alerts(
            r,
            ev(0, "mount.park", state="unparked"),
            ev(5, "ekos.state", state="idle"),
            ev(30, "ekos.state", state="success"),
            until=500,
        )
        == []
    )


def test_all_clear_when_parked_after_the_emergency():
    r = Rules(unparked_grace=60)
    alerts(r, ev(0, "mount.park", state="unparked"), ev(5, "indi.state", state="idle"), until=70)
    assert alerts(r, ev(300, "mount.park", state="parked")) == [
        ("mount_unparked_cleared", "normal")
    ]


def test_the_all_clear_is_sent_at_once_with_no_session():
    # Its Emergency comes after Ekos stops, usually with no session open, so no hourly digest
    # would ever carry it: the phone would keep the Emergency with nothing after it.
    from ekos_notifier.notify import should_send

    r = Rules(unparked_grace=60)
    for e in [ev(0, "mount.park", state="unparked"), ev(5, "indi.state", state="idle")]:
        r.handle(e)
    assert [a.data["rule"] for a in r.tick(T + 70)] == ["ekos_stopped_mount_unparked"]
    [cleared] = [a for a in r.handle(ev(300, "mount.park", state="parked")) if a.kind == "alert"]
    assert cleared.data["immediate"] and should_send(cleared.data)


def test_summary_counts_frames_of_the_session():
    r = Rules()
    out = []
    for e in [
        ev(0, "session.started", night="2026-11-16", reason="scheduler"),
        ev(60, "capture.frame", type="light", filter="Red", exposure=60.0),
        ev(120, "capture.frame", type="light", filter="Red", exposure=60.0),
        ev(130, "capture.frame", type="flat", filter="Red", exposure=1.0),
        ev(
            200,
            "session.closed",
            night="2026-11-16",
            state="closed",
            reason="user",
            ended_at=T + 200,
        ),
    ]:
        out += r.handle(e)
    summary = out[-1].data
    assert summary["rule"] == "session_summary" and summary["immediate"]
    assert "Red 2×60s" in summary["body"] and "Integration 2 min" in summary["body"]
    assert "1 flat" in summary["body"] and "after 3 min" in summary["body"]


def test_abandoned_session_sends_no_summary():
    r = Rules()
    assert (
        alerts(r, ev(0, "session.closed", night="n", state="abandoned", reason="ekos_stopped"))
        == []
    )


def test_m31_night_alerts_in_order_and_on_time():
    _, records = replay.read(FIXTURE)
    out = []
    simulate(replay.events(records), Pipeline(out.append))
    got = [a for a in out if a.kind == "alert"]
    assert [a.data["rule"] for a in got] == [
        "session_started",
        "session_summary",
        "flats_reminder",  # Luminance lights, but flats only for the other six filters
        "ekos_stopped_mount_unparked",
    ]
    started, summary, flats, emergency = got
    assert flats.data["filters"] == ["Luminance"] and flats.ts == summary.ts
    sched_idle = next(e for e in out if e.kind == "scheduler.state" and e.data["state"] == "idle")
    assert summary.ts == sched_idle.ts  # at once, not after Ekos's 10-minute grace
    body = summary.data["body"]
    assert "because the Scheduler finished." in body and "Integration 49 min" in body
    assert body.splitlines()[-1] == "Mount: not parked (last seen tracking)."
    indi_idle = next(e for e in out if e.kind == "indi.state" and e.data["state"] == "idle")
    assert 120 <= emergency.ts - indi_idle.ts <= 130  # 2 min grace, 10 s ticks
    assert "tracking" in emergency.data["body"]
    assert emergency.data["body"].splitlines()[-1] == "Park the mount or check the rig."
    assert {a.session_id for a in got} == {started.session_id}


def test_no_summary_when_you_stopped_ekos_but_one_after_a_crash():
    def closed(reason):
        r = Rules()
        r.handle(ev(0, "session.started", night="n", reason="light_frame"))
        r.handle(ev(60, "capture.frame", type="light", filter="Ha", exposure=60.0))
        return [
            a.data
            for a in r.handle(ev(900, "session.closed", night="n", state="closed", reason=reason))
            if a.kind == "alert"
        ]

    assert closed("ekos_stopped") == []
    (summary,) = closed("ekos_lost")
    assert "because Ekos stopped unexpectedly" in summary["body"] and "Ha 1×60s" in summary["body"]


def test_summary_after_a_restart_counts_the_sessions_frames():
    # Seen live: a restart mid-session gave "No light frames" for a session with 11.
    r = Rules()
    frames = [{"type": "light", "filter": "OIII", "exposure": 60.0}] * 3 + [
        {"type": "flat", "filter": "OIII", "exposure": 1.0}
    ]
    r.resume("s1", [], started_at=T - 1800, frames=frames)
    (summary,) = [
        a.data
        for a in r.handle(ev(0, "session.closed", night="n", state="closed", reason="ekos_lost"))
        if a.kind == "alert"
    ]
    assert "OIII 3×60s" in summary["body"] and "1 flat" in summary["body"]
    assert "after 30 min" in summary["body"]


def test_starting_session_comes_first():
    # Seen live: "Twilight coming" and "Target low" arrived just before "Starting session".
    from ekos_notifier.ekos.settings import ObservingLimits
    from ekos_notifier.sky import SkyRule

    r = Rules(sky=SkyRule(twilight_warning=86400, limits=lambda: ObservingLimits()))
    r.handle(ev(0, "site.location", latitude=51.5, longitude=0.0, via="kstars"))
    out = r.handle(ev(1, "session.started", night="n", reason="light_frame"))
    titles = [a.data["title"] for a in out if a.kind == "alert"]
    assert titles[0] == "Starting session" and "Twilight coming" in titles
