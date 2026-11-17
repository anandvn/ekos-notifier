"""Alert rules: events in, ``alert`` events out (docs/design.md, "Alerts").

Like the session manager, ``Rules`` is pure logic over session-tagged events and the clock, so
replayed nights test it. Alerts are stored as events (kind ``alert``, source ``rules``) and the
notifier sends them from the store.

Alert data: ``rule``, ``priority`` (emergency | high | normal | quiet), ``title``, ``body`` and
``immediate`` (a Normal alert sent at once rather than in the hourly digest).

Rules so far:

- **Starting session** (Normal, immediate) when a session starts.
- **Session summary** (Normal, immediate) when a session that imaged closes: as soon as the
  Scheduler stops or a manual sequence completes, parked or not, with the mount's last known
  state in the message.
- **Guiding lost** (High) and **Guiding recovered**, and **PHD2 warnings** (High, PHD2's own
  warnings and errors, once per message per session): see ``ekos_notifier.guiding``.
- **Ekos stopped with the mount unparked** (Emergency): Ekos or INDI stops, or KStars vanishes,
  while the last known mount state was unparked, and Ekos isn't back within ``unparked_grace``
  seconds. Uses the last known state on purpose: the mount's properties vanish with Ekos.
  A later "parked" sends an all-clear.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from ekos_notifier.events import Event
from ekos_notifier.guiding import RECOVERY, GuidingRule

UNPARKED_GRACE = 120.0
MOVING = {"tracking", "slewing", "moving"}

START_REASONS = {
    "scheduler": "the Ekos Scheduler started",
    "light_frame": "the first light frame was saved",
    "user": "you pressed Start",
}
END_REASONS = {
    "scheduler_finished": "the Scheduler finished",
    "scheduler_aborted": "the Scheduler stopped (aborted)",
    "sequence_complete": "the capture sequence completed",
    "ekos_stopped": "Ekos stopped",
    "night_ended": "the night ended",
    "user": "you pressed End",
}


def _clock(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def _duration(seconds: float) -> str:
    minutes = int(round(seconds / 60))
    return f"{minutes // 60} h {minutes % 60:02d} min" if minutes >= 60 else f"{minutes} min"


class Rules:
    def __init__(
        self, *, unparked_grace: float = UNPARKED_GRACE, guiding_recovery: float = RECOVERY
    ) -> None:
        self.unparked_grace = unparked_grace
        self.guiding = GuidingRule(guiding_recovery)
        self._park: str | None = None
        self._mount: str | None = None
        self._gone_at: float | None = None
        self._gone_state: str | None = None
        self._gone_session: str | None = None
        self._unparked_alert_sent = False
        self._started: dict[str, float] = {}
        self._lights: dict[str, dict[tuple[str, float], int]] = defaultdict(
            lambda: defaultdict(int)
        )
        self._calibration: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    # -- events ------------------------------------------------------------------------------

    def resume(self, session_id: str, alerts: Iterable[Event] = ()) -> None:
        """The service restarted during an active session; ``alerts`` are the ones it already
        raised, so they aren't raised again."""
        sent = [
            a.data.get("message") or a.data.get("body", "").split("\n")[0]
            for a in alerts
            if a.data.get("rule") == "phd2_warning"
        ]
        self.guiding.resume(session_id, sent)

    def handle(self, event: Event) -> list[Event]:
        out = self.tick(event.ts) + self.guiding.handle(event)
        kind, d, sid = event.kind, event.data, event.session_id
        state = d.get("state")

        if kind == "mount.park":
            self._park = state
            if state == "parked" and self._unparked_alert_sent:
                self._unparked_alert_sent = False
                out.append(
                    self._alert(
                        event.ts,
                        sid,
                        "mount_unparked_cleared",
                        "normal",
                        "Mount parked",
                        "The mount is parked again. All clear.",
                    )
                )
        elif kind == "mount.state":
            self._mount = state
        elif self._ekos_gone(kind, state):
            if self._gone_at is None and self._unparked():
                self._gone_at, self._gone_session = event.ts, sid
                self._gone_state = self._park if self._park == "unparked" else self._mount
        elif kind in ("ekos.state", "indi.state") and state == "success":
            self._gone_at = None
        elif kind == "capture.frame" and sid:
            if d.get("type") == "light":
                self._lights[sid][(d.get("filter") or "no filter", d.get("exposure") or 0.0)] += 1
            elif d.get("type"):
                self._calibration[sid][d["type"]] += 1
        elif kind == "session.started":
            self._started[sid] = event.ts
            out.append(
                self._alert(
                    event.ts,
                    sid,
                    "session_started",
                    "normal",
                    "Starting session",
                    f"Night of {d['night']}: session started at {_clock(event.ts)} "
                    f"because {START_REASONS.get(d.get('reason'), d.get('reason'))}.",
                    immediate=True,
                )
            )
        elif kind == "session.closed" and d.get("state") == "closed":
            out.append(self._summary(event, sid))
        return out

    def tick(self, now: float) -> list[Event]:
        return self.guiding.tick(now) + self._unparked_tick(now)

    def _unparked_tick(self, now: float) -> list[Event]:
        if (
            self._gone_at is not None
            and not self._unparked_alert_sent
            and now - self._gone_at >= self.unparked_grace
        ):
            self._unparked_alert_sent = True
            gone_at, self._gone_at = self._gone_at, None
            return [
                self._alert(
                    now,
                    self._gone_session,
                    "ekos_stopped_mount_unparked",
                    "emergency",
                    "Mount left unparked",
                    f"Ekos stopped at {_clock(gone_at)} while the mount was "
                    f"{self._gone_state or 'unparked'}, and it hasn't come back. Nothing is "
                    "watching the mount now.\n"
                    "Park the mount or check the rig.",
                )
            ]
        return []

    # -- helpers -------------------------------------------------------------------------------

    @staticmethod
    def _ekos_gone(kind: str, state: Any) -> bool:
        return kind == "kstars.vanished" or (
            kind in ("ekos.state", "indi.state") and state in ("idle", "error")
        )

    def _unparked(self) -> bool:
        if self._park == "parked":
            return False
        return self._park == "unparked" or self._mount in MOVING

    def _mount_line(self) -> str | None:
        if self._park == "parked":
            return "Mount: parked."
        if self._unparked():
            return f"Mount: not parked (last seen {self._mount or 'unparked'})."
        return None

    def _summary(self, event: Event, sid: str | None) -> Event:
        d = event.data
        ended = d.get("ended_at", event.ts)
        lines = []
        started = self._started.pop(sid, None)
        when = f"Session ended at {_clock(ended)}"
        if started is not None:
            when += f" after {_duration(ended - started)}"
        lines.append(f"{when} because {END_REASONS.get(d.get('reason'), d.get('reason'))}.")
        lights = self._lights.pop(sid, {})
        if lights:
            total = sum(n * exp for (_, exp), n in lights.items())
            parts = [f"{f} {n}×{exp:g}s" for (f, exp), n in sorted(lights.items())]
            lines.append(f"Lights: {', '.join(parts)}. Integration {_duration(total)}.")
        else:
            lines.append("No light frames.")
        calibration = self._calibration.pop(sid, {})
        if calibration:
            lines.append(
                "Calibration: "
                + ", ".join(f"{n} {t}" for t, n in sorted(calibration.items()))
                + "."
            )
        mount = self._mount_line()
        if mount:
            lines.append(mount)
        return self._alert(
            event.ts,
            sid,
            "session_summary",
            "normal",
            "Session summary",
            "\n".join(lines),
            immediate=True,
        )

    @staticmethod
    def _alert(
        ts: float,
        sid: str | None,
        rule: str,
        priority: str,
        title: str,
        body: str,
        immediate: bool = False,
    ) -> Event:
        return Event(
            ts,
            "alert",
            {
                "rule": rule,
                "priority": priority,
                "title": title,
                "body": body,
                "immediate": immediate,
            },
            "rules",
            sid,
        )
