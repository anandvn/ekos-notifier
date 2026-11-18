"""Shared pieces for alert rules: building alert events, wording times, and a base class that
knows whether a session is active.

An alert is an ``Event`` of kind ``alert`` (source ``rules``) with ``rule``, ``priority``
(emergency | high | normal | quiet), ``title``, ``body`` and ``immediate`` (a Normal alert sent at
once rather than in the hourly digest). The notifier sends them from the store.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

from ekos_notifier.events import Event


def make_alert(
    ts: float,
    session_id: str | None,
    rule: str,
    priority: str,
    title: str,
    body: str,
    *,
    immediate: bool = False,
    **extra: Any,
) -> Event:
    data: dict[str, Any] = {
        "rule": rule,
        "priority": priority,
        "title": title,
        "body": body,
        "immediate": immediate,
        **extra,
    }
    return Event(ts, "alert", data, "rules", session_id)


def hhmm(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def minutes(seconds: float) -> str:
    return f"{max(1, round(seconds / 60))} min"


class SessionRule:
    """A rule that only alerts during an active session. Subclasses implement ``on_event`` and
    ``on_tick``; ``session`` is the active session's id, or None."""

    def __init__(self) -> None:
        self.session: str | None = None

    def resume(self, session_id: str | None, alerts: Iterable[Event] = ()) -> None:
        self.session = session_id

    def handle(self, event: Event) -> list[Event]:
        if event.kind == "session.started":
            self.resume(event.session_id)
        out = self.on_event(event)
        if event.kind == "session.closed":
            self.session = None
            self.on_session_closed()
        return out + self.tick(event.ts)

    def tick(self, now: float) -> list[Event]:
        return self.on_tick(now) if self.session else []

    def on_event(self, event: Event) -> list[Event]:
        return []

    def on_tick(self, now: float) -> list[Event]:
        return []

    def on_session_closed(self) -> None:
        pass

    def preflight(self, now: float) -> list[tuple[bool, str]]:
        """Pre-flight checks for "Starting session": (passed, words) for what this rule can see
        of the rig right now. Nothing when it can't tell."""
        return []
