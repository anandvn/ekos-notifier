import itertools
from datetime import datetime
from pathlib import Path

import pytest

from ekos_notifier import replay, session
from ekos_notifier.events import Event
from ekos_notifier.session import SessionManager, night_of
from ekos_notifier.store import Store

FIXTURE = Path(__file__).parent / "fixtures/replay/sim-m31-scheduler-kstars-3.8.5-flatpak.ndjson.gz"
EVENING = datetime(2026, 11, 16, 21, 0).timestamp()


def manager(**kw):
    ids = (f"s{n}" for n in itertools.count(1))
    return SessionManager(new_id=lambda: next(ids), **kw)


def ev(offset, kind, **data):
    return Event(EVENING + offset, kind, data)


def feed(m, *events):
    out = []
    for e in events:
        out += m.handle(e)
    return out


def lifecycle(out):
    return [
        (e.kind, e.session_id, e.data.get("reason"), e.data.get("state"))
        for e in out
        if e.kind.startswith("session.")
    ]


def test_night_of():
    assert night_of(datetime(2026, 11, 17, 2, 0).timestamp()) == "2026-11-16"
    assert night_of(datetime(2026, 11, 16, 21, 0).timestamp()) == "2026-11-16"
    assert night_of(datetime(2026, 11, 17, 13, 0).timestamp()) == "2026-11-17"


def test_setup_then_scheduler_then_finish_parked_or_not():
    m = manager()
    out = feed(
        m,
        ev(0, "ekos.state", state="success"),
        ev(60, "focus.state", state="complete"),
        ev(120, "scheduler.state", state="running"),
        ev(130, "capture.frame", type="light"),
        ev(890, "capture.state", state="complete"),  # one of the Scheduler's jobs: not the end
        ev(900, "scheduler.state", state="idle"),
    )
    assert lifecycle(out) == [
        ("session.setup", "s1", None, None),
        ("session.started", "s1", "scheduler", None),
        ("session.closed", "s1", "scheduler_finished", "closed"),
    ]
    # Setup events belong to the session they led into.
    assert {e.session_id for e in out if not e.kind.startswith("session.")} == {"s1"}
    assert m.state == "standby"


def test_scheduler_abort_closes_too():
    m = manager()
    out = feed(
        m, ev(0, "scheduler.state", state="running"), ev(9, "scheduler.state", state="aborted")
    )
    assert lifecycle(out)[-1] == ("session.closed", "s1", "scheduler_aborted", "closed")


def test_manual_sequence_complete_closes():
    m = manager()
    out = feed(
        m,
        ev(0, "capture.frame", type="light"),
        ev(60, "capture.state", state="complete", snapshot=True),  # a reading, not an end
        ev(70, "capture.frame", type="light"),
        ev(80, "capture.state", state="complete"),
    )
    assert lifecycle(out) == [
        ("session.started", "s1", "light_frame", None),
        ("session.closed", "s1", "sequence_complete", "closed"),
    ]


def test_scheduler_snapshot_reading_idle_mid_run_is_not_the_end():
    # Seen live: after a service restart the snapshot said idle while the Scheduler ran.
    m = manager()
    out = feed(
        m,
        ev(0, "scheduler.state", state="running"),
        ev(60, "scheduler.state", state="idle", snapshot=True),
        ev(64, "scheduler.state", state="running"),
    )
    assert lifecycle(out) == [("session.started", "s1", "scheduler", None)]


def test_light_frame_starts_a_session_from_standby_but_flats_dont():
    m = manager()
    out = feed(m, ev(0, "capture.frame", type="flat"), ev(10, "capture.frame", type="light"))
    assert out[0].session_id is None
    assert lifecycle(out) == [("session.started", "s1", "light_frame", None)]
    assert out[-1].session_id == "s1"


def test_ekos_crash_and_restart_within_grace_keeps_the_session():
    m = manager(grace=600)
    out = feed(
        m,
        ev(0, "scheduler.state", state="running"),
        ev(100, "kstars.vanished"),
        ev(400, "ekos.state", state="success"),
        ev(500, "capture.frame", type="light"),
        ev(2000, "capture.frame", type="light"),
    )
    assert lifecycle(out) == [("session.started", "s1", "scheduler", None)]
    assert out[-1].session_id == "s1"


def test_ekos_gone_past_grace_closes_at_the_time_it_went():
    m = manager(grace=600)
    out = feed(m, ev(0, "scheduler.state", state="running"), ev(100, "ekos.state", state="idle"))
    out += m.tick(EVENING + 699)
    assert lifecycle(out) == [("session.started", "s1", "scheduler", None)]
    out += m.tick(EVENING + 700)
    closed = out[-1]
    assert (closed.kind, closed.data["reason"], closed.data["ended_at"]) == (
        "session.closed",
        "ekos_lost",  # no shutdown logged: a crash
        EVENING + 100,
    )


def test_setup_without_imaging_is_abandoned():
    m = manager(grace=60)
    out = feed(m, ev(0, "indi.state", state="success"), ev(10, "indi.state", state="idle"))
    out += m.tick(EVENING + 100)
    assert lifecycle(out)[-1] == ("session.closed", "s1", "ekos_lost", "abandoned")


