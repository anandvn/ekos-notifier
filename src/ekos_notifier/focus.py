"""The "Autofocus failed" alert (High, docs/design.md "Alerts").

Sent when ``failures`` autofocus runs in a row fail during a session (default 2), once per
streak; a run that completes resets the count.

What a failure looks like (KStars 3.8.5, seen live 2026-11-17): Ekos logs "Autofocus failed,
moving back to initial focus position …", retries by itself ("Restarting autofocus process…",
then a new ``in_progress``), and when it gives up reports ``aborted`` — not ``failed``. An earlier
run reported ``aborted`` for each attempt and ``failed`` at the end. So:

- a run is everything from ``in_progress`` until it ends ``complete``, ``failed`` or ``aborted``;
  a failed ending is counted ``RESTART_WINDOW`` seconds later, unless Ekos's "Restarting
  autofocus process" comes first, in which case the run is still going;
- it failed if it ended ``failed``, or ended ``aborted`` with "Autofocus failed" logged during
  it. An ``aborted`` with no failure logged is the user's own Abort and counts for nothing.
"""

from __future__ import annotations

from ekos_notifier.alerts import SessionRule, hhmm, make_alert
from ekos_notifier.events import Event

FAILURES = 2
RUNNING = {"in_progress", "framing", "changing_filter", "waiting"}
RESTART_WINDOW = 10.0  # seconds after a run ends in which Ekos's own restart continues it


class AutofocusRule(SessionRule):
    def __init__(self, failures: int = FAILURES) -> None:
        super().__init__()
        self.failures = failures
        self._streak = 0
        self._in_run = False
        self._run_failed = False  # "Autofocus failed" logged in this run
        self._failed_at: float | None = None  # a failed ending, counted once no restart follows
        self._alerted = False
        self._hfr: float | None = None

    def on_event(self, event: Event) -> list[Event]:
        kind, d = event.kind, event.data
        if kind == "focus.hfr" and isinstance(d.get("hfr"), (int, float)) and d["hfr"] > 0:
            self._hfr = float(d["hfr"])
        elif kind == "ekos.log" and d.get("module") == "Focus":
            text = str(d.get("text", ""))
            if "Autofocus failed" in text:
                self._run_failed = True
            elif "Restarting autofocus process" in text and self._failed_at is not None:
                # Ekos is retrying: that ending was one attempt of a run still going.
                self._failed_at = None
                self._in_run, self._run_failed = True, True
        if kind != "focus.state" or d.get("snapshot"):
            return []
        state = d.get("state")
        if state in RUNNING:
            if not self._in_run:
                self._in_run, self._run_failed = True, False
            return []
        if not self._in_run:
            return []
        if state == "complete":
            self._in_run = False
            self._streak, self._alerted, self._failed_at = 0, False, None
        elif state == "failed" or (state == "aborted" and self._run_failed):
            self._in_run = False
            self._failed_at = event.ts
        elif state == "aborted":
            self._in_run = False  # the user's Abort: neither a failure nor a success
        return []

    def on_tick(self, now: float) -> list[Event]:
        if self._failed_at is None or now - self._failed_at < RESTART_WINDOW:
            return []
        failed_at, self._failed_at = self._failed_at, None
        self._streak += 1
        if self._streak >= self.failures and not self._alerted:
            self._alerted = True
            return [self._alert(failed_at, now)]
        return []

    def _alert(self, failed_at: float, now: float) -> Event:
        lines = [f"Autofocus has failed {self._streak} times in a row (last at {hhmm(failed_at)})."]
        if self._hfr is not None:
            lines.append(f"Last HFR measured: {self._hfr:.2f}.")
        lines.append("Stars may be too faint (clouds, dew) or too far out of focus to measure.")
        return make_alert(
            now, self.session, "autofocus_failed", "high", "Autofocus failed", "\n".join(lines)
        )
