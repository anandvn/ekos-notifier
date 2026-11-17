"""The local SQLite store: every event is written here first.

Outputs (notifier, GUI, heartbeat, sync) only read from the store, so a dead network or a slow
consumer never loses data or blocks the adapters. See docs/design.md, "Data model".

- ``events`` is append-only and holds every event as the adapters reported it.
- ``frames`` and ``samples`` are projections filled from events in the same transaction, for
  queries the GUI and session summaries make often.
- WAL mode: readers (``Store(path, readonly=True)``) never block the writer. ``synchronous=NORMAL``
  can lose the last moments before a power cut but never corrupts the database.
- Schema changes are migrations keyed on ``PRAGMA user_version``.

``Store`` is synchronous and must be used from one thread. ``EventWriter`` is the asyncio face:
adapters call ``writer.put(event)``; one task writes batches on a dedicated thread.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sqlite3
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from town_crier.events import Event

log = logging.getLogger(__name__)

MIGRATIONS = [
    # 1: events, with frames and samples projected from them.
    """
    CREATE TABLE events (
        id         INTEGER PRIMARY KEY,
        session_id TEXT,
        ts         REAL NOT NULL,
        source     TEXT NOT NULL,
        kind       TEXT NOT NULL,
        priority   TEXT,               -- set only on alerts from the rules engine
        data       TEXT NOT NULL       -- JSON
    );
    CREATE INDEX events_ts ON events (ts);
    CREATE INDEX events_kind_ts ON events (kind, ts);
    CREATE INDEX events_session ON events (session_id, id);

    CREATE TABLE frames (
        id          INTEGER PRIMARY KEY,
        event_id    INTEGER NOT NULL REFERENCES events (id),
        session_id  TEXT,
        target_id   TEXT,
        ts          REAL NOT NULL,
        type        TEXT,
        filter      TEXT,
        exposure    REAL,
        hfr         REAL,
        star_count  INTEGER,
        guide_rms   REAL,              -- total arcsec, last guide.rms within GUIDE_RMS_MAX_AGE
        camera_temp REAL,
        file        TEXT
    );
    CREATE INDEX frames_session_ts ON frames (session_id, ts);

    CREATE TABLE samples (
        ts     REAL NOT NULL,
        metric TEXT NOT NULL,
        value  REAL NOT NULL
    );
    CREATE INDEX samples_metric_ts ON samples (metric, ts);
    """,
]

# A frame's guide RMS is the last guide.rms seen at most this many seconds before it.
GUIDE_RMS_MAX_AGE = 30.0


def default_path() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    return Path(base) / "town-crier" / "town-crier.db"


class Store:
    def __init__(self, path: str | Path, *, readonly: bool = False) -> None:
        self.path = Path(path)
        if readonly:
            self.db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(self.path)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self._migrate()
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self._guide_rms: tuple[float, float] | None = None  # (ts, total)

    def close(self) -> None:
        self.db.close()

    @property
    def schema_version(self) -> int:
        return self.db.execute("PRAGMA user_version").fetchone()[0]

    def _migrate(self) -> None:
        version = self.schema_version
        if version > len(MIGRATIONS):
            raise RuntimeError(
                f"{self.path} has schema {version}; this Town Crier knows up to "
                f"{len(MIGRATIONS)}. Upgrade Town Crier, or use a different database."
            )
        for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
            with self.db:
                self.db.executescript(f"BEGIN; {script}; PRAGMA user_version = {number};")
            log.info("store: migrated %s to schema %d", self.path, number)

    # -- writing -----------------------------------------------------------------------------

    def append(self, events: Iterable[Event], session_id: str | None = None) -> list[int]:
        """Write events and their projections in one transaction; returns the event ids."""
        ids = []
        with self.db:
            for event in events:
                cur = self.db.execute(
                    "INSERT INTO events (session_id, ts, source, kind, data)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (session_id, event.ts, event.source, event.kind, json.dumps(event.data)),
                )
                ids.append(cur.lastrowid)
                self._project(cur.lastrowid, event, session_id)
        return ids

    def _project(self, event_id: int, event: Event, session_id: str | None) -> None:
        d = event.data
        if event.kind == "guide.rms":
            total = math.hypot(d["ra"], d["dec"])
            self._guide_rms = (event.ts, total)
            self.db.executemany(
                "INSERT INTO samples (ts, metric, value) VALUES (?, ?, ?)",
                [
                    (event.ts, "guide.rms_ra", d["ra"]),
                    (event.ts, "guide.rms_dec", d["dec"]),
                    (event.ts, "guide.rms", total),
                ],
            )
        elif event.kind == "capture.frame":
            rms = None
            if self._guide_rms and 0 <= event.ts - self._guide_rms[0] <= GUIDE_RMS_MAX_AGE:
                rms = self._guide_rms[1]
            self.db.execute(
                "INSERT INTO frames (event_id, session_id, ts, type, filter, exposure, hfr,"
                " star_count, guide_rms, file) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    session_id,
                    event.ts,
                    d.get("type"),
                    d.get("filter"),
                    d.get("exposure"),
                    d.get("hfr"),
                    d.get("star_count"),
                    rms,
                    d.get("file"),
                ),
            )

    # -- reading -----------------------------------------------------------------------------

    def events_after(
        self, last_id: int = 0, kinds: Iterable[str] | None = None, limit: int = 1000
    ) -> list[Event]:
        """Events with id > last_id, oldest first: how a consumer catches up."""
        return [e for _, e in self.events_after_with_ids(last_id, kinds, limit)]

    def events_after_with_ids(
        self, last_id: int = 0, kinds: Iterable[str] | None = None, limit: int = 1000
    ) -> list[tuple[int, Event]]:
        sql = "SELECT id, ts, source, kind, data FROM events WHERE id > ?"
        params: list[Any] = [last_id]
        if kinds is not None:
            kinds = list(kinds)
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            params += kinds
        sql += " ORDER BY id LIMIT ?"
        params.append(limit)
        return [
            (r["id"], Event(r["ts"], r["kind"], json.loads(r["data"]), r["source"]))
            for r in self.db.execute(sql, params)
        ]

    def frames(self, session_id: str | None = None) -> Iterator[sqlite3.Row]:
        if session_id is None:
            return iter(self.db.execute("SELECT * FROM frames ORDER BY ts"))
        return iter(
            self.db.execute("SELECT * FROM frames WHERE session_id = ? ORDER BY ts", (session_id,))
        )

    def samples(self, metric: str, since: float = 0.0) -> list[tuple[float, float]]:
        rows = self.db.execute(
            "SELECT ts, value FROM samples WHERE metric = ? AND ts >= ? ORDER BY ts",
            (metric, since),
        )
        return [(r[0], r[1]) for r in rows]


class EventWriter:
    """Queue events from asyncio code and write them in batches on the store's own thread.

    ``put`` never blocks, so it can be an adapter's ``emit``. A failed write keeps the batch
    and retries; events are only dropped from memory once they're committed.
    """

    def __init__(
        self,
        store_path: str | Path,
        *,
        batch_size: int = 500,
        linger: float = 0.25,
        retry_delay: float = 5.0,
    ) -> None:
        self.store_path = store_path
        self.batch_size = batch_size
        self.linger = linger
        self.retry_delay = retry_delay
        self.queue: asyncio.Queue[Event] = asyncio.Queue()
        self.written = 0
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="store")
        self._store: Store | None = None

    def put(self, event: Event) -> None:
        self.queue.put_nowait(event)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        self._store = await loop.run_in_executor(self._executor, Store, self.store_path)
        try:
            while True:
                batch = [await self.queue.get()]
                await self._gather(batch)
                await self._write(batch)
        except asyncio.CancelledError:
            await self.flush()
            raise
        finally:
            store, self._store = self._store, None
            if store is not None:
                await loop.run_in_executor(self._executor, store.close)
            self._executor.shutdown(wait=False)

    async def flush(self) -> None:
        """Write whatever is queued (on shutdown, and in tests)."""
        while not self.queue.empty():
            batch = []
            while not self.queue.empty() and len(batch) < self.batch_size:
                batch.append(self.queue.get_nowait())
            await self._write(batch, retry=False)

    async def _gather(self, batch: list[Event]) -> None:
        """Wait briefly for more events so a burst becomes one transaction."""
        deadline = asyncio.get_running_loop().time() + self.linger
        while len(batch) < self.batch_size:
            timeout = deadline - asyncio.get_running_loop().time()
            if timeout <= 0:
                break
            try:
                batch.append(await asyncio.wait_for(self.queue.get(), timeout))
            except asyncio.TimeoutError:
                break

    async def _write(self, batch: list[Event], retry: bool = True) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                await loop.run_in_executor(self._executor, self._store.append, batch)
                self.written += len(batch)
                return
            except sqlite3.Error as exc:
                if not retry:
                    log.error("store: lost %d events at shutdown: %s", len(batch), exc)
                    return
                log.warning("store: write failed (%s); retrying in %ss", exc, self.retry_delay)
                await asyncio.sleep(self.retry_delay)
