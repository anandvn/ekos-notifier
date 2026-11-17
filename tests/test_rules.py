from datetime import datetime
from pathlib import Path

from town_crier import replay
from town_crier.events import Event
from town_crier.pipeline import Pipeline, simulate
from town_crier.rules import Rules

FIXTURE = Path(__file__).parent / "fixtures/replay/sim-m31-scheduler-kstars-3.8.5-flatpak.ndjson.gz"
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
    simulate(replay.ekos_events(records), Pipeline(out.append))
    got = [a for a in out if a.kind == "alert"]
    assert [a.data["rule"] for a in got] == [
        "session_started",
        "ekos_stopped_mount_unparked",
        "session_summary",
    ]
    indi_idle = next(e for e in out if e.kind == "indi.state" and e.data["state"] == "idle")
    emergency = got[1]
    assert 120 <= emergency.ts - indi_idle.ts <= 130  # 2 min grace, 10 s ticks
    assert "tracking" in emergency.data["body"]
    assert emergency.data["body"].splitlines()[-1] == "Park the mount or check the rig."
    assert {a.session_id for a in got} == {got[0].session_id}
    assert "Integration 49 min" in got[2].data["body"]
