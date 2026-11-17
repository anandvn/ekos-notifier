"""The "Guiding lost" alert (High) and its all-clear (docs/design.md, "Alerts").

Guiding counts as lost when, after it has been guiding, Ekos's Guide module leaves its guiding
states (aborted, a lost star being reacquired, a calibration or dither error, stopped…) or PHD2
reports it has lost the star. Looping and recalibrating don't count as recovered: a guider stuck
retrying is still not guiding. Only actually guiding again (or Ekos suspending it on purpose)
resets the clock.

Intent-aware: the clock only runs while guiding is expected. It's paused, and restarts from zero
afterwards, while Ekos does something that stops guiding on purpose or makes it unnecessary:

- the mount is slewing, parking or parked;
- Capture is flipping, plate solving, focusing or calibrating the guider, or it had already
  stopped (idle, paused, aborted, complete) before guiding did. A capture Ekos aborts *because*
  guiding stopped doesn't count: that's the failure itself;
- Focus or Align is busy, or calibration frames (flats, darks, bias) are being taken;
- Ekos is gone (that's the "mount unparked" Emergency's business).

Capture *suspended* doesn't pause it: that's Ekos waiting for guiding to come back. An alert is
sent only during an active session, once per outage, after ``recovery`` seconds; when guiding
resumes a Normal "Guiding recovered" follows. A session closing ends the outage silently.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
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
# Capture not capturing pauses the clock only if it stopped before guiding did. Without the
# Scheduler, Ekos aborts the capture *because* guiding stopped ("Autoguiding stopped.
# Aborting...", seen live 2026-11-17): that's the failure itself, not the user stopping.
CAPTURE_STOPPED = {"idle", "paused", "aborted", "complete"}
CAPTURE_PAUSES = {
    "meridian_flip",
    "aligning",
    "focusing",
    "filter_focus",
    "calibrating",
    "setting_temperature",
    "setting_rotator",
}
# Not "idle": a mount that stopped tracking during lights is a reason to alert, not to pause
# (seen live: guiding failed because the mount wasn't tracking).
MOUNT_PAUSES = {"slewing", "moving", "parking", "parked"}
FOCUS_BUSY = {"waiting", "in_progress", "framing", "changing_filter"}
ALIGN_BUSY = {"in_progress", "syncing", "slewing", "rotating"}
CALIBRATION = {"flat", "dark", "bias"}
# PHD2 tags some advice as a warning; it isn't something to act on mid-night.
PHD2_ADVICE = ("For best results",)


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
        self._seq = 0  # event order: same-second events still have an order
        self._capture_stopped_seq: int | None = None
        self._lost_seq: int | None = None
        self._focus: str | None = None
        self._align: str | None = None
        self._calibration_frames = False
        self._hints: dict[str, str] = {}
        self._phd2_sent: set[str] = set()  # PHD2 warnings already sent this session
        self._rms: tuple[float, float] | None = None  # Ekos's latest guide RMS (RA, DEC), arcsec

    # -- events ------------------------------------------------------------------------------

    def handle(self, event: Event) -> list[Event]:
        kind, d = event.kind, event.data
        self._seq += 1
        state = d.get("state")
        out: list[Event] = []

        if kind == "session.started":
            self.resume(event.session_id)
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
            out += self._phd2_warning(event)
        elif kind == "guide.rms" and d.get("ra") is not None and d.get("dec") is not None:
            self._rms = (float(d["ra"]), float(d["dec"]))
        elif kind == "mount.state":
            self._mount = state
        elif kind == "capture.state":
            self._capture = state
            if state not in CAPTURE_STOPPED:
                self._capture_stopped_seq = None
            elif self._capture_stopped_seq is None:
                self._capture_stopped_seq = self._seq
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
            self._lost_seq = self._seq
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
            or self._capture_stopped_first()
            or self._focus in FOCUS_BUSY
            or self._align in ALIGN_BUSY
            or self._calibration_frames
        )

    def _capture_stopped_first(self) -> bool:
        """Capture is stopped, and was before guiding was lost: the user (or the run) stopped."""
        return (
            self._capture_stopped_seq is not None
            and self._lost_seq is not None
            and self._capture_stopped_seq < self._lost_seq
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

    def resume(self, session_id: str | None, phd2_sent: Iterable[str] = ()) -> None:
        """A session is active: from ``session.started``, or picked up after a service restart
        (with the PHD2 messages already sent in it, so a restart doesn't send them again)."""
        if session_id != self._session:
            self._phd2_sent = set()
        self._phd2_sent.update(phd2_sent)
        self._session = session_id

    def _phd2_warning(self, event: Event) -> list[Event]:
        """PHD2's own warnings and errors, once per message per session."""
        d = event.data
        message = str(d["message"]).strip()
        if (
            self._session is None
            # The run is over or Ekos is going: PHD2 then warns that INDI disconnected the
            # camera and mount (seen at the end of every Scheduler run).
            or self._capture == "complete"
            or self._ekos_gone
            or d.get("type") not in ("warning", "error")
            or message.startswith(PHD2_ADVICE)
            or message in self._phd2_sent
        ):
            return []
        self._phd2_sent.add(message)
        lines = [message]
        if self._rms is not None:
            ra, dec = self._rms
            lines.append(f"Guide RMS now: RA {ra:.2f}\u2033, DEC {dec:.2f}\u2033.")
        lines.append("Repeats of this message won't be sent again this session.")
        title = "PHD2 error" if d.get("type") == "error" else "PHD2 warning"
        alert = _alert(event.ts, self._session, "phd2_warning", "high", title, "\n".join(lines))
        alert.data["message"] = message  # what a restart checks against
        return [alert]

    def _end_outage(self) -> None:
        self._lost_at = self._clock_from = self._lost_seq = None
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
        if self._mount == "idle":
            details.append("mount: not tracking")
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
