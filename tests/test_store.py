import asyncio
import sqlite3
from pathlib import Path

import pytest

from town_crier import replay
from town_crier.events import Event
from town_crier.store import MIGRATIONS, EventWriter, Store

FIXTURE = Path(__file__).parent / "fixtures/replay/sim-m31-scheduler-kstars-3.8.5-flatpak.ndjson.gz"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "tc.db")
    yield s
    s.close()


def test_new_database_is_migrated_and_in_wal_mode(store):
    assert store.schema_version == len(MIGRATIONS)
    assert store.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_reopening_does_not_migrate_again(tmp_path):
    Store(tmp_path / "tc.db").close()
    s = Store(tmp_path / "tc.db")
    assert s.schema_version == len(MIGRATIONS)
    s.close()


def test_refuses_a_newer_schema(tmp_path):
    db = sqlite3.connect(tmp_path / "tc.db")
    db.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
    db.close()
    with pytest.raises(RuntimeError, match="Upgrade Town Crier"):
        Store(tmp_path / "tc.db")


def test_append_and_catch_up(store):
    ids = store.append(
        [Event(1.0, "mount.park", {"state": "parked", "code": 1}), Event(2.0, "x", {})]
    )
    assert ids == [1, 2]
    assert [e.kind for e in store.events_after(0)] == ["mount.park", "x"]
    assert store.events_after(1) == [Event(2.0, "x", {}, "ekos")]
    assert [e.kind for e in store.events_after(0, kinds=["x"])] == ["x"]


def test_frames_take_recent_guide_rms(store):
    store.append(
        [
            Event(100.0, "guide.rms", {"ra": 0.3, "dec": 0.4}),
            Event(110.0, "capture.frame", {"type": "light", "filter": "Red", "exposure": 10.0}),
            Event(200.0, "capture.frame", {"type": "light", "filter": "Red", "exposure": 10.0}),
        ]
    )
    frames = list(store.frames())
    assert frames[0]["guide_rms"] == pytest.approx(0.5)
    assert frames[1]["guide_rms"] is None  # last RMS is 100 s old
    assert store.samples("guide.rms") == [(100.0, pytest.approx(0.5))]


def test_a_reader_sees_committed_events_while_the_writer_is_open(tmp_path):
    w = Store(tmp_path / "tc.db")
    w.append([Event(1.0, "a", {})])
    r = Store(tmp_path / "tc.db", readonly=True)
    assert [e.kind for e in r.events_after()] == ["a"]
    with pytest.raises(sqlite3.OperationalError):
        r.db.execute("INSERT INTO samples VALUES (1, 'x', 1)")
    r.close()
    w.close()


def test_writer_batches_and_flushes_on_cancel(tmp_path):
    async def scenario():
        writer = EventWriter(tmp_path / "tc.db", linger=0.05)
        task = asyncio.create_task(writer.run())
        for i in range(1200):
            writer.put(Event(float(i), "guide.delta", {"ra": 0.1, "dec": 0.1}))
        await asyncio.sleep(0.3)
        writer.put(Event(9999.0, "last", {}))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return writer.written

    assert asyncio.run(scenario()) == 1201
    s = Store(tmp_path / "tc.db", readonly=True)
    events = s.events_after(0, limit=5000)
    assert len(events) == 1201 and events[-1].kind == "last"
    s.close()


def test_m31_night_into_the_store(store):
    _, records = replay.read(FIXTURE)
    events = list(replay.ekos_events(records))
    store.append(events)
    frames = list(store.frames())
    assert len(frames) == 140
    assert sum(f["type"] == "flat" for f in frames) == 24
    # Lights were guided; PHD2 paused guiding for the flats.
    lights = [f for f in frames if f["type"] == "light"]
    assert all(f["guide_rms"] is not None for f in lights)
    assert len(store.events_after(0, limit=len(events) + 1)) == len(events)
