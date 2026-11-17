"""Capture and Scheduler alerts (docs/design.md, "Alerts", High).

- **Capture aborted:** Capture goes ``aborted`` during a session and hasn't resumed within
  ``ABORT_GRACE`` seconds (the Scheduler may restart a job). Only when Ekos gave a reason in the
  log just before (e.g. "Autoguiding stopped. Aborting...", "Autofocus failed. Aborting
  exposure"), or guiding, focus or a device had just failed: an abort with no cause is taken as
  the user's own Stop and only recorded.
- **Scheduler aborted:** the Scheduler goes ``aborted`` after running, with its last log lines.
  (Pressing Stop leaves it ``idle``.)
- **Capture stalled:** Capture is busy (exposing, downloading, changing filter, starting a job,
  dithering) but nothing has happened for 2 × (exposure + download), and at least
  ``STALL_MIN`` seconds. The exposure comes from Ekos's "Capturing 60.000-second …" log line, the
  download time from its "Download Time: 0.10 s" line. States where Capture waits on something
  else on purpose (guiding, focus, flip, temperature) don't count. A Normal "Capture running
  again" follows when a frame arrives.
"""

from __future__ import annotations

import re
from collections import deque

from ekos_notifier.alerts import SessionRule, hhmm, make_alert, minutes
from ekos_notifier.events import Event

ABORT_GRACE = 60.0
STALL_MIN = 120.0
CAUSE_WINDOW = 5.0  # seconds before an abort in which its cause shows up
WORKING = {"capturing", "image_received", "in_progress", "changing_filter", "dithering"}
RESUMED = WORKING | {"meridian_flip", "aligning", "focusing", "filter_focus", "calibrating"}
BUSY = RESUMED | {"suspended", "guider_drift", "pause_planned", "waiting", "setting_temperature"}
EXPOSURE = re.compile(r"Capturing ([\d.]+)-second")
DOWNLOAD = re.compile(r"Download Time: ([\d.]+) s")
ROUTINE = re.compile(r"Download Time|Received image|Captured |Capturing [\d.]+-second")


