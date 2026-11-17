"""The "Guiding lost" alert (High) and its all-clear (docs/design.md, "Alerts").

Guiding counts as lost when, after it has been guiding, Ekos's Guide module leaves its guiding
states (aborted, a lost star being reacquired, a calibration or dither error, stopped…) or PHD2
reports it has lost the star. Looping and recalibrating don't count as recovered: a guider stuck
retrying is still not guiding. Only actually guiding again (or Ekos suspending it on purpose)
resets the clock.

Intent-aware: the clock only runs while guiding is expected. It's paused, and restarts from zero
afterwards, while Ekos does something that stops guiding on purpose or makes it unnecessary:

- the mount is slewing, parking or not tracking (wall flats turn tracking off);
- Capture is flipping, plate solving, focusing, calibrating the guider, or isn't capturing at
  all (idle, paused, aborted, complete);
- Focus or Align is busy, or calibration frames (flats, darks, bias) are being taken;
- Ekos is gone (that's the "mount unparked" Emergency's business).

Capture *suspended* doesn't pause it: that's Ekos waiting for guiding to come back. An alert is
sent only during an active session, once per outage, after ``recovery`` seconds; when guiding
resumes a Normal "Guiding recovered" follows. A session closing ends the outage silently.
"""

from __future__ import annotations

import time
from typing import Any

from ekos_notifier.events import Event

RECOVERY = 300.0

GUIDING_OK = {
    "guiding",
    "dithering",
    "manual_dithering",
    "dithering_success",
    "dithering_settle",
    "suspended",  # Ekos pauses guiding on purpose (autofocus, flats)
}
CAPTURE_PAUSES = {
    "idle",
    "paused",
    "aborted",
    "complete",
    "meridian_flip",
    "aligning",
    "focusing",
    "filter_focus",
    "calibrating",
    "setting_temperature",
    "setting_rotator",
}
MOUNT_PAUSES = {"slewing", "moving", "parking", "parked", "idle"}
FOCUS_BUSY = {"waiting", "in_progress", "framing", "changing_filter"}
ALIGN_BUSY = {"in_progress", "syncing", "slewing", "rotating"}
CALIBRATION = {"flat", "dark", "bias"}


