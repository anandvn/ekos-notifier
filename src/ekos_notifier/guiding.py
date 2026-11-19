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

import functools
import math
import re
import time
from collections.abc import Callable, Iterable
from typing import Any

from ekos_notifier.alerts import SessionRule, hhmm, make_alert, minutes
from ekos_notifier.ekos import translation
from ekos_notifier.ekos.translation import NUMBER, localized, plain
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
# PHD2 tags some advice as a warning; it isn't something to act on mid-night. PHD2 translates it,
# so its translations in the PHD2 catalogs installed here are recognised too.
PHD2_ADVICE = ("For best results",)
PHD2_LOCALES = (
    "/usr/share/locale",
    "/usr/local/share/locale",
    "~/.local/share/flatpak/app/org.openphdguiding.OpenPHDGuiding/current/active/files/share/locale",
    "/var/lib/flatpak/app/org.openphdguiding.OpenPHDGuiding/current/active/files/share/locale",
)


@functools.cache
def phd2_advice() -> tuple[str, ...]:
    return translation.translated_prefixes(PHD2_ADVICE, "phd2", PHD2_LOCALES)


PHD2_HOLD = 10.0  # seconds a PHD2 warning waits, in case Ekos is shutting down


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
        self._phd2_pending: list[tuple[float, Event]] = []  # (send at, alert), held PHD2_HOLD s
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
            self._phd2_pending.clear()
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
            if self._ekos_gone:
                self._phd2_pending.clear()
        elif kind == "kstars.vanished":
            self._ekos_gone = True
            self._phd2_pending.clear()
        elif kind == "ekos.log" and "Disconnecting INDI devices" in str(d.get("text", "")):
            # Ekos is shutting down (the user, or the Scheduler's end): PHD2 is about to warn
            # that the camera and mount disconnected, just before INDI reports idle (seen live).
            self._phd2_pending.clear()

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
        return self._due_phd2(now) + self._lost_tick(now)

    def _due_phd2(self, now: float) -> list[Event]:
        due = [a for t, a in self._phd2_pending if t <= now]
        if not due or self._session is None:
            return []
        self._phd2_pending = [(t, a) for t, a in self._phd2_pending if t > now]
        out = []
        for alert in due:
            if alert.data["message"] not in self._phd2_sent:
                self._phd2_sent.add(alert.data["message"])
                out.append(Event(now, alert.kind, alert.data, alert.source, self._session))
        return out

    def _lost_tick(self, now: float) -> list[Event]:
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
        """PHD2's own warnings and errors, once per message per session. Held ``PHD2_HOLD``
        seconds and dropped if Ekos shuts down meanwhile."""
        d = event.data
        message = str(d["message"]).strip()
        if (
            self._session is None
            # The run is over or Ekos is going: PHD2 then warns that INDI disconnected the
            # camera and mount (seen at the end of every Scheduler run).
            or self._capture == "complete"
            or self._ekos_gone
            or d.get("type") not in ("warning", "error")
            or message.startswith(phd2_advice())
            or message in self._phd2_sent
            or any(a.data["message"] == message for _, a in self._phd2_pending)
        ):
            return []
        lines = [message]
        if self._rms is not None:
            ra, dec = self._rms
            lines.append(f"Guide RMS now: RA {ra:.2f}\u2033, DEC {dec:.2f}\u2033.")
        lines.append("Repeats of this message won't be sent again this session.")
        title = "PHD2 error" if d.get("type") == "error" else "PHD2 warning"
        alert = _alert(event.ts, self._session, "phd2_warning", "high", title, "\n".join(lines))
        alert.data["message"] = message  # what a restart checks against
        self._phd2_pending.append((event.ts + PHD2_HOLD, alert))
        return []

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


