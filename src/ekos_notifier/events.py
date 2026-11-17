"""The event type that adapters produce and the store records."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Event:
    """One thing that happened, as reported by a source.

    ``kind`` is a dotted name (``capture.frame``, ``mount.park``); ``data`` holds plain JSON-able
    values only, so events can be written to SQLite and replayed as they are. ``session_id`` is
    set by the session manager, never by adapters.
    """

    ts: float
    kind: str
    data: dict[str, Any] = field(default_factory=dict)
    source: str = "ekos"
    session_id: str | None = None
