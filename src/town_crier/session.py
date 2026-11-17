"""Session lifecycle: Standby → Setup → Active → Closed (docs/design.md, "Workflow").

``SessionManager`` is pure logic driven by events and the clock, so a replayed night exercises
the same decisions as a live one. ``handle(event)`` returns the event tagged with its session,
plus any ``session.*`` events it caused, in order; the store records sessions from those.
``tick(now)`` applies time-based rules and must be called periodically (the service does this;
replay calls it at each event and once at the end).

Rules:

- **Setup** opens when Ekos or INDI reports a successful connection. Events during setup belong
  to the session they lead into.
- **Active** starts on the Scheduler running, the first light frame, or the GUI's Start. It can
  start straight from Standby (Town Crier started mid-night). Flats, darks and bias never start a
  session.
- **Closed** when the imaging run ends: the Scheduler stops after running (finished or aborted,
  parked or not), or, without the Scheduler, Capture reports its sequence complete. Also on the
  GUI's End, when Ekos stays gone for ``grace`` seconds (a crash followed by a restart doesn't
  split the night; the close time is when Ekos went away), or when the night is over (local
  noon). A session that closes without ever imaging is ``abandoned``. Snapshot readings never
  close a session.
- Calibration frames taken outside an active session go to the most recent imaging session.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from town_crier.events import Event

GRACE = 600.0  # seconds Ekos may be gone before the session closes
CALIBRATION = {"flat", "dark", "bias"}
SCHEDULER_BUSY = {"startup", "running", "paused", "shutdown", "loading"}
SCHEDULER_DONE = {"idle", "aborted"}


def is_snapshot(data: dict[str, Any]) -> bool:
    """A snapshot reading, which can't mark an end: after a restart mid-run the Scheduler's
    snapshot has read ``idle`` while it was running."""
    return bool(data.get("snapshot"))


def night_of(ts: float) -> str:
    """The local date of the evening a night began: 02:00 on the 17th is the night of the 16th."""
    return (datetime.fromtimestamp(ts) - timedelta(hours=12)).date().isoformat()


def _night_over(night: str, now: float) -> bool:
    """True from local noon the day after the night's evening."""
    noon_after = datetime.fromisoformat(night) + timedelta(days=1, hours=12)
    return datetime.fromtimestamp(now) >= noon_after


