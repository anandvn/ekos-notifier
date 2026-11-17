"""Live Ekos source: listen to KStars on the session bus and emit events.

Observe-only: the adapter reads properties and listens to signals; it never calls an Ekos
method that changes anything.

- Signals are matched by the well-known name ``org.kde.kstars``, so objects Ekos creates later
  (Capture, Mount… appear only once Ekos starts) are covered without re-introspecting.
- ``NameOwnerChanged`` for ``org.kde.kstars`` reports KStars starting, quitting or crashing.
- On connect, when KStars (re)appears and when Ekos announces a module, the adapter reads the
  current states so the store knows where things stand without waiting for the next change.
  Snapshot events carry ``"snapshot": True``.
- ``run()`` never returns: a lost bus or a failure reconnects with backoff.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable
from typing import Any

from dbus_fast import Message, MessageType
from dbus_fast.aio import MessageBus

from ekos_notifier.ekos import states
from ekos_notifier.ekos.bus import Candidate, candidates
from ekos_notifier.ekos.decode import (
    EKOS_PATH,
    KSTARS_NAME,
    decode_name_owner_changed,
    decode_signal,
    plain,
)
from ekos_notifier.events import Event

log = logging.getLogger(__name__)

DBUS = "org.freedesktop.DBus"
MATCH_RULES = (
    f"type='signal',sender='{KSTARS_NAME}'",
    f"type='signal',sender='{DBUS}',interface='{DBUS}',member='NameOwnerChanged',"
    f"arg0='{KSTARS_NAME}'",
)
BACKOFF = (1, 2, 5, 10, 30)
CONNECT_TIMEOUT = 5.0

# (module, property, event kind, state table). Module "" is /KStars/Ekos itself.
SNAPSHOT = (
    ("", "ekosStatus", "ekos.state", "communication"),
    ("", "indiStatus", "indi.state", "communication"),
    ("Scheduler", "status", "scheduler.state", "scheduler"),
    ("Mount", "status", "mount.state", "mount"),
    ("Mount", "parkStatus", "mount.park", "park"),
    ("Mount", "pierSide", "mount.pier_side", "pier_side"),
    ("Capture", "status", "capture.state", "capture"),
    ("Guide", "status", "guide.state", "guide"),
    ("Align", "status", "align.state", "align"),
)


def _path(module: str) -> str:
    return f"{EKOS_PATH}/{module}" if module else EKOS_PATH


def _interface(module: str) -> str:
    return f"{KSTARS_NAME}.Ekos.{module}" if module else f"{KSTARS_NAME}.Ekos"


class EkosAdapter:
    def __init__(
        self,
        emit: Callable[[Event], None],
        *,
        find: Callable[[], list[Candidate]] = candidates,
        clock: Callable[[], float] = time.time,
        backoff: tuple[float, ...] = BACKOFF,
    ) -> None:
        self._emit = emit
        self._find = find
        self._clock = clock
        self._backoff = backoff
        self._bus: Any = None
        self._tasks: set[asyncio.Task] = set()

    # -- lifecycle -------------------------------------------------------------------------

    async def run(self) -> None:
        failures = 0
        while True:
            try:
                await self.run_once()
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - supervisor: log, back off, retry
                log.warning("Ekos adapter: %s", exc)
            delay = self._backoff[min(failures, len(self._backoff) - 1)]
            failures += 1
            await asyncio.sleep(delay)

    async def run_once(self) -> None:
        """One connection: subscribe, snapshot, then wait until the bus goes away."""
        bus, source, kstars_running = await self._connect()
        self._bus = bus
        try:
            self._send("ekos.bus_connected", {"source": source, "kstars_running": kstars_running})
            for rule in MATCH_RULES:
                await self._call(bus, DBUS, "/org/freedesktop/DBus", DBUS, "AddMatch", "s", [rule])
            bus.add_message_handler(self.on_message)
            if kstars_running:
                await self.snapshot(bus)
            await bus.wait_for_disconnect()
        finally:
            self._bus = None
            if bus.connected:
                bus.disconnect()
        self._send("ekos.bus_lost", {"source": source})

    async def _connect(self) -> tuple[Any, str, bool]:
        """The first bus that has KStars on it, else the first that connects at all (KStars
        may start later; NameOwnerChanged will tell us)."""
        fallback = None
        errors = []
        for cand in self._find():
            try:
                bus = await asyncio.wait_for(
                    MessageBus(bus_address=cand.address).connect(), CONNECT_TIMEOUT
                )
            except Exception as exc:  # noqa: BLE001 - try the next candidate
                errors.append(f"{cand.source}: {exc}")
                continue
            reply = await self._call(
                bus, DBUS, "/org/freedesktop/DBus", DBUS, "NameHasOwner", "s", [KSTARS_NAME]
            )
            if reply is not None and reply.body and reply.body[0]:
                if fallback:
                    fallback[0].disconnect()
                return bus, cand.source, True
            if fallback is None:
                fallback = (bus, cand.source)
            else:
                bus.disconnect()
        if fallback:
            return fallback[0], fallback[1], False
        raise ConnectionError("no session bus reachable: " + ("; ".join(errors) or "none found"))

    # -- incoming signals ------------------------------------------------------------------

    def on_message(self, msg: Message) -> None:
        """dbus-fast message handler. Never raises: a bad payload is logged and skipped."""
        if msg.message_type != MessageType.SIGNAL:
            return None
        try:
            now = self._clock()
            if msg.interface == DBUS and msg.member == "NameOwnerChanged":
                events = decode_name_owner_changed(now, list(msg.body))
                for event in events:
                    self._emit(event)
                if any(e.kind == "kstars.appeared" for e in events):
                    self._spawn(self.snapshot(self._bus))
                return None
            if not (msg.path or "").startswith("/KStars"):
                return None
            if msg.path == EKOS_PATH and msg.member == "newModule" and msg.body:
                self._spawn(self.snapshot(self._bus, modules=[str(plain(msg.body[0]))]))
            for event in decode_signal(now, msg.path, msg.member, list(msg.body)):
                self._emit(event)
        except Exception:  # noqa: BLE001
            log.exception("Ekos adapter: could not decode %s %s", msg.path, msg.member)
        return None

    # -- current state -----------------------------------------------------------------------

    async def snapshot(self, bus: Any, modules: Iterable[str] | None = None) -> None:
        """Read current states. Objects Ekos hasn't created yet are skipped quietly."""
        if bus is None:
            return
        wanted = None if modules is None else set(modules)
        for module, prop, kind, table in SNAPSHOT:
            if wanted is not None and module not in wanted:
                continue
            reply = await self._call(
                bus,
                KSTARS_NAME,
                _path(module),
                "org.freedesktop.DBus.Properties",
                "Get",
                "ss",
                [_interface(module), prop],
            )
            if reply is None or not reply.body:
                continue
            code = int(plain(reply.body[0]))
            self._send(kind, {"state": states.name(table, code), "code": code, "snapshot": True})

    # -- helpers -------------------------------------------------------------------------------

    async def _call(
        self, bus: Any, dest: str, path: str, iface: str, member: str, sig: str, body: list[Any]
    ) -> Message | None:
        """A method call; ``None`` on an error reply (e.g. UnknownObject while Ekos is stopped)."""
        reply = await bus.call(
            Message(
                destination=dest,
                path=path,
                interface=iface,
                member=member,
                signature=sig,
                body=body,
            )
        )
        if reply is None or reply.message_type == MessageType.ERROR:
            log.debug("%s %s.%s: %s", path, iface, member, getattr(reply, "error_name", "no reply"))
            return None
        return reply

    def _send(self, kind: str, data: dict[str, Any]) -> None:
        self._emit(Event(self._clock(), kind, data))

    def _spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
