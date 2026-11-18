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


# What the log calls each metric, for "INDI recognised: …" (anything else by its own name).
METRIC_WORDS = {
    "power.voltage": "battery voltage",
    "power.current": "current",
    "power.watts": "power",
    "power.outputs": "outputs",
    "env.temperature": "temperature",
    "env.humidity": "humidity",
    "env.dewpoint": "dew point",
    "camera.temperature": "camera temperature",
    "focuser.temperature": "focuser temperature",
}
REPORT_AFTER = 5.0  # seconds without a change before saying what's recognised


def recognised(caps: dict[str, list[str]]) -> str:
    """One line for the log: each device and what's read from it."""
    by_device: dict[str, list[str]] = {}
    for metric, devices in caps.items():
        for device in devices:
            by_device.setdefault(device, []).append(METRIC_WORDS.get(metric, metric))
    return "; ".join(f"{dev} ({', '.join(words)})" for dev, words in sorted(by_device.items()))


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
        self.report_after = REPORT_AFTER
        self._caps: dict[str, list[str]] | None = None  # the latest, not yet logged
        self._reported: dict[str, list[str]] | None = None  # the last logged (None: none yet)
        self._report_handle: asyncio.TimerHandle | None = None
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
                log.warning("INDI adapter: %s", exc, exc_info=True)  # not a connection: a bug
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
        self._reported = None  # say again what this connection recognises
        self.decoder.reset()
        self._capabilities({})  # nothing recognised so far: says so if it stays that way
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
            if self._report_handle is not None:  # the devices are going: nothing to report
                self._report_handle.cancel()
                self._report_handle = None
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
                if event.kind == "indi.capabilities":
                    self._capabilities(event.data.get("metrics", {}))
        except Exception:  # noqa: BLE001
            log.exception("INDI adapter: could not decode <%s>", el.tag)

    # -- what's recognised, for the log ------------------------------------------------------

    def _capabilities(self, caps: dict[str, list[str]]) -> None:
        """Devices are recognised property by property as they connect: report once it has
        settled, so a box connecting gives one line, not a dozen."""
        self._caps = caps
        if self._report_handle is not None:
            self._report_handle.cancel()
            self._report_handle = None
        if self.report_after <= 0:
            self._report()
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # no loop (a test feeding elements by hand): say it now
            self._report()
            return
        self._report_handle = loop.call_later(self.report_after, self._report)

    def _report(self) -> None:
        self._report_handle = None
        caps = self._caps or {}
        if caps == self._reported:
            return
        if caps:
            log.info("INDI recognised: %s", recognised(caps))
        elif self._reported:
            log.info("INDI: no power box, weather sensor or camera temperature recognised now")
        else:
            log.info(
                "INDI: no power box, weather sensor or camera temperature recognised yet; "
                "they're picked up as Ekos connects them (not yours? see `ekos-notifier "
                "indi-dump` and the README)"
            )
        self._reported = caps

    def _send(self, kind: str, data: dict) -> None:
        self._emit(Event(self._clock(), kind, data, SOURCE))
