"""The "Autofocus failed" alert (High, docs/design.md "Alerts").

Sent when ``failures`` autofocus runs in a row end in failure during a session (default 2).
Ekos retries a failed autofocus by itself ("Restarting autofocus process…"), reporting each
attempt as ``aborted``; only the last attempt of the loop reports ``failed``, sometimes right
after ``aborted`` (docs/open-questions.md). So a run counts once, when ``failed`` arrives; a
``failed`` repeated without a new start in between is the same run. A ``complete`` run resets the
count, and the alert is sent once per streak.
"""

from __future__ import annotations

from ekos_notifier.alerts import SessionRule, hhmm, make_alert
from ekos_notifier.events import Event

FAILURES = 2
RUNNING = {"in_progress", "framing", "changing_filter", "waiting"}


class AutofocusRule(SessionRule):
    def __init__(self, failures: int = FAILURES) -> None:
        super().__init__()
        self.failures = failures
        self._streak = 0
        self._run_counted = False  # this run's failure is already counted
        self._alerted = False
        self._hfr: float | None = None

    def on_event(self, event: Event) -> list[Event]:
        kind, d = event.kind, event.data
        if kind == "focus.hfr" and isinstance(d.get("hfr"), (int, float)) and d["hfr"] > 0:
            self._hfr = float(d["hfr"])
        if kind != "focus.state" or d.get("snapshot"):
            return []
        state = d.get("state")
        if state in RUNNING:
            self._run_counted = False
        elif state == "complete":
            self._streak, self._alerted, self._run_counted = 0, False, False
        elif state == "failed" and not self._run_counted:
            self._run_counted = True
            self._streak += 1
            if self._streak >= self.failures and not self._alerted and self.session:
                self._alerted = True
                return [self._alert(event.ts)]
        return []

    def _alert(self, ts: float) -> Event:
        lines = [f"Autofocus has failed {self._streak} times in a row (last at {hhmm(ts)})."]
        if self._hfr is not None:
            lines.append(f"Last HFR measured: {self._hfr:.2f}.")
        lines.append("Stars may be too faint (clouds, dew) or too far out of focus to measure.")
        return make_alert(
            ts, self.session, "autofocus_failed", "high", "Autofocus failed", "\n".join(lines)
        )
