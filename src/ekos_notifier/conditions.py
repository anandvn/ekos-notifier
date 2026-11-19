"""Dew and camera-cooler alerts (docs/design.md, "Alerts", High).

Inputs are canonical metrics from INDI (``env.temperature``, ``env.dewpoint`` (measured or
derived), ``env.humidity``, ``camera.temperature``), ``power.output`` events for dew heaters,
``camera.cooler`` states and Ekos's log. A rig without the sensors never gets these alerts.

- **Dew risk:** the dew margin (ambient − dew point) under ``alerts.dew_margin_c`` (default 2 °C)
  or humidity over ``HUMIDITY`` %, for ``DEW_SUSTAIN`` seconds during a session. With several
  sensors, the smallest margin and highest humidity count. The message names dew heaters that are
  off or at 0 % (power boxes on INDI's standard power interface). Once; **Dew risk passed**
  (Normal, for the digest) after the margin has been 1 °C over the limit and humidity 5 % under
  it for ``DEW_CLEAR`` seconds, which re-arms it.
- **Cooler can't hold:** the imaging camera reached its setpoint, then has stayed more than
  ``alerts.cooler_tolerance_c`` (default 2 °C) above it for ``HOLD_AFTER`` seconds.
- **Cooler can't reach:** still more than the tolerance above the setpoint ``REACH_WITHIN``
  seconds after it was set.
- Both once per camera per session; **Cooler at setpoint** (Normal, immediate) when it's back.

The setpoint: INDI never sends it to other clients, so it comes from Ekos Capture's log ("Setting
temperature to -10.00 °C...", when a job enforces a temperature), for Capture's camera. Without
one, the temperature at the moment the driver reports reaching its target (``CCD_TEMPERATURE``
going Busy → Ok) is taken as the setpoint; the driver going Busy again then means a new target
until it's reached, and Idle means regulation is off.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from ekos_notifier.alerts import SessionRule, hhmm, make_alert, minutes
from ekos_notifier.ekos.translation import NUMBER, localized
from ekos_notifier.events import Event

HUMIDITY = 90.0
DEW_SUSTAIN = 300.0
DEW_CLEAR = 600.0
HOLD_AFTER = 600.0
REACH_WITHIN = 1200.0
SETPOINT = re.compile(rf"Setting temperature to ({NUMBER})")
ENV = {"env.temperature": "temp", "env.dewpoint": "dew", "env.humidity": "rh"}


def _c(value: float) -> str:
    return f"{value:.1f} °C".replace("-", "−")


class DewRule(SessionRule):
    SETTINGS = ("margin", "humidity")

    def __init__(
        self,
        margin: float = 2.0,
        humidity: float = HUMIDITY,
        *,
        sustain: float = DEW_SUSTAIN,
        clear_after: float = DEW_CLEAR,
    ) -> None:
        super().__init__()
        self.margin = margin
        self.humidity = humidity
        self.sustain = sustain
        self.clear_after = clear_after
        self._env: dict[str, dict[str, float]] = {"temp": {}, "dew": {}, "rh": {}}
        self._heaters: dict[tuple[str, str], tuple[str, bool | None, float | None]] = {}
        self._risk_since: float | None = None
        self._clear_since: float | None = None
        self._raised = False

    def resume(self, session_id: str | None, alerts: Iterable[Event] = ()) -> None:
        super().resume(session_id, alerts)
        for a in alerts:
            if a.data.get("rule") == "dew_risk":
                self._raised = True
            elif a.data.get("rule") == "dew_risk_cleared":
                self._raised = False

    def on_session_closed(self) -> None:
        self._raised = False
        self._risk_since = self._clear_since = None

    def on_event(self, event: Event) -> list[Event]:
        kind, d = event.kind, event.data
        if kind == "metric" and d.get("metric") in ENV and isinstance(d.get("value"), (int, float)):
            self._env[ENV[d["metric"]]][d.get("device") or ""] = float(d["value"])
            self._update(event.ts)
        elif kind == "indi.capabilities":
            metrics = d.get("metrics", {})
            for metric, key in ENV.items():
                keep = set(metrics.get(metric, []))
                self._env[key] = {k: v for k, v in self._env[key].items() if k in keep}
            outputs = set(metrics.get("power.outputs", []))
            self._heaters = {k: v for k, v in self._heaters.items() if k[0] in outputs}
            self._update(event.ts)
        elif kind == "power.output" and d.get("kind") == "dew":
            key = (d.get("device") or "", d.get("output") or "")
            self._heaters[key] = (d.get("label") or key[1], d.get("on"), d.get("duty"))
        return []

    def on_tick(self, now: float) -> list[Event]:
        if self.margin <= 0:
            return []
        if not self._raised and self._risk_since is not None:
            if now - self._risk_since >= self.sustain:
                self._raised = True
                self._clear_since = None
                return [self._alert(now)]
        if self._raised and self._clear_since is not None:
            if now - self._clear_since >= self.clear_after:
                self._raised = False
                self._clear_since = None
                body = "Conditions are drier again: " + self._reading()
                return [
                    make_alert(
                        now, self.session, "dew_risk_cleared", "normal", "Dew risk passed", body
                    )
                ]
        return []

    def preflight(self, now: float) -> list[tuple[bool, str]]:
        if self.margin <= 0:
            return []
        _, margin, rh = self._worst()
        out = []
        if margin is not None:
            text = f"dew margin {_c(margin)}"
            out.append(
                (
                    margin >= self.margin,
                    text + (f", under {self.margin:g} °C" if margin < self.margin else ""),
                )
            )
        if rh is not None:
            out.append(
                (
                    rh <= self.humidity,
                    f"RH {rh:.0f}%" + (f", over {self.humidity:g}%" if rh > self.humidity else ""),
                )
            )
        return out

    # -- helpers -------------------------------------------------------------------------------

    def _worst(self) -> tuple[str | None, float | None, float | None]:
        """(device, margin) with the smallest dew margin, and the highest humidity."""
        temp, dew, rh = self._env["temp"], self._env["dew"], self._env["rh"]
        margins = {dev: t - dew[dev] for dev, t in temp.items() if dev in dew}
        device = min(margins, key=margins.__getitem__) if margins else None
        return device, margins.get(device) if device else None, max(rh.values(), default=None)

    def _update(self, ts: float) -> None:
        _, margin, rh = self._worst()
        risky = (margin is not None and margin < self.margin) or (
            rh is not None and rh > self.humidity
        )
        dry = (margin is None or margin >= self.margin + 1.0) and (
            rh is None or rh <= self.humidity - 5.0
        )
        if margin is None and rh is None:
            risky = dry = False
        if risky:
            self._clear_since = None
            if self._risk_since is None:
                self._risk_since = ts
        else:
            self._risk_since = None
            if dry and self._clear_since is None:
                self._clear_since = ts
            elif not dry:
                self._clear_since = None

    def _reading(self) -> str:
        device, margin, rh = self._worst()
        parts = []
        if device is not None and margin is not None:
            parts.append(
                f"dew margin {_c(margin)} (ambient {_c(self._env['temp'][device])}, "
                f"dew point {_c(self._env['dew'][device])})"
            )
        if rh is not None:
            parts.append(f"RH {rh:.0f}%")
        return ", ".join(parts) + "."

    def _alert(self, now: float) -> Event:
        reading = self._reading()
        lines = [reading[0].upper() + reading[1:]]
        if self._heaters:
            state = []
            idle = []
            for label, on, duty in sorted(self._heaters.values(), key=lambda h: h[0]):
                if on is False or duty == 0:
                    state.append(f"{label} off" if on is False else f"{label} 0%")
                    idle.append(f"“{label}”")
                else:
                    state.append(f"{label} {duty:.0f}%" if duty is not None else f"{label} on")
            lines.append("Dew heaters: " + ", ".join(state) + ".")
            if idle:
                lines.append(f"Turn on {', '.join(idle)}, or check the optics for dew.")
            else:
                lines.append("Check the optics for dew.")
        else:
            lines.append("Check the optics for dew, and the dew heaters.")
        return make_alert(now, self.session, "dew_risk", "high", "Dew risk", "\n".join(lines))


class CoolerRule(SessionRule):
    SETTINGS = ("tolerance",)

    def __init__(
        self,
        tolerance: float = 2.0,
        *,
        hold_after: float = HOLD_AFTER,
        reach_within: float = REACH_WITHIN,
    ) -> None:
        super().__init__()
        self.tolerance = tolerance
        self.hold_after = hold_after
        self.reach_within = reach_within
        self._temp: dict[str, float] = {}  # camera -> °C
        self._setpoint: dict[str, dict[str, Any]] = {}  # camera -> value, since, source, reached
        self._ekos: tuple[float, float] | None = None  # Ekos's setpoint, not yet given a camera
        self._camera: str | None = None  # Ekos Capture's camera
        self._over_since: dict[str, float] = {}
        self._raised: dict[str, str] = {}
        self._ambient: float | None = None

    def resume(self, session_id: str | None, alerts: Iterable[Event] = ()) -> None:
        super().resume(session_id, alerts)
        for a in alerts:
            rule, device = a.data.get("rule"), a.data.get("device")
            if rule in ("cooler_cant_hold", "cooler_cant_reach") and device:
                self._raised[device] = rule
            elif rule == "cooler_ok" and device:
                self._raised.pop(device, None)

    def on_session_closed(self) -> None:
        self._raised.clear()
        self._over_since.clear()

    def on_event(self, event: Event) -> list[Event]:
        kind, d, ts = event.kind, event.data, event.ts
        if kind == "metric" and isinstance(d.get("value"), (int, float)):
            if d.get("metric") == "camera.temperature":
                return self._temperature(ts, d.get("device") or "", float(d["value"]))
            if d.get("metric") == "env.temperature":
                self._ambient = float(d["value"])
        elif kind == "capture.camera" and d.get("camera"):
            self._camera = d["camera"]
            self._assign()
        elif kind == "ekos.log" and d.get("module") == "Capture":
            m = SETPOINT.search(str(d.get("text", "")))
            if m and (value := localized(m.group(1), d)) is not None:
                self._ekos = (value, ts)
                self._assign()
        elif kind == "camera.cooler":
            self._driver(ts, d.get("device") or "", d.get("state"), d.get("temperature"))
        elif kind == "indi.capabilities":
            cameras = set(d.get("metrics", {}).get("camera.temperature", []))
            for table in (self._temp, self._setpoint, self._over_since):
                for dev in [k for k in table if k not in cameras]:
                    del table[dev]
        return []

    def on_tick(self, now: float) -> list[Event]:
        if self.tolerance <= 0:
            return []
        out = []
        for dev, sp in self._setpoint.items():
            if dev in self._raised or dev not in self._temp:
                continue
            temp = self._temp[dev]
            over = self._over_since.get(dev)
            if sp["reached"] and over is not None and now - over >= self.hold_after:
                self._raised[dev] = "cooler_cant_hold"
                out.append(self._alert(now, dev, "cooler_cant_hold", over))
            elif (
                not sp["reached"]
                and now - sp["since"] >= self.reach_within
                and temp > sp["value"] + self.tolerance
            ):
                self._raised[dev] = "cooler_cant_reach"
                out.append(self._alert(now, dev, "cooler_cant_reach", sp["since"]))
        return out

    def preflight(self, now: float) -> list[tuple[bool, str]]:
        """The imaging camera's temperature against its setpoint. Still cooling is a reading,
        not a warning: Ekos waits for the setpoint, and "Cooler can't reach" covers a cooler that
        never gets there."""
        cameras = list(self._temp)
        camera = self._camera if self._camera in self._temp else None
        if camera is None and len(cameras) == 1:
            camera = cameras[0]
        if camera is None:
            return []
        temp, sp = self._temp[camera], self._setpoint.get(camera)
        if sp is None:
            return [(True, f"camera {_c(temp)}")]
        if abs(temp - sp["value"]) <= self.tolerance:
            return [(True, f"camera {_c(temp)} (at setpoint)")]
        if sp["reached"] and temp > sp["value"] + self.tolerance:
            return [(False, f"camera {_c(temp)}, over its {_c(sp['value'])} setpoint")]
        return [(True, f"camera cooling, {_c(temp)} → {_c(sp['value'])}")]

    # -- helpers -------------------------------------------------------------------------------

    def _assign(self) -> None:
        """Ekos's setpoint goes to Capture's camera, or the only camera there is."""
        if self._ekos is None:
            return
        cameras = list(self._temp)
        camera = self._camera if self._camera else (cameras[0] if len(cameras) == 1 else None)
        if camera is None:
            return
        value, since = self._ekos
        self._ekos = None
        self._setpoint[camera] = {
            "value": value,
            "since": since,
            "source": "ekos",
            "reached": False,
        }
        self._over_since.pop(camera, None)
        if camera in self._temp:
            self._check(since, camera)

    def _driver(self, ts: float, dev: str, state: Any, temperature: Any) -> None:
        sp = self._setpoint.get(dev)
        if state == "idle":
            self._setpoint.pop(dev, None)  # regulation off
            self._over_since.pop(dev, None)
        elif state == "ok" and isinstance(temperature, (int, float)):
            if sp is None or sp["source"] == "indi":
                self._setpoint[dev] = {
                    "value": float(temperature),
                    "since": ts,
                    "source": "indi",
                    "reached": True,
                }
        elif state == "busy" and sp is not None and sp["source"] == "indi" and sp["reached"]:
            del self._setpoint[dev]  # a new target we can't see; wait for it to be reached
            self._over_since.pop(dev, None)

    def _temperature(self, ts: float, dev: str, value: float) -> list[Event]:
        self._temp[dev] = value
        self._assign()
        return self._check(ts, dev)

    def _check(self, ts: float, dev: str) -> list[Event]:
        sp = self._setpoint.get(dev)
        if sp is None or dev not in self._temp:
            return []
        temp = self._temp[dev]
        if temp <= sp["value"] + self.tolerance:
            self._over_since.pop(dev, None)
            if abs(temp - sp["value"]) <= self.tolerance:
                sp["reached"] = True
            if dev in self._raised:
                del self._raised[dev]
                body = f"{dev}: {_c(temp)}, setpoint {_c(sp['value'])}."
                return [
                    make_alert(
                        ts,
                        self.session,
                        "cooler_ok",
                        "normal",
                        "Cooler at setpoint",
                        body,
                        immediate=True,
                        device=dev,
                    )
                ]
        elif sp["reached"]:
            self._over_since.setdefault(dev, ts)
        return []

    def _alert(self, now: float, dev: str, rule: str, since: float) -> Event:
        sp, temp = self._setpoint[dev], self._temp[dev]
        if rule == "cooler_cant_hold":
            title = "Cooler can't hold"
            what = (
                f"{dev} is at {_c(temp)}, {temp - sp['value']:.1f} °C over its setpoint of "
                f"{_c(sp['value'])}, since {hhmm(since)} ({minutes(now - since)})."
            )
        else:
            title = "Cooler can't reach setpoint"
            what = (
                f"{dev} is at {_c(temp)}, {minutes(now - since)} after the setpoint of "
                f"{_c(sp['value'])} was set at {hhmm(since)}."
            )
        lines = [what]
        if self._ambient is not None:
            lines.append(
                f"Ambient {_c(self._ambient)}: on a warm night, a setpoint nearer ambient helps."
            )
        lines.append("New frames won't match darks taken at the setpoint.")
        return make_alert(now, self.session, rule, "high", title, "\n".join(lines), device=dev)
