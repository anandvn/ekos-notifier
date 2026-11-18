"""The Ekos Notifier service: sources → sessions → rules → store → notifier (docs/design.md,
"Architecture").

``serve()`` runs until SIGINT/SIGTERM. Shutdown stops the sources and the clock first, then lets
the writer commit everything still queued, so stopping the service loses nothing.

A source is a callable taking ``emit`` and returning a coroutine that runs until cancelled
(``EkosAdapter(emit).run()``, ``Phd2Adapter(emit).run()``). Events flow through ``Pipeline``
(sessions, rules) into the ``EventWriter``; the notifier reads alerts back from the store.
Everything runs on the event loop thread except SQLite and sending, which have their own threads.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from collections.abc import Callable, Coroutine, Iterable
from pathlib import Path
from typing import Any

from ekos_notifier import __version__
from ekos_notifier.conditions import CoolerRule, DewRule
from ekos_notifier.config import Config
from ekos_notifier.digest import DigestRule
from ekos_notifier.ekos.adapter import EkosAdapter
from ekos_notifier.ekos.settings import guide_deviation_limit, observing_limits
from ekos_notifier.events import Event
from ekos_notifier.indi.adapter import IndiAdapter
from ekos_notifier.indi.decode import IndiDecoder
from ekos_notifier.indi.presets import load_builtin
from ekos_notifier.lock import InstanceLock
from ekos_notifier.notify import Notifier, init_cursor
from ekos_notifier.phd2.adapter import Phd2Adapter
from ekos_notifier.pipeline import TICK, Pipeline
from ekos_notifier.power import PowerRule
from ekos_notifier.rules import Rules
from ekos_notifier.session import SessionManager
from ekos_notifier.sky import SkyRule
from ekos_notifier.store import EventWriter, Store

log = logging.getLogger(__name__)

Emit = Callable[[Event], None]
Source = Callable[[Emit], Coroutine[Any, Any, None]]


def ekos_source(emit: Emit) -> Coroutine[Any, Any, None]:
    return EkosAdapter(emit).run()


def phd2_source(emit: Emit) -> Coroutine[Any, Any, None]:
    return Phd2Adapter(emit).run()


def indi_source(config: Config) -> Source:
    def source(emit: Emit) -> Coroutine[Any, Any, None]:
        decoder = IndiDecoder(load_builtin(), config.indi_mappings)
        return IndiAdapter(emit, decoder, host=config.indi_host, port=config.indi_port).run()

    return source


SOURCES: tuple[Source, ...] = (ekos_source, phd2_source)


async def serve(
    db_path: str | Path,
    *,
    config: Config | None = None,
    sources: Iterable[Source] | None = None,
    notifier: Notifier | None = None,
    tick: float = TICK,
    clock: Callable[[], float] = time.time,
    stop: asyncio.Event | None = None,
) -> None:
    """Run until ``stop`` is set (or SIGINT/SIGTERM when ``stop`` isn't given)."""
    db_path = Path(db_path)
    config = config or Config()
    lock = InstanceLock(db_path)
    lock.acquire()  # raises AlreadyRunning: two would send every alert twice
    try:
        await _serve(db_path, config, sources, notifier, tick, clock, stop)
    finally:
        lock.release()


async def _serve(
    db_path: Path,
    config: Config,
    sources: Iterable[Source] | None,
    notifier: Notifier | None,
    tick: float,
    clock: Callable[[], float],
    stop: asyncio.Event | None,
) -> None:
    if sources is None:
        sources = (*SOURCES, indi_source(config))
    store = Store(db_path)  # migrates, and tells us where the last run left off
    manager = SessionManager.resume(store)
    resumed_alerts = (
        store.session_alerts(manager.session_id)
        if manager.state == "active" and manager.session_id
        else []
    )
    open_row = store.open_session()
    resumed_start = open_row["started_at"] if open_row else None
    resumed_frames = [dict(f) for f in store.frames(open_row["id"])] if open_row else []
    init_cursor(store)
    store.close()
    if manager.session_id:
        log.info("resuming %s session %s", manager.state, manager.session_id)

    writer = EventWriter(db_path)
    rules = Rules(
        guiding_recovery=config.guiding_recovery_minutes * 60,
        guiding_poor_window=config.guiding_poor_minutes * 60,
        deviation_limit=guide_deviation_limit,
        sky=SkyRule(
            twilight_warning=config.twilight_warning_minutes * 60,
            target_low_warning=config.target_low_warning_minutes * 60,
            limits=observing_limits,
        ),
        power=PowerRule(config.low_volts, config.critical_volts),
        dew=DewRule(config.dew_margin_c),
        cooler=CoolerRule(config.cooler_tolerance_c),
        digest=DigestRule(
            enabled=config.digest_enabled,
            interval=config.digest_interval_minutes * 60,
            align_to_clock=config.digest_align_to_clock,
        ),
    )
    if manager.state == "active" and manager.session_id:
        rules.resume(manager.session_id, resumed_alerts, resumed_start, resumed_frames)
    pipeline = Pipeline(writer.put, manager=manager, rules=rules)
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
    log.info("Ekos Notifier %s running; store %s", __version__, db_path)

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
