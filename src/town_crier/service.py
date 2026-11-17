"""The Town Crier service: sources → sessions → rules → store → notifier (docs/design.md,
"Architecture").

``serve()`` runs until SIGINT/SIGTERM. Shutdown stops the sources and the clock first, then lets
the writer commit everything still queued, so stopping the service loses nothing.

A source is a callable taking ``emit`` and returning a coroutine that runs until cancelled
(``EkosAdapter(emit).run()``). Events flow through ``Pipeline`` (sessions, rules) into the
``EventWriter``; the notifier reads alerts back from the store. Everything runs on the event loop
thread except SQLite and sending, which have their own threads.
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
from town_crier.config import Config
from town_crier.ekos.adapter import EkosAdapter
from town_crier.events import Event
from town_crier.notify import Notifier, init_cursor
from town_crier.pipeline import TICK, Pipeline
from town_crier.session import SessionManager
from town_crier.store import EventWriter, Store

log = logging.getLogger(__name__)

Emit = Callable[[Event], None]
Source = Callable[[Emit], Coroutine[Any, Any, None]]


def ekos_source(emit: Emit) -> Coroutine[Any, Any, None]:
    return EkosAdapter(emit).run()


async def serve(
    db_path: str | Path,
    *,
    config: Config | None = None,
    sources: Iterable[Source] = (ekos_source,),
    notifier: Notifier | None = None,
    tick: float = TICK,
    clock: Callable[[], float] = time.time,
    stop: asyncio.Event | None = None,
) -> None:
    """Run until ``stop`` is set (or SIGINT/SIGTERM when ``stop`` isn't given)."""
    db_path = Path(db_path)
    config = config or Config()
    store = Store(db_path)  # migrates, and tells us where the last run left off
    manager = SessionManager.resume(store)
    init_cursor(store)
    store.close()
    if manager.session_id:
        log.info("resuming %s session %s", manager.state, manager.session_id)

    writer = EventWriter(db_path)
    pipeline = Pipeline(writer.put, manager=manager)
    writer_task = asyncio.create_task(writer.run(), name="store")
    writer.put(Event(clock(), "service.started", {"version": __version__}, "service"))
    if notifier is None and config.notify:
        notifier = Notifier(db_path, config.notify, max_age=config.notify_expire_minutes * 60)
    if notifier is None:
        log.info("no notifiers configured: recording only")

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
    if notifier is not None:
        workers.append(asyncio.create_task(notifier.run(), name="notifier"))
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
            writer.put(Event(clock(), "service.stopped", {}, "service"))
            writer_task.cancel()  # the writer flushes its queue before finishing
            await asyncio.gather(writer_task, return_exceptions=True)
        log.info("stopped; %d events written this run", writer.written)
