"""Calibration reminders (docs/design.md, "Alerts", Normal — manual steps).

- **Flats reminder** (Normal, sent with the session summary): when a session that imaged ends,
  the filters that have lights tonight but no flats tonight. Flats taken by the sequence itself, or
  in an earlier session of the same night, count; each filter is reminded once per night. Not when
  the user stopped Ekos (they're at the rig), the same as the summary.
- **Darks reminder** (off by default; turn it on in ``messages:``): when a dark job starts (the
  Ekos adapter reads the active job's frame type as Capture starts, ``capture.job``), a reminder
  to cover the telescope. Once per night, session or not: darks don't start a session.

Nights are the session manager's (``night_of``: 02:00 belongs to the evening before).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from ekos_notifier.alerts import SessionRule, make_alert
from ekos_notifier.events import Event
from ekos_notifier.i18n import _
from ekos_notifier.session import night_of

QUIET_ENDS = {"ekos_stopped"}  # as for the summary: you stopped Ekos yourself


class CalibrationRule(SessionRule):
    def __init__(self) -> None:
        super().__init__()
        self._lights: dict[str, set[str]] = defaultdict(set)  # night -> filters with lights
        self._flats: dict[str, set[str]] = defaultdict(set)  # night -> filters with flats
        self._reminded: dict[str, set[str]] = defaultdict(set)  # night -> filters reminded
        self._darks_reminded: set[str] = set()  # nights

    def resume(self, session_id: str | None, alerts: Iterable[Event] = ()) -> None:
        super().resume(session_id, alerts)
        for a in alerts:
            if a.data.get("rule") == "flats_reminder":
                self._reminded[night_of(a.ts)].update(a.data.get("filters", []))
            elif a.data.get("rule") == "darks_reminder":
                self._darks_reminded.add(night_of(a.ts))

    def tick(self, now: float) -> list[Event]:
        return []

    def on_event(self, event: Event) -> list[Event]:
        kind, d = event.kind, event.data
        night = night_of(event.ts)
        if kind == "capture.frame" and d.get("type") in ("light", "flat"):
            table = self._lights if d["type"] == "light" else self._flats
            table[night].add(d.get("filter") or "no filter")
        elif kind == "session.closed" and d.get("state") == "closed":
            if d.get("reason") not in QUIET_ENDS:
                night = d.get("night") or night
                missing = self._lights[night] - self._flats[night] - self._reminded[night]
                if missing:
                    self._reminded[night] |= missing
                    return [self._flats_reminder(event, sorted(missing))]
        elif kind == "capture.job" and d.get("frame_type") == "dark":
            if night not in self._darks_reminded:
                self._darks_reminded.add(night)
                return [
                    make_alert(
                        event.ts,
                        event.session_id,
                        "darks_reminder",
                        "normal",
                        _("Darks starting"),
                        _("Ekos is starting dark frames.")
                        + "\n"
                        + _("Cover the telescope (or close the dust cap) if it isn't already."),
                        immediate=True,
                    )
                ]
        return []

    @staticmethod
    def _flats_reminder(event: Event, filters: list[str]) -> Event:
        which = ", ".join(filters)
        body = (
            _("No flats yet tonight for {filters}.").format(filters=which)
            + "\n"
            + _(
                "Take them before the focus, rotation or camera changes, or tonight's lights"
                " won't have matching flats."
            )
        )
        return make_alert(
            event.ts,
            event.session_id,
            "flats_reminder",
            "normal",
            _("Flats reminder"),
            body,
            immediate=True,
            filters=filters,
        )