class SessionManager:
    def __init__(
        self,
        *,
        grace: float = GRACE,
        new_id: Callable[[], str] = lambda: uuid.uuid4().hex,
        open_session: dict[str, Any] | None = None,
        last_imaging_id: str | None = None,
        last_activity: float | None = None,
    ) -> None:
        self.grace = grace
        self._new_id = new_id
        self.state = "standby"
        self.session_id: str | None = None
        self.night: str | None = None
        self.started = False
        self.last_imaging_id = last_imaging_id
        self._last_activity = last_activity
        self._ekos_lost_at: float | None = None
        self._scheduler_ran = False  # this session's run is the Scheduler's
        self._scheduler_busy = False  # the Scheduler is driving Capture right now
        if open_session:  # the service restarted during a session
            self.session_id = open_session["id"]
            self.state = open_session["state"]
            self.night = open_session["night"]
            self.started = open_session.get("started_at") is not None

    @classmethod
    def resume(cls, store: Any, **kwargs: Any) -> SessionManager:
        """Pick up where the store left off."""
        row = store.open_session()
        last = store.last_imaging_session()
        return cls(
            open_session=dict(row) if row else None,
            last_imaging_id=last["id"] if last else None,
            last_activity=store.last_event_ts(row["id"]) if row else None,
            **kwargs,
        )

    # -- events --------------------------------------------------------------------------

    def handle(self, event: Event) -> list[Event]:
        out = self.tick(event.ts)
        kind, d = event.kind, event.data
        state = d.get("state")
        done: str | None = None  # the imaging run ended, for this reason

        if kind in ("ekos.state", "indi.state") and state == "success":
            self._ekos_lost_at = None
            if self.state == "standby":
                out.append(self._open(event.ts))
        elif (kind in ("ekos.state", "indi.state") and state in ("idle", "error")) or (
            kind == "kstars.vanished"
        ):
            if self.state != "standby" and self._ekos_lost_at is None:
                self._ekos_lost_at = event.ts
        elif kind == "scheduler.state":
            self._scheduler_busy = state in SCHEDULER_BUSY
            if state == "running":
                self._scheduler_ran = True
                out += self._start(event.ts, "scheduler")
            elif state in SCHEDULER_DONE and self._scheduler_ran and not is_snapshot(d):
                done = "scheduler_finished" if state == "idle" else "scheduler_aborted"
        elif kind == "capture.frame" and d.get("type") == "light":
            out += self._start(event.ts, "light_frame")
        elif (
            kind == "capture.state"
            and state == "complete"
            and not self._scheduler_busy
            and not is_snapshot(d)
        ):
            done = "sequence_complete"

        if kind in ("capture.frame", "scheduler.state") and state != "idle":
            self._ekos_lost_at = None  # imaging carries on: Ekos is evidently back

        out.append(self._tag(event))
        if self.state != "standby":
            self._last_activity = event.ts

        if done and self.state == "active":
            out.append(self._close(event.ts, done))
        return out

    def tick(self, now: float) -> list[Event]:
        if self.state == "standby":
            return []
        if self._ekos_lost_at is not None and now - self._ekos_lost_at >= self.grace:
            return [self._close(now, "ekos_stopped", ended_at=self._ekos_lost_at)]
        if self.night and _night_over(self.night, now):
            return [self._close(now, "night_ended", ended_at=self._last_activity or now)]
        return []

    # -- GUI overrides ---------------------------------------------------------------------

    def start(self, now: float) -> list[Event]:
        return self.tick(now) + self._start(now, "user")

    def end(self, now: float) -> list[Event]:
        out = self.tick(now)
        if self.state != "standby":
            out.append(self._close(now, "user"))
        return out

    # -- transitions -----------------------------------------------------------------------

    def _open(self, ts: float) -> Event:
        self.state, self.session_id, self.night, self.started = (
            "setup",
            self._new_id(),
            night_of(ts),
            False,
        )
        return self._lifecycle(ts, "session.setup", {})

    def _start(self, ts: float, reason: str) -> list[Event]:
        if self.state == "active":
            return []
        out = []
        if self.state == "standby":
            self.session_id, self.night = self._new_id(), night_of(ts)
        self.state, self.started = "active", True
        self._ekos_lost_at = None
        out.append(self._lifecycle(ts, "session.started", {"reason": reason}))
        return out

    def _close(self, ts: float, reason: str, ended_at: float | None = None) -> Event:
        final = "closed" if self.started else "abandoned"
        event = self._lifecycle(
            ts, "session.closed", {"state": final, "reason": reason, "ended_at": ended_at or ts}
        )
        if self.started:
            self.last_imaging_id = self.session_id
        self.state, self.session_id, self.night, self.started = "standby", None, None, False
        self._ekos_lost_at = None
        self._scheduler_ran = False
        return event

    def _lifecycle(self, ts: float, kind: str, data: dict[str, Any]) -> Event:
        return Event(ts, kind, {"night": self.night, **data}, "session", self.session_id)

    def _tag(self, event: Event) -> Event:
        session = self.session_id
        if (
            self.state != "active"
            and event.kind == "capture.frame"
            and event.data.get("type") in CALIBRATION
            and self.last_imaging_id
        ):
            session = self.last_imaging_id
        return replace(event, session_id=session)


def run(
    events: Iterable[Event], manager: SessionManager, end: float | None = None
) -> Iterator[Event]:
    """Pass events through the manager; then tick at ``end`` (default: last event + grace), as
    the service's clock would after the recording stops."""
    last = None
    for event in events:
        last = event.ts
        yield from manager.handle(event)
    if end is None and last is not None:
        end = last + manager.grace
    if end is not None:
        yield from manager.tick(end)
