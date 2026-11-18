"""Live PHD2 source: connect to PHD2's event server and emit events.

Observe-only: the adapter reads the event stream and sends nothing. PHD2 serves any number of
clients, so Ekos's own connection is unaffected.

Before each connection attempt the adapter reads which guider Ekos is set to (its profile and
Guide setting, ``ekos_notifier.ekos.settings``). If it's certainly not PHD2 (the internal guider or
lin_guider), the adapter doesn't touch PHD2 at all; it re-reads the KStars settings every
``recheck`` seconds so a switch to PHD2 mid-night is picked up. If it is PHD2, it connects to
the host and port Ekos uses. If it can't tell (KStars not running yet), it tries localhost:4400.
While PHD2 isn't reachable it retries quietly and says once in the log that it's waiting. Each
change of guider setting is recorded as an ``ekos.guider`` event. ``run()`` never returns.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
from collections.abc import Callable

from ekos_notifier.ekos.settings import UNKNOWN, GuiderSettings, guider_settings
from ekos_notifier.events import Event
from ekos_notifier.phd2.decode import Phd2Decoder

log = logging.getLogger(__name__)

HOST = "127.0.0.1"
PORT = 4400
BACKOFF = (1, 2, 5, 10, 30)
RECHECK = 30.0  # seconds between config reads while Ekos uses another guider
CONNECT_TIMEOUT = 5.0
MAX_LINE = 1 << 20


class Phd2Adapter:
    def __init__(
        self,
        emit: Callable[[Event], None],
        *,
        host: str | None = None,
        port: int | None = None,
        settings: Callable[[], GuiderSettings] = guider_settings,
        clock: Callable[[], float] = time.time,
        backoff: tuple[float, ...] = BACKOFF,
        recheck: float = RECHECK,
    ) -> None:
        """``host``/``port`` given explicitly override what Ekos is set to."""
        self._emit = emit
        self._host, self._port = host, port
        self.host, self.port = host or HOST, port or PORT
        self._settings = settings
        self._clock = clock
        self._backoff = backoff
        self._recheck = recheck
        self.decoder = Phd2Decoder()
        self._waiting_logged = False
        self._guider: GuiderSettings | None = None

    async def run(self) -> None:
        failures = 0
        while True:
            if not self._check_guider():
                failures = 0
                await asyncio.sleep(self._recheck)
                continue
            try:
                await self.run_once()
                failures = 0
            except asyncio.CancelledError:
                raise
            except OSError as exc:
                if not self._waiting_logged:
                    log.info("PHD2 not reachable on %s:%d (%s); will keep trying", *self._addr, exc)
                    self._waiting_logged = True
            except Exception as exc:  # noqa: BLE001 - supervisor: log, back off, retry
                log.warning("PHD2 adapter: %s", exc, exc_info=True)  # not a connection: a bug
            delay = self._backoff[min(failures, len(self._backoff) - 1)]
            failures += 1
            await asyncio.sleep(delay)

    def _check_guider(self) -> bool:
        """Read Ekos's guider setting; False if it's certainly not PHD2."""
        try:
            current = self._settings()
        except Exception:  # noqa: BLE001 - never let a config quirk stop the adapter
            log.exception("PHD2 adapter: could not read the KStars guider setting")
            current = UNKNOWN
        if current != self._guider:
            self._guider = current
            self._emit(
                Event(
                    self._clock(),
                    "ekos.guider",
                    {
                        "guider": current.guider,
                        "profile": current.profile,
                        "phd2_host": current.phd2_host,
                        "phd2_port": current.phd2_port,
                    },
                )
            )
            if current.uses_phd2 is False:
                log.info(
                    "Ekos profile %r uses the %s guider: not connecting to PHD2",
                    current.profile,
                    current.guider,
                )
            self._waiting_logged = False
        if current.uses_phd2:
            self.host = self._host or current.phd2_host
            self.port = self._port or current.phd2_port
        else:
            self.host, self.port = self._host or HOST, self._port or PORT
        return current.uses_phd2 is not False

    @property
    def _addr(self) -> tuple[str, int]:
        return self.host, self.port

    async def run_once(self) -> None:
        """One connection: read events until PHD2 closes it."""
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port, limit=MAX_LINE), CONNECT_TIMEOUT
        )
        sock = writer.get_extra_info("socket")
        if sock is not None:  # PHD2 is silent while idle: let the OS notice a dead peer
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        log.info("connected to PHD2 on %s:%d", *self._addr)
        self._waiting_logged = False
        reason = "PHD2 closed the connection"
        try:
            while True:
                try:
                    line = await reader.readline()
                except (OSError, ValueError) as exc:  # ValueError: a line over MAX_LINE
                    reason = str(exc) or type(exc).__name__
                    break
                if not line:
                    break
                self.on_line(line)
        finally:
            for event in self.decoder.lost(self._clock(), reason):
                self._emit(event)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    def on_line(self, line: bytes) -> None:
        """One line from PHD2. Never raises: a bad line is logged and skipped."""
        if not line.strip():
            return
        try:
            msg = json.loads(line)
            if isinstance(msg, dict):
                for event in self.decoder.feed(self._clock(), msg):
                    self._emit(event)
        except Exception:  # noqa: BLE001
            log.exception("PHD2 adapter: could not decode %r", line[:200])
