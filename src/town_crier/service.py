"""The Town Crier service: sources → session manager → store (docs/design.md, "Architecture").

``serve()`` runs until SIGINT/SIGTERM. Shutdown stops the sources and the clock first, then lets
the writer commit everything still queued, so stopping the service loses nothing.

A source is a callable taking ``emit`` and returning a coroutine that runs until cancelled
(``EkosAdapter(emit).run()``). Events flow ``source → SessionManager.handle → EventWriter``;
all of it runs on the event loop thread except SQLite, which has its own.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from collections.abc import Callable, Coroutine, Iterable
from pathlib import Path
from typing import Any

from town_crier import __version__
from town_crier.ekos.adapter import EkosAdapter
from town_crier.events import Event
from town_crier.session import SessionManager
from town_crier.store import EventWriter, Store

log = logging.getLogger(__name__)

Emit = Callable[[Event], None]
Source = Callable[[Emit], Coroutine[Any, Any, None]]

TICK = 30.0  # seconds between clock ticks for time-based session rules


def ekos_source(emit: Emit) -> Coroutine[Any, Any, None]:
    return EkosAdapter(emit).run()


class Pipeline:
    """Routes events through the session manager into the writer, and logs what matters."""

    def __init__(self, manager: SessionManager, writer: EventWriter) -> None:
        self.manager = manager
        self.writer = writer

    def emit(self, event: Event) -> None:
        self._put(self.manager.handle(event))

    def tick(self, now: float) -> None:
        self._put(self.manager.tick(now))

    def _put(self, events: Iterable[Event]) -> None:
        for event in events:
            if event.kind.startswith("session."):
                log.info(
                    "%s %s (%s)",
                    event.kind,
                    event.session_id,
                    event.data.get("reason") or event.data.get("night"),
                )
            elif event.kind in ("kstars.appeared", "kstars.vanished", "ekos.bus_connected"):
                log.info("%s %s", event.kind, event.data)
            self.writer.put(event)


async def serve(
    db_path: str | Path,
    *,
    sources: Iterable[Source] = (ekos_source,),
    tick: float = TICK,
    clock: Callable[[], float] = time.time,
    stop: asyncio.Event | None = None,
) -> None:
    """Run until ``stop`` is set (or SIGINT/SIGTERM when ``stop`` isn't given)."""
    db_path = Path(db_path)
    store = Store(db_path)  # migrates, and tells us where the last run left off
    manager = SessionManager.resume(store)
    store.close()
    if manager.session_id:
        log.info("resuming %s session %s", manager.state, manager.session_id)

    writer = EventWriter(db_path)
    pipeline = Pipeline(manager, writer)
    writer_task = asyncio.create_task(writer.run(), name="store")
    pipeline._put([Event(clock(), "service.started", {"version": __version__}, "service")])

    loop = asyncio.get_running_loop()
    if stop is None:
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

    async def ticker() -> None:
        while True:
            await asyncio.sleep(tick)
            pipeline.tick(clock())

    workers = [asyncio.create_task(src(pipeline.emit), name="source") for src in sources]
    workers.append(asyncio.create_task(ticker(), name="clock"))
    stopper = asyncio.create_task(stop.wait(), name="stop")
    log.info("Town Crier %s running; store %s", __version__, db_path)

    try:
        done, _ = await asyncio.wait(
            [stopper, writer_task, *workers], return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:  # anything but the stop signal finishing is a fault
            if task is not stopper and not task.cancelled() and task.exception():
                raise task.exception()
    finally:
        for task in [stopper, *workers]:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        if not writer_task.done():
            pipeline._put([Event(clock(), "service.stopped", {}, "service")])
            writer_task.cancel()  # the writer flushes its queue before finishing
            await asyncio.gather(writer_task, return_exceptions=True)
        log.info("stopped; %d events written this run", writer.written)
