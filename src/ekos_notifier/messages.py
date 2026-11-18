"""Which alerts are sent, and how: every alert Ekos Notifier can raise, its default, and the user's
``messages:`` settings (docs/design.md, "Alerts").

Each alert can be set to one of:

- ``off``: never sent, and left out of the digest. It's still recorded in the session.
- ``digest``: not sent on its own; listed in the next hourly digest, and a problem still counts
  in the digest's "Attention" verdict.
- ``quiet``: sent at once, silently.
- ``normal``, ``high``, ``emergency``: sent at once at that priority (an Emergency repeats on
  Pushover until acknowledged).

An all-clear (e.g. Guiding recovered) the user hasn't set follows its problem when that's ``off``
or ``digest``: an all-clear for something never sent is noise. The hourly digest itself is
``digest`` in the table only in name: set to ``quiet`` (the default), ``normal`` or more, its
"Attention" form is never below Normal; ``off`` stops it being sent.

Settings are applied as alerts are raised (``MessageSettings.apply``), so the digest, the notifier,
the GUI and the session record all see the same thing. The alert keeps ``default_priority`` when
it was changed.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from ekos_notifier.events import Event

LEVELS = ("off", "digest", "quiet", "normal", "high", "emergency")
PRIORITIES = ("quiet", "normal", "high", "emergency")


@dataclass(frozen=True)
class Alert:
    priority: str  # emergency | high | normal | quiet
    immediate: bool  # a Normal or Quiet one sent at once (not just in the digest)
    what: str  # one line for `config alerts`
    clears: str | None = None  # the problem this is the all-clear for
    off: bool = False  # off unless the user turns it on

    @property
    def default(self) -> str:
        """The default as a ``messages:`` level."""
        if self.off:
            return "off"
        if self.priority in ("emergency", "high") or self.immediate:
            return self.priority
        return "digest"


A = Alert
ALERTS: dict[str, Alert] = {
    # Emergency
    "ekos_stopped_mount_unparked": A("emergency", False, "Ekos stopped with the mount unparked"),
    "battery_critical": A("emergency", False, "Battery under the critical voltage"),
    "meridian_flip_failed": A("emergency", False, "Ekos reports a failed meridian flip"),
    "meridian_flip_overdue": A("emergency", False, "Still unflipped well past the flip time"),
    # High
    "guiding_lost": A("high", False, "Guiding stopped and hasn't recovered"),
    "phd2_warning": A("high", False, "PHD2 raised a warning or error"),
    "guiding_poor": A("high", False, "Guiding over Ekos's deviation limit"),
    "capture_aborted": A("high", False, "Capture aborted, with Ekos's reason"),
    "scheduler_aborted": A("high", False, "The Scheduler aborted"),
    "capture_stalled": A("high", False, "Capture busy but making no progress"),
    "autofocus_failed": A("high", False, "Autofocus failed twice in a row"),
    "plate_solve_failed": A("high", False, "Ekos couldn't solve a frame"),
    "dew_risk": A("high", False, "Dew margin small or humidity high"),
    "cooler_cant_hold": A("high", False, "Camera drifted above its setpoint"),
    "cooler_cant_reach": A("high", False, "Camera never reached its setpoint"),
    "battery_low": A("high", False, "Battery under the low voltage"),
    "power_output_off": A("high", False, "A power box output switched off"),
    "power_output_dead": A("high", False, "A power box output stopped drawing current"),
    "disk_low": A("high", False, "Little disk space left for frames"),
    # Normal, sent at once
    "session_started": A("normal", True, "A session started"),
    "session_summary": A("normal", True, "A session ended: frames, integration, mount"),
    "twilight_coming": A("normal", True, "Astronomical dawn is near"),
    "target_low": A("normal", True, "The target sinks below the altitude limit soon"),
    "flats_reminder": A("normal", True, "Filters with lights but no flats tonight"),
    "darks_reminder": A("normal", True, "Darks starting: cover the telescope", off=True),
    # All-clears
    "mount_unparked_cleared": A("normal", False, "The mount is parked again",
                                "ekos_stopped_mount_unparked"),
    "battery_ok": A("normal", True, "Battery voltage back up", "battery_low"),
    "meridian_flip_done": A("normal", True, "The meridian flip completed",
                            "meridian_flip_failed"),
    "guiding_recovered": A("normal", True, "Guiding is back", "guiding_lost"),
    "guiding_poor_cleared": A("normal", True, "Guiding back within limits", "guiding_poor"),
    "capture_running": A("normal", True, "Capture is making progress again", "capture_stalled"),
    "plate_solve_ok": A("normal", True, "A frame solved again", "plate_solve_failed"),
    "dew_risk_cleared": A("normal", False, "Conditions drier again", "dew_risk"),
    "cooler_ok": A("normal", True, "Camera back at its setpoint", "cooler_cant_hold"),
    "power_output_back": A("normal", True, "A power box output is back", "power_output_off"),
    "disk_ok": A("normal", True, "Disk space back over the limit", "disk_low"),
    # Quiet
    "meridian_flip_soon": A("quiet", False, "Ekos will flip soon (in the digest)"),
    "digest": A("quiet", True, "The hourly digest (its Attention form is Normal)"),
}  # fmt: skip
# All-clears that also clear a second problem.
ALSO_CLEARS = {
    "battery_ok": "battery_critical",
    "meridian_flip_done": "meridian_flip_overdue",
    "cooler_ok": "cooler_cant_reach",
    "power_output_back": "power_output_dead",
}


class SettingsError(ValueError):
    """Worded for the user."""


@dataclass
class MessageSettings:
    levels: dict[str, str] = field(default_factory=dict)  # alert -> level, as the user set them

    @classmethod
    def parse(cls, raw: Mapping[str, object] | None) -> MessageSettings:
        levels = {}
        for name, value in (raw or {}).items():
            name = str(name)
            if name not in ALERTS:
                close = difflib.get_close_matches(name, ALERTS, n=1)
                hint = f" (did you mean '{close[0]}'?)" if close else ""
                raise SettingsError(
                    f"unknown alert '{name}'{hint}; `ekos-notifier config alerts` lists them"
                )
            level = "off" if value is False else str(value).lower()
            if level not in LEVELS:
                raise SettingsError(f"'{name}' must be one of {' | '.join(LEVELS)}, not {value!r}")
            if name == "digest" and level == "digest":
                raise SettingsError("'digest' can be off, quiet, normal, high or emergency")
            levels[name] = level
        return cls(levels)

    def level(self, name: str) -> str:
        """The level an alert goes out at: the user's, an all-clear following its problem, or
        the default."""
        if name in self.levels:
            return self.levels[name]
        alert = ALERTS.get(name)
        if alert is None:
            return "normal"
        for problem in (alert.clears, ALSO_CLEARS.get(name)):
            if problem and self.levels.get(problem) in ("off", "digest"):
                return self.levels[problem]
        return alert.default

    def apply(self, events: Iterable[Event]) -> list[Event]:
        return [self._apply(e) if e.kind == "alert" else e for e in events]

    def _apply(self, event: Event) -> Event:
        d = event.data
        name = d.get("rule", "")
        alert = ALERTS.get(name)
        level = self.level(name)
        if alert is None or (level == alert.default and not alert.off):
            return event  # as the rule raised it
        data = {**d, "default_priority": d.get("priority")}
        if level == "off":
            data.update(off=True, immediate=False)
        elif level == "digest":
            held = "quiet" if d.get("priority") == "quiet" else "normal"
            data.update(priority=held, immediate=False)
        else:
            priority = level
            if name == "digest" and d.get("priority") == "normal":  # "Attention": never below
                priority = max(level, "normal", key=PRIORITIES.index)
            data.update(priority=priority, immediate=True)
        return Event(event.ts, event.kind, data, event.source, event.session_id)

    def lowered_emergencies(self) -> list[str]:
        return [
            name
            for name, alert in ALERTS.items()
            if alert.priority == "emergency" and self.level(name) != "emergency"
        ]