def test_calibration_after_close_goes_to_last_imaging_session():
    m = manager(grace=60)
    out = feed(
        m,
        ev(0, "capture.frame", type="light"),
        ev(10, "ekos.state", state="idle"),
        ev(200, "ekos.state", state="success"),  # opens a new setup session (s2)
        ev(210, "capture.frame", type="flat"),
        ev(220, "focus.state", state="complete"),
    )
    assert out[-2].kind == "capture.frame" and out[-2].session_id == "s1"
    assert out[-1].session_id == "s2"


def test_gui_start_and_end():
    m = manager()
    out = m.start(EVENING) + m.end(EVENING + 60)
    assert lifecycle(out) == [
        ("session.started", "s1", "user", None),
        ("session.closed", "s1", "user", "closed"),
    ]
    assert m.end(EVENING + 70) == []


def test_night_over_closes_at_last_activity():
    m = manager()
    feed(m, ev(0, "scheduler.state", state="running"), ev(3600, "guide.rms", ra=0.4, dec=0.3))
    next_noon = datetime(2026, 11, 17, 12, 0).timestamp()
    assert m.tick(next_noon - 1) == []
    closed = m.tick(next_noon)[0]
    assert (closed.data["reason"], closed.data["ended_at"]) == ("night_ended", EVENING + 3600)


def test_resume_after_service_restart(tmp_path):
    store = Store(tmp_path / "tc.db")
    m = manager()
    store.append(
        feed(m, ev(0, "ekos.state", state="success"), ev(10, "scheduler.state", state="running"))
    )
    m2 = SessionManager.resume(store)
    assert (m2.state, m2.session_id, m2.started) == ("active", "s1", True)
    out = feed(m2, ev(20, "capture.frame", type="light"))
    assert out[-1].session_id == "s1" and lifecycle(out) == []
    store.close()


@pytest.fixture(scope="module")
def m31():
    _, records = replay.read(FIXTURE)
    return list(session.run(replay.events(records), manager()))


def test_m31_is_one_session_closed_when_the_scheduler_finished(m31):
    life = [e for e in m31 if e.kind.startswith("session.")]
    assert [(e.kind, e.data.get("reason")) for e in life] == [
        ("session.started", "scheduler"),
        ("session.closed", "scheduler_finished"),
    ]
    started, closed = life
    sched_idle = next(e for e in m31 if e.kind == "scheduler.state" and e.data["state"] == "idle")
    assert closed.ts == closed.data["ended_at"] == sched_idle.ts
    frames = [e for e in m31 if e.kind == "capture.frame"]
    assert len(frames) == 140 and {f.session_id for f in frames} == {started.session_id}
    # Before the Scheduler started, Ekos Notifier was in standby.
    assert all(e.session_id is None for e in m31 if e.ts < started.ts)


def test_m31_session_row_in_the_store(tmp_path, m31):
    store = Store(tmp_path / "tc.db")
    store.append(m31)
    (row,) = store.sessions()
    assert (row["state"], row["start_reason"], row["end_reason"]) == (
        "closed",
        "scheduler",
        "scheduler_finished",
    )
    assert row["night"] == "2026-11-16"
    assert len(list(store.frames(row["id"]))) == 140
    assert store.open_session() is None
    store.close()


def test_your_own_ekos_stop_closes_quietly_and_a_crash_does_not():
    # Seen live: Stop in Ekos logs "Disconnecting INDI devices..." before INDI and Ekos go idle.
    m = manager(grace=600)
    out = feed(
        m,
        ev(0, "capture.frame", type="light"),
        ev(100, "ekos.log", module="Ekos", text="Disconnecting INDI devices..."),
        ev(100, "indi.state", state="idle"),
        ev(102, "ekos.state", state="idle"),
    )
    out += m.tick(EVENING + 700)
    assert lifecycle(out)[-1] == ("session.closed", "s1", "ekos_stopped", "closed")
    assert out[-1].data["ended_at"] == EVENING + 100

    m = manager(grace=600)
    out = feed(m, ev(0, "capture.frame", type="light"), ev(100, "kstars.vanished"))
    out += m.tick(EVENING + 700)
    assert lifecycle(out)[-1] == ("session.closed", "s1", "ekos_lost", "closed")


def test_a_shutdown_logged_long_before_doesnt_make_a_crash_orderly():
    m = manager(grace=600)
    out = feed(
        m,
        ev(0, "ekos.log", module="Ekos", text="Disconnecting INDI devices..."),
        ev(10, "ekos.state", state="success"),
        ev(20, "capture.frame", type="light"),
        ev(5000, "ekos.state", state="idle"),
    )
    out += m.tick(EVENING + 5700)
    assert lifecycle(out)[-1][2] == "ekos_lost"


def test_resume_keeps_when_and_how_ekos_went(tmp_path):
    store = Store(tmp_path / "tc.db")
    m = manager()
    store.append(
        feed(
            m,
            ev(0, "capture.frame", type="light", filter="Ha", exposure=300.0),
            ev(100, "ekos.log", module="Ekos", text="Disconnecting INDI devices..."),
            ev(100, "indi.state", state="idle"),
        )
    )
    m2 = SessionManager.resume(store)  # the service restarted at 100 + something
    out = m2.tick(EVENING + 700)
    assert lifecycle(out) == [("session.closed", "s1", "ekos_stopped", "closed")]
    assert out[0].data["ended_at"] == EVENING + 100  # not the restart time
    store.close()