class CaptureRule(SessionRule):
    def __init__(self, abort_grace: float = ABORT_GRACE, stall_min: float = STALL_MIN) -> None:
        super().__init__()
        self.abort_grace = abort_grace
        self.stall_min = stall_min
        self._log: deque[tuple[float, str, str]] = deque(maxlen=50)  # (ts, module, text)
        self._causes: deque[tuple[float, str]] = deque(maxlen=20)  # (ts, cause) not from the log
        self._capture: str | None = None
        self._since: float | None = None  # last capture progress (state change or frame)
        self._exposure = 0.0
        self._download = 0.0
        self._abort: tuple[float, str] | None = None  # pending (ts, reason)
        self._stalled_at: float | None = None
        self._scheduler_ran = False

    def on_session_closed(self) -> None:
        self._abort = None
        self._stalled_at = None

    # -- events ------------------------------------------------------------------------------

    def on_event(self, event: Event) -> list[Event]:
        kind, d = event.kind, event.data
        state = d.get("state")
        out: list[Event] = []
        if kind == "ekos.log" and d.get("text"):
            text = str(d["text"])
            self._log.append((event.ts, str(d.get("module", "")), text))
            if m := EXPOSURE.search(text):
                self._exposure = float(m.group(1))
            if m := DOWNLOAD.search(text):
                self._download = float(m.group(1))
        elif kind == "guide.state" and state in ("aborted", "calibration_error", "disconnected"):
            self._causes.append((event.ts, "guiding stopped"))
        elif kind == "focus.state" and state == "failed":
            self._causes.append((event.ts, "autofocus failed"))
        elif kind == "kstars.vanished" or (
            kind in ("ekos.state", "indi.state") and state in ("idle", "error")
        ):
            self._capture = None  # its last state is stale now; the Emergency rule takes over
            self._abort = None
        elif kind == "device.disconnected":
            self._causes.append((event.ts, "a device disconnected"))
        elif kind == "capture.frame":
            self._since = event.ts
            if d.get("exposure"):
                self._exposure = float(d["exposure"])
            out += self._unstalled(event.ts)
        elif kind == "capture.state" and not d.get("snapshot"):
            was, self._capture, self._since = self._capture, state, event.ts
            # Only an abort of a capture that was running: a failed focus exposure makes Ekos
            # report Capture "aborted" even when it was idle (seen live).
            if state == "aborted" and self.session and was in BUSY:
                self._abort = (event.ts, self._reason(event.ts))
            elif state in RESUMED:
                self._abort = None
        elif kind == "scheduler.state":
            if state == "running":
                self._scheduler_ran = True
            elif state == "aborted" and self._scheduler_ran and not d.get("snapshot"):
                self._scheduler_ran = False
                if self.session:
                    out.append(self._scheduler_alert(event.ts))
            elif state == "idle":
                self._scheduler_ran = False
        return out

    def on_tick(self, now: float) -> list[Event]:
        out: list[Event] = []
        if self._abort is not None and now - self._abort[0] >= self.abort_grace:
            (ts, reason), self._abort = self._abort, None
            if reason:
                out.append(self._abort_alert(now, ts, reason))
        if (
            self._stalled_at is None
            and self._capture in WORKING
            and self._since is not None
            and now - self._since >= self._stall_limit()
        ):
            self._stalled_at = self._since
            out.append(self._stall_alert(now))
        return out

    # -- helpers -------------------------------------------------------------------------------

    def _stall_limit(self) -> float:
        expected = self._exposure if self._capture == "capturing" else 0.0
        return max(self.stall_min, 2 * (expected + self._download))

    def _reason(self, ts: float) -> str:
        """Why Capture aborted, from what happened in the seconds before; "" if nothing."""
        recent = [
            text
            for t, module, text in self._log
            if ts - t <= CAUSE_WINDOW and module == "Capture" and "abort" in text.lower()
        ]
        if recent:
            text = re.sub(r"^\[[^\]]*\]\s*", "", recent[-1])  # drop "[CCD Simulator] "
            return re.sub(r"\s*Aborting( exposure)?\.*$", "", text, flags=re.I).strip() or text
        causes = [c for t, c in self._causes if ts - t <= CAUSE_WINDOW]
        return causes[-1] if causes else ""

    def _abort_alert(self, now: float, ts: float, reason: str) -> Event:
        body = (
            f"Capture aborted at {hhmm(ts)} and hasn't restarted.\n"
            f"Ekos: {reason.rstrip('.')}.\n"
            "The sequence is stopped: nothing is being captured."
        )
        return make_alert(now, self.session, "capture_aborted", "high", "Capture aborted", body)

    def _scheduler_alert(self, ts: float) -> Event:
        lines = [f"The Scheduler aborted at {hhmm(ts)}."]
        recent = [text for t, module, text in self._log if module == "Scheduler" and ts - t <= 60]
        if recent:
            lines.append("Scheduler: " + recent[-1])
        lines.append("No more jobs will run tonight unless you restart it.")
        return make_alert(
            ts, self.session, "scheduler_aborted", "high", "Scheduler aborted", "\n".join(lines)
        )

    def _stall_alert(self, now: float) -> Event:
        assert self._since is not None
        what = (self._capture or "busy").replace("_", " ")
        if self._capture == "capturing" and self._exposure:
            what = f"capturing a {self._exposure:g} s exposure"
        body = (
            f"No progress from Capture since {hhmm(self._since)} ({minutes(now - self._since)}): "
            f"it's still {what}.\n"
            "Check the camera, its connection and the filter wheel."
        )
        return make_alert(now, self.session, "capture_stalled", "high", "Capture stalled", body)

    def _unstalled(self, ts: float) -> list[Event]:
        if self._stalled_at is None:
            return []
        since, self._stalled_at = self._stalled_at, None
        if not self.session:
            return []
        body = f"A new frame arrived at {hhmm(ts)}, {minutes(ts - since)} after Capture stalled."
        return [
            make_alert(
                ts,
                self.session,
                "capture_running",
                "normal",
                "Capture running again",
                body,
                immediate=True,
            )
        ]