class PoorGuidingRule(SessionRule):
    """ "Guiding poor" (High), following Ekos's own guide-deviation limit.

    - **Ekos's limit is on** for the running job: Ekos suspends an exposure when deviation goes
      over it ("Guiding deviation 3.791 exceeded limit value of 0.5 arcsecs …, suspending
      exposure"), resumes once under its separate resume limit ("… is now lower than limit value
      of 2 arcsecs, resuming exposure"), and can trip again seconds later (seen live). Alert when
      Ekos has been suspending exposures and no frame has finished for ``window`` seconds.
    - **Ekos's limit is off** (Ekos does nothing about poor guiding): watch Ekos's guide RMS,
      total √(RA² + DEC²) in arcseconds, against Ekos's own deviation value (``limit()``, from
      ``kstarsrc`` ``[Capture] GuideDeviation``, default 2″), and alert after ``window`` seconds
      over it while guiding. Guiding stopping (lost star, flip, recalibration) restarts the window;
      dithering doesn't.
      Once Ekos is seen enforcing its limit in a session, only that path is used.

    A Normal "Guiding back within limits" follows: when a frame finishes (Ekos path), or after the
    RMS has stayed under the limit for ``clear_after`` seconds. ``window`` 0 turns the rule off.
    Ekos logs in the KStars language: the adapter turns its lines back into English
    (ekos.translation), and reads its numbers in the format KStars wrote them in.
    """

    SETTINGS = ("window",)

    # "exceeded" names the limit that suspends exposures; "still higher" and "now lower" name the
    # separate, usually looser, limit Ekos resumes at (2" when the trip limit was 0.5", live).
    EXCEEDED = re.compile(
        rf"deviation (?:at capture startup )?({NUMBER}) exceeded limit(?: value of)? "
        rf"({NUMBER}) arcsecs"
    )
    DEVIATION = re.compile(
        rf"deviation (?:at capture startup )?({NUMBER}) (?:is still higher than|is now lower than"
        rf"|below) limit value of ({NUMBER}) arcsecs"
    )
    STOPPED = {"aborted", "complete", "paused"}

    def __init__(
        self,
        window: float = 600.0,
        limit: Callable[[], float | None] = lambda: None,
        clear_after: float = 120.0,
    ) -> None:
        super().__init__()
        self.window = window
        self._limit_source = limit
        self.clear_after = clear_after
        self.limit = DEFAULT_DEVIATION
        self._enforced = False  # Ekos was seen enforcing its limit this session
        self._held_since: float | None = None  # first suspension since the last finished frame
        self._ekos_limit: float | None = None  # the limit that suspends exposures
        self._deviation: float | None = None  # Ekos's latest reported deviation
        self._guiding = False
        self._rms: tuple[float, float] | None = None
        self._bad_since: float | None = None
        self._good_since: float | None = None
        self._alerted = False

    def resume(self, session_id: str | None, alerts: Iterable[Event] = ()) -> None:
        if session_id != self.session:
            try:
                self.limit = self._limit_source() or DEFAULT_DEVIATION
            except Exception:  # noqa: BLE001 - a settings quirk must not stop the rules
                self.limit = DEFAULT_DEVIATION
        super().resume(session_id, alerts)

    def on_event(self, event: Event) -> list[Event]:
        if self.window <= 0:
            return []
        kind, d = event.kind, event.data
        out: list[Event] = []
        if kind == "ekos.log" and d.get("module") == "Capture" and d.get("text"):
            text = str(d["text"])
            if m := self.EXCEEDED.search(text):
                self._enforced = True
                # KStars writes the deviation plainly, its limit in its locale
                self._deviation, self._ekos_limit = plain(m.group(1)), localized(m.group(2), d)
                if self._held_since is None:
                    self._held_since = event.ts
            elif m := self.DEVIATION.search(text):
                self._deviation = plain(m.group(1))
        elif kind == "capture.frame" and self._enforced:
            # Ekos resuming isn't recovery (it can trip again within seconds); a finished frame is.
            self._held_since = None
            if self._alerted:
                self._alerted = False
                out.append(self._back(event.ts))
        elif kind == "capture.state" and d.get("state") in self.STOPPED:
            self._held_since = None  # capture stopped: nothing is being held any more
        elif kind == "guide.state":
            # Dithering and Ekos's own suspensions are part of guiding: with a dither every frame
            # or two, resetting on them would mean the window never fills.
            self._guiding = d.get("state") in GUIDING_OK
            if not self._guiding:
                self._bad_since = None
        elif kind == "guide.rms" and d.get("ra") is not None and d.get("dec") is not None:
            self._rms = (float(d["ra"]), float(d["dec"]))
            if self._total() > self.limit:
                self._good_since = None
                if self._guiding and self._bad_since is None:
                    self._bad_since = event.ts
            else:
                self._bad_since = None
                if self._good_since is None:
                    self._good_since = event.ts
        return out

    def on_tick(self, now: float) -> list[Event]:
        if self.window <= 0 or self._alerted and self._enforced:
            return []
        if self._enforced:
            if self._held_since is not None and now - self._held_since >= self.window:
                self._alerted = True
                return [self._poor_ekos(now)]
            return []
        if self._rms is None:
            return []
        if not self._alerted:
            if (
                self._guiding
                and self._bad_since is not None
                and now - self._bad_since >= self.window
            ):
                self._alerted = True
                return [self._poor_rms(now)]
        elif self._good_since is not None and now - self._good_since >= self.clear_after:
            self._alerted = False
            return [self._back(now)]
        return []

    def on_session_closed(self) -> None:
        self._bad_since = self._good_since = self._held_since = None
        self._alerted = self._enforced = False
        self._ekos_limit = self._deviation = None

    def _total(self) -> float:
        assert self._rms is not None
        return math.hypot(*self._rms)

    def _values(self) -> str:
        if self._rms is None:
            return ""
        ra, dec = self._rms
        return f"RA {ra:.2f}\u2033, DEC {dec:.2f}\u2033 (total {self._total():.2f}\u2033)"

    def _poor_ekos(self, now: float) -> Event:
        assert self._held_since is not None
        lines = [
            "Ekos keeps suspending exposures for guide deviation: no frame has finished since "
            f"{hhmm(self._held_since)} ({minutes(now - self._held_since)})."
        ]
        if self._ekos_limit is not None:
            line = f"Its limit is {self._ekos_limit:g}\u2033"
            if self._deviation is not None:
                line += f"; latest deviation {self._deviation:g}\u2033"
            lines.append(line + ".")
        if self._rms is not None:
            lines.append(f"Guide RMS: {self._values()}.")
        lines.append(ADVICE)
        return make_alert(
            now, self.session, "guiding_poor", "high", "Guiding poor", "\n".join(lines)
        )

    def _poor_rms(self, now: float) -> Event:
        body = (
            f"Guide RMS has been over {self.limit:g}\u2033 (Ekos's guide deviation limit) for "
            f"{minutes(self.window)}: {self._values()}.\n"
            "Ekos isn't enforcing that limit for this sequence, so it keeps exposing.\n" + ADVICE
        )
        return make_alert(now, self.session, "guiding_poor", "high", "Guiding poor", body)

    def _back(self, now: float) -> Event:
        if self._enforced:
            body = f"Exposures are finishing again: a frame completed at {hhmm(now)}."
        else:
            body = f"Guiding is back under {self.limit:g}\u2033."
        if self._rms is not None:
            body += f" Guide RMS: {self._values()}."
        return make_alert(
            now,
            self.session,
            "guiding_poor_cleared",
            "normal",
            "Guiding back within limits",
            body,
            immediate=True,
        )


DEFAULT_DEVIATION = 2.0  # Ekos's own default (kstars.kcfg GuideDeviation)
ADVICE = (
    "Stars are probably trailing. Check balance, wind, seeing, cables and the guide calibration."
)
