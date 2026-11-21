"""Event routing shared by the live service and replay: source → sessions → rules → output.

``emit(event)`` takes an adapter event; ``tick(now)`` advances the clock for time-based rules.
The output callable receives every event to store: the session-tagged originals, ``session.*``
lifecycle events and ``alert`` events, in order.

``simulate()`` drives a pipeline from recorded events, ticking at regular steps of recorded time
the way the live clock does, so timers (a 2-minute grace, a 10-minute grace) fire when they
would have on the night, not when the next event happens to arrive.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import replace

from ekos_notifier.events import Event
from ekos_notifier.rules import Rules
from ekos_notifier.session import SessionManager
from ekos_notifier.sources import Marker

log = logging.getLogger(__name__)

TICK = 10.0  # seconds between clock ticks
# A frame saved here is a preview, not a light to keep: the StellarMate app's one-shot captures
# (focusing, polar alignment, framing) go through Ekos's Capture as "light" frames saved to
# /tmp/mcp_cache_<camera>.fits (seen live, issues #52 and #53), as do Ekos's own unsaved
# previews. Lights worth keeping are never written where a reboot empties them.
TEMP_FOLDERS = ("/tmp/", "/var/tmp/")


def classify(event: Event) -> Event:
    """``capture.frame`` saved to a temporary folder becomes ``capture.preview``: recorded, but no
    rule counts it (it starts no session, isn't a light, and doesn't move the disk check)."""
    if event.kind == "capture.frame" and str(event.data.get("file") or "").startswith(TEMP_FOLDERS):
        return replace(event, kind="capture.preview")
    return event


class Pipeline:
    def __init__(
        self,
        output: Callable[[Event], None],
        *,
        manager: SessionManager | None = None,
        rules: Rules | None = None,
    ) -> None:
        self.output = output
        self.sources = Marker()
        self.manager = manager or SessionManager()
        self.rules = rules or Rules()

    def emit(self, event: Event) -> None:
        # Here, not in the D-Bus decoder, so nights stored before this also replay right.
        self._route(self.manager.handle(self.sources.mark(classify(event))))

    def tick(self, now: float) -> None:
        self._route(self.manager.tick(now))
        self._put(self.rules.tick(now))

    def _route(self, events: Iterable[Event]) -> None:
        for event in events:
            self._put([event])
            self._put(self.rules.handle(event))

    def _put(self, events: Iterable[Event]) -> None:
        for event in events:
            if event.kind == "alert":
                log.info("alert %s: %s", event.data["priority"], event.data["title"])
            elif event.kind.startswith("session."):
                log.info(
                    "%s %s (%s)",
                    event.kind,
                    event.session_id,
                    event.data.get("reason") or event.data.get("night"),
                )
            self.output(event)


def simulate(
    events: Iterable[Event], pipeline: Pipeline, *, tick: float = TICK, tail: float | None = None
) -> None:
    """Feed recorded events, ticking every ``tick`` seconds of recorded time in between, and for
    ``tail`` seconds after the last event (default: long enough for every grace period)."""
    next_tick = None
    last = None
    for event in events:
        if next_tick is None:
            next_tick = event.ts + tick
        while next_tick <= event.ts:
            pipeline.tick(next_tick)
            next_tick += tick
        pipeline.emit(event)
        last = event.ts
    if last is None:
        return
    if tail is None:
        tail = max(pipeline.manager.grace, pipeline.rules.unparked_grace) + tick
    while next_tick <= last + tail:
        pipeline.tick(next_tick)
        next_tick += tick
