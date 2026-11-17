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

from ekos_notifier.events import Event
from ekos_notifier.rules import Rules
from ekos_notifier.session import SessionManager

log = logging.getLogger(__name__)

TICK = 10.0  # seconds between clock ticks


class Pipeline:
    def __init__(
        self,
        output: Callable[[Event], None],
        *,
        manager: SessionManager | None = None,
        rules: Rules | None = None,
    ) -> None:
        self.output = output
        self.manager = manager or SessionManager()
        self.rules = rules or Rules()

    def emit(self, event: Event) -> None:
        self._route(self.manager.handle(event))

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
