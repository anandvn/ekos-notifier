"""The "Weather alert" (High) and its all-clear "Weather OK" (docs/design.md, "Alerts").

From INDI's standard weather interface (``weather.status`` events, see ``indi.decode``): every
weather driver has lights for its critical readings (rain, wind, clouds, forecast…), each Ok,
Busy (near its limit) or Alert (past it), against limits set in the driver. That works for a
cloud and rain sensor at the rig (an AAG CloudWatcher) and for OpenWeatherMap's regional
weather alike, which is why it's built on the interface rather than on one driver (issue #54).
Ekos's Scheduler can park the mount when a weather device goes to Alert; this says why.

- **Weather alert:** a reading goes to Alert. Sent at any time, session or not, like the battery
  alerts: rain while setting up or waiting for dark matters too. It names each reading past
  its limit with its value, and says when the source is regional (``sources``).
- **Weather OK:** once nothing has been in Alert for ``clear_after`` seconds (10 min), so a
  reading flickering at its limit doesn't send a stream of alerts.
- **Per reading, a weather station at the rig wins** over a secondary source (OpenWeatherMap):
  a secondary source's light counts only for a reading no direct device watches. Seen in a
  real dump (#54): a Pegasus PPBM's weather status watches only temperature, so OpenWeatherMap's
  rain still counts beside it, while its temperature doesn't. Readings match by their INDI
  name (``WEATHER_RAIN_HOUR``); drivers that name the same reading differently both count.
- Pre-flight: the status at the session's start, as a warning when a reading is in Alert or
  near its limit.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ekos_notifier import sources
from ekos_notifier.alerts import SessionRule, make_alert, minutes
from ekos_notifier.events import Event
from ekos_notifier.i18n import _

CLEAR_AFTER = 600.0
# Seconds to wait after a reading goes to Alert before sending: drivers update their lights
# before their readings, so this lets the same cycle's values arrive (seen on the simulator).
SETTLE = 5.0
RANK = {"idle": 0, "ok": 1, "warning": 2, "alert": 3}


class WeatherRule(SessionRule):
    SETTINGS = ("clear_after",)

    def __init__(self, clear_after: float = CLEAR_AFTER) -> None:
        super().__init__()
        self.clear_after = clear_after
        self._status: dict[str, dict[str, Any]] = {}  # device -> latest weather.status data
        self._secondary: set[str] = set()
        self._raised = False
        self._calm_since: float | None = None  # nothing in Alert since, while raised
        self._alert_since: float | None = None  # in Alert since, not yet sent

    # Weather doesn't wait for a session, like the battery.
    def tick(self, now: float) -> list[Event]:
        return self.on_tick(now)

    def resume(self, session_id: str | None, alerts: Iterable[Event] = ()) -> None:
        super().resume(session_id, alerts)
        for a in alerts:
            if a.data.get("rule") == "weather_alert":
                self._raised = True
            elif a.data.get("rule") == "weather_ok":
                self._raised = False

    def on_session_closed(self) -> None:
        pass  # the weather goes on

    # -- events ------------------------------------------------------------------------------

    def on_event(self, event: Event) -> list[Event]:
        kind, d = event.kind, event.data
        if kind == "weather.status" and d.get("device"):
            sources.note(self._secondary, d)
            self._status[d["device"]] = d
            return self._check(event.ts)
        if kind == "indi.capabilities":
            keep = set(d.get("metrics", {}).get("weather.status", []))
            self._status = {k: v for k, v in self._status.items() if k in keep}
            return self._check(event.ts)
        return []

    def on_tick(self, now: float) -> list[Event]:
        since = self._alert_since
        if since is not None and now - since >= SETTLE:
            self._alert_since = None
            self._raised = True
            return [self._alert(now, self._counted())]
        calm = self._calm_since
        if not self._raised or calm is None or now - calm < self.clear_after:
            return []
        self._raised = False
        self._calm_since = None
        body = _("The weather has been within its limits for {duration}.").format(
            duration=minutes(self.clear_after)
        )
        return [
            make_alert(
                now, self.session, "weather_ok", "normal", _("Weather OK"), body, immediate=True
            )
        ]

    def preflight(self, now: float) -> list[tuple[bool, str]]:
        counted = self._counted()
        state = self._state(counted)
        if state == "idle":
            return []
        if state in ("alert", "warning"):
            text = "; ".join(
                _("{device}: {readings}").format(
                    device=device, readings=self._readings(device, names)
                )
                for device, names in self._by_state(counted, state).items()
            )
            if state == "alert":
                return [(False, _("weather alert from {readings}").format(readings=text))]
            return [(False, _("weather near its limits ({readings})").format(readings=text))]
        devices = ", ".join(sorted(d for d, lights in counted.items() if lights))
        return [(True, _("weather OK ({device})").format(device=devices))]

    # -- helpers -------------------------------------------------------------------------------

    def _counted(self) -> dict[str, dict[str, str]]:
        """device -> {reading: state} for the lights that count: every direct device's, and a
        secondary source's for the readings no direct device watches."""
        direct = {
            name
            for device, status in self._status.items()
            if device not in self._secondary
            for name in status.get("lights") or {}
        }
        out = {}
        for device, status in self._status.items():
            lights = {
                n: (v or {}).get("state", "idle") for n, v in (status.get("lights") or {}).items()
            }
            if device in self._secondary:
                lights = {n: s for n, s in lights.items() if n not in direct}
            out[device] = lights
        return out

    @staticmethod
    def _state(counted: dict[str, dict[str, str]]) -> str:
        states = [s for lights in counted.values() for s in lights.values()]
        return max(states, key=RANK.get, default="idle")

    @staticmethod
    def _by_state(counted: dict[str, dict[str, str]], state: str) -> dict[str, list[str]]:
        out = {}
        for device in sorted(counted):
            names = sorted(n for n, s in counted[device].items() if s == state)
            if names:
                out[device] = names
        return out

    def _readings(self, device: str, names: Iterable[str]) -> str:
        """ "Rain precip (mm) 1.2, Wind (m/s) 14": the driver's labels, with their values."""
        status = self._status.get(device, {})
        lights = status.get("lights") or {}
        values = status.get("values") or {}
        parts = []
        for name in names:
            label = (lights.get(name) or {}).get("label") or name
            value = values.get(label)
            parts.append(f"{label} {value:g}" if isinstance(value, (int, float)) else label)
        return ", ".join(parts)

    def _check(self, ts: float) -> list[Event]:
        counted = self._counted()
        if self._state(counted) == "alert":
            self._calm_since = None
            if not self._raised and self._alert_since is None:
                self._alert_since = ts  # sent once SETTLE has passed (on_tick)
        else:
            self._alert_since = None  # a blip shorter than SETTLE sends nothing
            if self._raised and self._calm_since is None:
                self._calm_since = ts
        return []

    def _alert(self, now: float, counted: dict[str, dict[str, str]]) -> Event:
        alerting = self._by_state(counted, "alert")
        lines = [
            _("{device} reports, past its limits: {readings}.").format(
                device=device, readings=self._readings(device, names)
            )
            for device, names in alerting.items()
        ]
        if alerting and all(d in self._secondary for d in alerting):
            lines.append(_("That's regional weather from the internet, not measured at the rig."))
        lines.append(
            _(
                "If Ekos is set to park on bad weather, it will now. Check the sky, and cover the"
                " rig if rain is coming."
            )
        )
        return make_alert(
            now, self.session, "weather_alert", "high", _("Weather alert"), "\n".join(lines)
        )
