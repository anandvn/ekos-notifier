"""Live INDI source: a read-only client of the INDI server Ekos uses.

Observe-only: the client sends ``getProperties`` (a request for the property list, which changes
nothing) and never enables BLOBs, so no images cross the wire. It never sends a ``new*`` element.
INDI serves any number of clients, so Ekos's own connection is unaffected.

The server is the active Ekos profile's (``host``/``port`` in ``userdb.sqlite``; empty means
local mode on localhost:7624), re-read before each connection attempt; config can override it.
The server only runs while Ekos is connected, so "connection refused" is normal: the adapter
says once that it's waiting and keeps retrying quietly. ``run()`` never returns.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable

from ekos_notifier.ekos.settings import INDI_HOST, INDI_PORT, indi_server
from ekos_notifier.events import Event
from ekos_notifier.indi.decode import SOURCE, IndiDecoder

log = logging.getLogger(__name__)

BACKOFF = (1, 2, 5, 10, 30)
CONNECT_TIMEOUT = 5.0
GET_PROPERTIES = b'<getProperties version="1.7"/>\n'


class IndiAdapter:
    def __init__(
        self,
        emit: Callable[[Event], None],
        decoder: IndiDecoder,
        *,
        host: str | None = None,
        port: int | None = None,
        server: Callable[[], tuple[str, int] | None] = indi_server,
        clock: Callable[[], float] = time.time,
        backoff: tuple[float, ...] = BACKOFF,
    ) -> None:
        self._emit = emit
        self.decoder = decoder
        self._host, self._port = host, port
        self._server = server
        self._clock = clock
        self._backoff = backoff
        self._waiting_logged = False
        self.host, self.port = host or INDI_HOST, port or INDI_PORT

    async def run(self) -> None:
        failures = 0
        while True:
            self._pick_server()
            try:
                await self.run_once()
                failures = 0
            except asyncio.CancelledError:
                raise
            except OSError as exc:
                if not self._waiting_logged:
                    log.info(
                        "INDI server not reachable on %s:%d (%s); will keep trying",
                        self.host,
                        self.port,
                        exc,
                    )
                    self._waiting_logged = True
            except Exception as exc:  # noqa: BLE001 - supervisor: log, back off, retry
                log.warning("INDI adapter: %s", exc)
            delay = self._backoff[min(failures, len(self._backoff) - 1)]
            failures += 1
            await asyncio.sleep(delay)

    def _pick_server(self) -> None:
        try:
            found = self._server()
        except Exception:  # noqa: BLE001 - a settings quirk must not stop the adapter
            log.exception("INDI adapter: could not read the Ekos profile")
            found = None
        host, port = found or (INDI_HOST, INDI_PORT)
        self.host, self.port = self._host or host, self._port or port

    async def run_once(self) -> None:
        """One connection: request the properties, then decode the stream until it ends."""
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), CONNECT_TIMEOUT
        )
        sock = writer.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        log.info("connected to INDI on %s:%d", self.host, self.port)
        self._waiting_logged = False
        self.decoder.reset()
        self._send("indi.connected", {"host": self.host, "port": self.port})
        reason = "the INDI server closed the connection"
        parser = ET.XMLPullParser(events=("start", "end"))
        parser.feed(b"<stream>")  # INDI is a stream of top-level elements: give it a root
        depth = 0
        root: ET.Element | None = None
        try:
            writer.write(GET_PROPERTIES)
            await writer.drain()
            while True:
                try:
                    chunk = await reader.read(65536)
                except OSError as exc:
                    reason = str(exc) or type(exc).__name__
                    break
                if not chunk:
                    break
                try:
                    parser.feed(chunk)
                    for event, el in parser.read_events():
                        if event == "start":
                            if root is None:
                                root = el
                            depth += 1
                            continue
                        depth -= 1
                        if depth == 1:  # a top-level element is complete
                            self.on_element(el)
                            if root is not None:
                                root.clear()  # don't keep a night's worth of elements
                except ET.ParseError as exc:
                    reason = f"unreadable INDI stream: {exc}"
                    break
        finally:
            ts = self._clock()
            for event in self.decoder.lost(ts):
                self._emit(event)
            self._send("indi.lost", {"reason": reason})
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    def on_element(self, el: ET.Element) -> None:
        """One complete element. Never raises: a bad one is logged and skipped."""
        try:
            for event in self.decoder.feed(self._clock(), el):
                self._emit(event)
        except Exception:  # noqa: BLE001
            log.exception("INDI adapter: could not decode <%s>", el.tag)

    def _send(self, kind: str, data: dict) -> None:
        self._emit(Event(self._clock(), kind, data, SOURCE))
