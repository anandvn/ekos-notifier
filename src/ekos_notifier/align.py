"""The "Plate solve failed" alert (High, docs/design.md "Alerts").

During a session, an Ekos Align run that fails, with no successful solve within ``RETRY_GRACE``
seconds (Ekos retries a solve by itself with looser settings, and the Scheduler retries a job's
alignment stage), sends **Plate solve failed**: what came just before (a meridian flip, or the
Scheduler moving to a new target), how many runs have failed, and Ekos's last reason ("Solver
timed out after 60 seconds."). Once per streak; **Plate solve succeeded** (Normal, immediate)
follows the next successful solve.

A run is everything from ``in_progress`` until it ends ``complete``/``successful``, ``failed`` or
``aborted``. It failed if it ended ``failed``, or ``aborted`` with a solver failure logged during
it (KStars 3.8.5's autofocus ends a failed run ``aborted``, seen live, so Align is treated the same
way until a failed solve is recorded). An ``aborted`` with no failure logged is the user's own
Abort and counts for nothing. Failure lines are Align's own (from KStars 3.8.5's strings): "Solver
Failed.", "Solver failed after %1 seconds.", "Solver timed out after %1 seconds.", "Maximum number
of iterations reached. Solver failed.", "Astrometry alignment failed". Its retry lines ("Solver
failed. Retrying without scale constraint.", "Plate solving failed, extending criteria and
retrying...") mean the run continues.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from ekos_notifier.alerts import SessionRule, hhmm, make_alert
from ekos_notifier.events import Event
from ekos_notifier.i18n import _, ngettext

RETRY_GRACE = 180.0
CONTEXT_WINDOW = 900.0  # a flip or target change this soon before the run explains it
RUNNING = {"in_progress", "syncing", "slewing", "rotating"}
SOLVED = {"complete", "successful"}
FAILURE = re.compile(
    r"solver failed|solver timed out|solver aborted|alignment failed|failed plate solving", re.I
)
RETRYING = re.compile(r"retry", re.I)
JOB = re.compile(r"Job '(.+?)' ")


class PlateSolveRule(SessionRule):
    def __init__(self, retry_grace: float = RETRY_GRACE) -> None:
        super().__init__()
        self.retry_grace = retry_grace
        self._in_run = False
        self._run_failed = False  # a final solver failure logged in this run
        self._reason: str | None = None
        self._failed_at: float | None = None  # first failed run of the streak, not yet sent
        self._streak = 0
        self._alerted = False
        self._flip_at: float | None = None
        self._target: tuple[float, str] | None = None  # when the Scheduler moved on, to what

    def resume(self, session_id: str | None, alerts: Iterable[Event] = ()) -> None:
        super().resume(session_id, alerts)
        for a in alerts:
            if a.data.get("rule") == "plate_solve_failed":
                self._alerted = True
            elif a.data.get("rule") == "plate_solve_ok":
                self._alerted = False

    def on_session_closed(self) -> None:
        self._in_run = self._run_failed = self._alerted = False
        self._failed_at, self._streak = None, 0

    def on_event(self, event: Event) -> list[Event]:
        kind, d, ts = event.kind, event.data, event.ts
        if kind == "capture.state" and d.get("state") == "meridian_flip":
            self._flip_at = ts
        elif kind == "capture.flip_started":
            self._flip_at = ts
        elif kind == "ekos.log":
            text = str(d.get("text", ""))
            if d.get("module") == "Align" and FAILURE.search(text) and not RETRYING.search(text):
                self._run_failed = True
                self._reason = text.strip()
            elif d.get("module") == "Scheduler" and (m := JOB.search(text)):
                if self._target is None or self._target[1] != m.group(1):
                    self._target = (ts, m.group(1))
        if kind != "align.state" or d.get("snapshot"):
            return []
        state = d.get("state")
        if state in RUNNING:
            if not self._in_run:
                self._in_run, self._run_failed = True, False
            return []
        if state in SOLVED:
            self._in_run = False
            self._failed_at, self._streak = None, 0
            if self._alerted:
                self._alerted = False
                return [
                    make_alert(
                        ts,
                        self.session,
                        "plate_solve_ok",
                        "normal",
                        _("Plate solve succeeded"),
                        _("Ekos solved the frame at {time}.").format(time=hhmm(ts)),
                        immediate=True,
                    )
                ]
            return []
        if self._in_run and (state == "failed" or (state == "aborted" and self._run_failed)):
            self._in_run = False
            self._streak += 1
            if self._failed_at is None:
                self._failed_at = ts
        elif state in ("failed", "aborted"):
            self._in_run = False
        return []

    def on_tick(self, now: float) -> list[Event]:
        if self._alerted or self._failed_at is None or now - self._failed_at < self.retry_grace:
            return []
        self._alerted = True
        failed_at, self._failed_at = self._failed_at, None
        return [self._alert(now, failed_at)]

    def _alert(self, now: float, failed_at: float) -> Event:
        time = hhmm(failed_at)
        if self._streak > 1:
            runs = ngettext(
                "{n} run failed since {time}", "{n} runs failed since {time}", self._streak
            ).format(n=self._streak, time=time)
        else:
            runs = _("a run failed since {time}").format(time=time)
        if self._flip_at is not None and 0 <= failed_at - self._flip_at <= CONTEXT_WINDOW:
            first = _("Ekos couldn't solve the frame after the meridian flip ({runs}).").format(
                runs=runs
            )
        elif self._target is not None and 0 <= failed_at - self._target[0] <= CONTEXT_WINDOW:
            first = _("Ekos couldn't solve the frame after slewing to {target} ({runs}).").format(
                target=self._target[1], runs=runs
            )
        else:
            first = _("Ekos couldn't solve the frame ({runs}).").format(runs=runs)
        lines = [first]
        if self._reason:
            lines.append(_("Ekos: “{line}”").format(line=self._reason))
        lines.append(
            _(
                "Without a solve the target may be off-centre. Check for clouds, dew or focus,"
                " and the solver settings."
            )
        )
        return make_alert(
            now,
            self.session,
            "plate_solve_failed",
            "high",
            _("Plate solve failed"),
            "\n".join(lines),
        )
