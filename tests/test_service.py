import asyncio
from pathlib import Path

from town_crier import replay
from town_crier.cli import main
from town_crier.events import Event
from town_crier.service import serve
from town_crier.store import Store

FIXTURE = Path(__file__).parent / "fixtures/replay/sim-m31-scheduler-kstars-3.8.5-flatpak.ndjson.gz"


def run_with(db, events, *, clock_after=None):
    """Serve with one source that emits ``events`` and then asks the service to stop."""

    async def scenario():
        stop = asyncio.Event()
        last = {"t": 0.0}

        def clock():
            return last["t"]

        async def source(emit):
            for event in events:
                last["t"] = event.ts
                emit(event)
            if clock_after is not None:
                last["t"] = clock_after
                await asyncio.sleep(0.05)  # let the ticker see the later time
            stop.set()
            await asyncio.Event().wait()

        await serve(db, sources=[source], tick=0.01, clock=clock, stop=stop)

    asyncio.run(scenario())


def test_m31_through_the_service(tmp_path):
    _, records = replay.read(FIXTURE)
    events = list(replay.ekos_events(records))
    run_with(tmp_path / "tc.db", events, clock_after=events[-1].ts + 601)
    store = Store(tmp_path / "tc.db", readonly=True)
    (session,) = store.sessions()
    assert (session["state"], session["end_reason"]) == ("closed", "scheduler_finished")
    kinds = [e.kind for e in store.events_after(0, limit=100_000)]
    assert kinds[0] == "service.started" and kinds[-1] == "service.stopped"
    assert len(list(store.frames(session["id"]))) == 140
    store.close()


def test_restart_resumes_the_open_session(tmp_path):
    db = tmp_path / "tc.db"
    run_with(db, [Event(1000.0, "scheduler.state", {"state": "running", "code": 2})])
    run_with(db, [Event(2000.0, "capture.frame", {"type": "light", "exposure": 10.0})])
    store = Store(db, readonly=True)
    (session,) = store.sessions()
    assert session["state"] == "active"
    assert len(list(store.frames(session["id"]))) == 1
    store.close()


def test_a_failing_writer_stops_the_service_with_an_error(tmp_path, caplog):
    db = tmp_path / "tc.db"
    import sqlite3

    Store(db).close()
    con = sqlite3.connect(db)
    con.execute("PRAGMA user_version = 99")
    con.close()
    assert main(["run", "--db", str(db)]) == 1
    assert "Upgrade Town Crier" in caplog.text