def _clock(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def _minutes(seconds: float) -> str:
    return f"{max(1, round(seconds / 60))} min"


class GuidingRule:
    def __init__(self, recovery: float = RECOVERY) -> None:
        self.recovery = recovery
        self._session: str | None = None
        self._was_guiding = False
        self._ekos_guide: str | None = None
        self._phd2_lost = False
        self._phd2: str | None = None  # PHD2's state, while connected
        self._lost_at: float | None = None  # when guiding stopped (None while guiding)
        self._clock_from: float | None = None  # when the current unpaused stretch began
        self._alerted = False
        self._ekos_gone = False
        self._mount: str | None = None
        self._capture: str | None = None
        self._focus: str | None = None
        self._align: str | None = None
        self._calibration_frames = False
        self._hints: dict[str, str] = {}

    # -- events ------------------------------------------------------------------------------

    def handle(self, event: Event) -> list[Event]:
        kind, d = event.kind, event.data
        state = d.get("state")
        out: list[Event] = []

        if kind == "session.started":
            self._session = event.session_id
        elif kind == "session.closed":
            self._session = None
            self._end_outage()
        elif kind == "guide.state":
            self._ekos_guide = state
        elif kind == "phd2.state":
            self._phd2_lost = state == "lost_lock"
            self._phd2 = state
            if state == "guiding":
                self._was_guiding = True
        elif kind == "phd2.lost":
            self._phd2_lost = False  # PHD2 gone: Ekos's own state says the rest
            self._phd2 = None
        elif kind == "phd2.star_lost" and d.get("status"):
            self._hints["star"] = str(d["status"])
        elif kind == "phd2.alert" and d.get("message"):
            self._hints["phd2_alert"] = str(d["message"]).splitlines()[0]
        elif kind == "mount.state":
            self._mount = state
        elif kind == "capture.state":
            self._capture = state
        elif kind == "focus.state":
            self._focus = state
        elif kind == "align.state":
            self._align = state
        elif kind == "capture.frame" and d.get("type"):
            self._calibration_frames = d["type"] in CALIBRATION
        elif kind in ("ekos.state", "indi.state"):
            self._ekos_gone = state in ("idle", "error")
        elif kind == "kstars.vanished":
            self._ekos_gone = True

        if self._guiding_ok():
            if self._ekos_guide == "guiding":
                self._was_guiding = True
            out += self._recovered(event.ts)
            self._hints.clear()  # hints describe an outage, not a blip that already passed
        elif self._was_guiding and self._lost_at is None:
            self._lost_at = event.ts
        return out + self.tick(event.ts)

    def tick(self, now: float) -> list[Event]:
        if self._lost_at is None or self._alerted:
            return []
        if self._session is None or self._paused():
            self._clock_from = None
            return []
        if self._clock_from is None:
            self._clock_from = now
        if now - self._clock_from < self.recovery:
            return []
        self._alerted = True
        return [self._lost_alert(now)]

    # -- helpers -------------------------------------------------------------------------------

    def _guiding_ok(self) -> bool:
        if self._phd2_lost:
            return False
        if self._ekos_guide is not None:
            return self._ekos_guide in GUIDING_OK
        # Ekos's guide state not seen yet (joined mid-night): go by PHD2, if it's there.
        return self._phd2 is None or self._phd2 in ("guiding", "paused")

    def _paused(self) -> bool:
        return (
            self._ekos_gone
            or self._mount in MOUNT_PAUSES
            or self._capture in CAPTURE_PAUSES
            or self._focus in FOCUS_BUSY
            or self._align in ALIGN_BUSY
            or self._calibration_frames
        )

    def _recovered(self, ts: float) -> list[Event]:
        if self._lost_at is None:
            return []
        lost_at, alerted = self._lost_at, self._alerted
        self._end_outage()
        if not alerted or self._session is None:
            return []
        return [
            _alert(
                ts,
                self._session,
                "guiding_recovered",
                "normal",
                "Guiding recovered",
                f"Guiding is back at {_clock(ts)}, {_minutes(ts - lost_at)} after it stopped.",
                immediate=True,
            )
        ]

    def _end_outage(self) -> None:
        self._lost_at = self._clock_from = None
        self._alerted = False
        self._hints = {}

    def _lost_alert(self, now: float) -> Event:
        assert self._lost_at is not None
        lines = [
            f"Guiding stopped at {_clock(self._lost_at)} and hasn't recovered "
            f"({_minutes(now - self._lost_at)})."
        ]
        if self._phd2_lost or "star" in self._hints:
            lines.append(f"PHD2: {self._hints.get('star', 'star lost')}.")
        if "phd2_alert" in self._hints:
            lines.append(f"PHD2 says: {self._hints['phd2_alert']}")
        details = []
        if self._ekos_guide:
            details.append(f"Ekos guide: {self._ekos_guide.replace('_', ' ')}")
        if self._capture:
            details.append(f"capture: {self._capture.replace('_', ' ')}")
        if details:
            lines.append("; ".join(details).capitalize() + ".")
        lines.append("Check the sky, the guide camera and guide star.")
        return _alert(now, self._session, "guiding_lost", "high", "Guiding lost", "\n".join(lines))


def _alert(
    ts: float,
    sid: str | None,
    rule: str,
    priority: str,
    title: str,
    body: str,
    immediate: bool = False,
) -> Event:
    data: dict[str, Any] = {
        "rule": rule,
        "priority": priority,
        "title": title,
        "body": body,
        "immediate": immediate,
    }
    return Event(ts, "alert", data, "rules", sid)
