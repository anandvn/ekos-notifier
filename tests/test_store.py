import asyncio
import sqlite3

import pytest

from ekos_notifier import replay
from ekos_notifier.events import Event
from ekos_notifier.samples import SAMPLE_NIGHT
from ekos_notifier.store import MIGRATIONS, EventWriter, Store

FIXTURE = SAMPLE_NIGHT


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
    with pytest.raises(RuntimeError, match="Upgrade Ekos Notifier"):
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
    events = list(replay.events(records))
    store.append(events)
    frames = list(store.frames())
    assert len(frames) == 140
    assert sum(f["type"] == "flat" for f in frames) == 24
    # Lights were guided; PHD2 paused guiding for the flats.
    lights = [f for f in frames if f["type"] == "light"]
    assert all(f["guide_rms"] is not None for f in lights)
    assert len(store.events_after(0, limit=len(events) + 1)) == len(events)


def test_writer_stopped_before_the_store_opens_still_writes(tmp_path):
    async def scenario():
        writer = EventWriter(tmp_path / "tc.db")
        task = asyncio.create_task(writer.run())
        writer.put(Event(1.0, "early", {}))
        await asyncio.sleep(0)  # the writer has started opening, not finished
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    s = Store(tmp_path / "tc.db", readonly=True)
    assert [e.kind for e in s.events_after()] == ["early"]
    s.close()


def test_writer_stopped_mid_batch_writes_the_batch(tmp_path):
    async def scenario():
        writer = EventWriter(tmp_path / "tc.db", linger=5.0)  # long gather window
        task = asyncio.create_task(writer.run())
        await asyncio.sleep(0.2)  # store open, writer waiting for events
        for i in range(300):
            writer.put(Event(float(i), "e", {}))
        await asyncio.sleep(0.1)  # writer has taken the batch and is lingering
        assert writer.queue.qsize() < 300
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return writer.written

    assert asyncio.run(scenario()) == 300
    s = Store(tmp_path / "tc.db", readonly=True)
    assert len(s.events_after(limit=1000)) == 300
    s.close()


def test_writer_drains_a_backlog_without_waiting_per_batch(tmp_path):
    async def scenario():
        writer = EventWriter(tmp_path / "tc.db", batch_size=500, linger=0.25)
        task = asyncio.create_task(writer.run())
        for i in range(6000):  # 12 batches: lingering before each would take 3 s
            writer.put(Event(float(i), "e", {}))
        loop = asyncio.get_running_loop()
        start = loop.time()
        while writer.written < 6000 and loop.time() - start < 5:
            await asyncio.sleep(0.01)
        elapsed = loop.time() - start
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return elapsed

    assert asyncio.run(scenario()) < 1.5
