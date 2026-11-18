"""Power alerts (docs/design.md, "Alerts"): battery voltage and power-box outputs.

Inputs are canonical, so any box works: ``power.voltage`` and ``power.current`` metrics (the
standard power-interface preset, or the user's own ``indi.metrics`` mapping) and ``power.output``
events (libindi's standard PowerInterface outputs, see ``ekos_notifier.indi.decode``). With none
of them on a rig, these rules never fire.

- **Battery critical** (Emergency): ``power.voltage`` under the critical level for ``SUSTAIN``
  seconds (a slew's dip isn't a flat battery). Sent at any time, session or not: it's the one
  alert sent in Standby.
- **Battery low** (High): under the low level for ``SUSTAIN`` seconds during an active session,
  once. Neither repeats until the voltage is back ``HYSTERESIS`` volts over the low level (a
  fresh battery or a charger), which sends **Battery OK** (Normal, immediate).
- **Power output off** (High): a DC output that was on and drawing current switches off during a
  session.
- **No current on an output** (High): a DC output stays on, but its current falls from at least
  ``DRAWING`` to under ``IDLE`` amps for ``DEAD_AFTER`` seconds: a fuse, a cable, or the device.
  Only on boxes that report per-output current.
- Both output alerts are held ``FAULT_HOLD`` seconds and dropped if Ekos shuts down meanwhile (a
  box set to power off on disconnect turns its outputs off as Ekos disconnects), and aren't
  raised in the ``SHUTDOWN_WINDOW`` after a shutdown starts. Once per output per session; **Power
  output back** (Normal, immediate) when it's on and drawing again.

Dew outputs aren't checked here: they belong with the dew-risk alerts.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ekos_notifier.alerts import SessionRule, hhmm, make_alert, minutes
from ekos_notifier.events import Event

SUSTAIN = 60.0
HYSTERESIS = 0.3
DRAWING = 0.1  # A: an output powering something
IDLE = 0.02  # A: an output powering nothing
DEAD_AFTER = 30.0
FAULT_HOLD = 10.0
SHUTDOWN_WINDOW = 60.0
SHUTDOWN_LOGS = ("Disconnecting INDI devices", "proceeding to stop Ekos/INDI")
# Battery presets: (low, critical) volts. LiFePO4 holds ~13 V until nearly empty, so its levels
# sit higher than lead-acid's; on mains, voltage alerts are off.
BATTERIES: dict[str, tuple[float | None, float | None]] = {
    "agm_12v": (12.0, 11.5),
    "lifepo4_12v": (12.8, 12.0),
    "mains": (None, None),
}
BATTERY_RULES = ("battery_low", "battery_critical")
OUTPUT_RULES = ("power_output_off", "power_output_dead")


class PowerRule(SessionRule):
    def __init__(
        self,
        low_volts: float | None = 12.0,
        critical_volts: float | None = 11.5,
        *,
        sustain: float = SUSTAIN,
    ) -> None:
        super().__init__()
        self.low = low_volts
        self.critical = critical_volts
        self.sustain = sustain
        self._volts: dict[str, float] = {}
        self._amps: dict[str, float] = {}
        self._below: dict[tuple[str, str], float] = {}  # (device, level) -> since
        self._level: dict[str, str] = {}  # device -> the battery alert sent ("low"/"critical")
        self._outputs: dict[tuple[str, str], dict[str, Any]] = {}  # (device, output) -> state
        self._pending: dict[tuple[str, str], Event] = {}  # output alerts being held
        self._faulted: dict[tuple[str, str], str] = {}  # output -> rule raised this session
        self._shutdown_at: float | None = None

    def resume(self, session_id: str | None, alerts: Iterable[Event] = ()) -> None:
        super().resume(session_id, alerts)
        for a in alerts:  # what was raised before a restart isn't raised again
            rule, device = a.data.get("rule"), a.data.get("device")
            if rule in BATTERY_RULES and device:
                self._level[device] = rule.removeprefix("battery_")
            elif rule == "battery_ok" and device:
                self._level.pop(device, None)
            elif rule in OUTPUT_RULES and device:
                self._faulted[(device, a.data.get("output", ""))] = rule
            elif rule == "power_output_back" and device:
                self._faulted.pop((device, a.data.get("output", "")), None)

    def on_session_closed(self) -> None:
        self._pending.clear()
        self._faulted.clear()
        for state in self._outputs.values():
            state["zero_since"] = None

    # The battery's Emergency doesn't wait for a session.
    def tick(self, now: float) -> list[Event]:
        return self.on_tick(now)

    # -- events ------------------------------------------------------------------------------

    def on_event(self, event: Event) -> list[Event]:
        kind, d = event.kind, event.data
        if kind == "metric" and isinstance(d.get("value"), (int, float)):
            if d.get("metric") == "power.voltage":
                return self._voltage(event.ts, d.get("device") or "", float(d["value"]))
            if d.get("metric") == "power.current":
                self._amps[d.get("device") or ""] = float(d["value"])
        elif kind == "indi.capabilities":
            metrics = d.get("metrics", {})
            self._gone(set(metrics.get("power.voltage", [])), set(metrics.get("power.outputs", [])))
        elif kind == "power.output" and d.get("kind") == "dc":
            return self._output(event)
        elif kind == "ekos.log" and any(s in str(d.get("text", "")) for s in SHUTDOWN_LOGS):
            self._shutting_down(event.ts)
        elif kind == "kstars.vanished" or (
            kind in ("ekos.state", "indi.state") and d.get("state") in ("idle", "error")
        ):
            self._shutting_down(event.ts)
        elif kind == "indi.device" and d.get("connected") is False:
            self._drop_device(d.get("device") or "")
        return []

    def on_tick(self, now: float) -> list[Event]:
        out = []
        for (device, level), since in sorted(self._below.items()):
            if now - since < self.sustain:
                continue
            sent = self._level.get(device)
            if level == "critical" and sent != "critical":
                self._level[device] = "critical"
                out.append(self._battery(now, device, "critical"))
            elif level == "low" and sent is None and self.session:
                self._level[device] = "low"
                out.append(self._battery(now, device, "low"))
        if self.session:
            for key, state in self._outputs.items():
                since = state.get("zero_since")
                if since is not None and now - since >= DEAD_AFTER:
                    state["zero_since"] = None
                    self._hold(now, key, "power_output_dead", state, since)
            for key, alert in list(self._pending.items()):
                if now - alert.ts >= FAULT_HOLD:
                    del self._pending[key]
                    self._faulted[key] = alert.data["rule"]
                    out.append(alert)
        return out

    def preflight(self, now: float) -> list[tuple[bool, str]]:
        out = []
        several = len(self._volts) > 1
        for device, volts in sorted(self._volts.items()):
            name = f"{device} " if several else ""
            if self.low is None and self.critical is None:
                out.append((True, f"{name}power {volts:.1f} V"))
            elif self.critical is not None and volts < self.critical:
                out.append(
                    (False, f"{name}battery {volts:.2f} V, under {self.critical:g} V critical")
                )
            elif self.low is not None and volts < self.low:
                out.append((False, f"{name}battery {volts:.2f} V, under {self.low:g} V low"))
            else:
                out.append((True, f"{name}battery {volts:.1f} V"))
        return out

    # -- battery -----------------------------------------------------------------------------

    def _voltage(self, ts: float, device: str, volts: float) -> list[Event]:
        self._volts[device] = volts
        for level, threshold in (("critical", self.critical), ("low", self.low)):
            if threshold is not None and volts < threshold:
                self._below.setdefault((device, level), ts)
            else:
                self._below.pop((device, level), None)
        recovered = (self.low if self.low is not None else self.critical or 0.0) + HYSTERESIS
        if device in self._level and volts >= recovered:
            del self._level[device]
            return [
                make_alert(
                    ts,
                    self.session,
                    "battery_ok",
                    "normal",
                    "Battery OK",
                    f"{device} reads {volts:.2f} V again.",
                    immediate=True,
                    device=device,
                )
            ]
        return []

    def _battery(self, now: float, device: str, level: str) -> Event:
        volts = self._volts[device]
        amps = self._amps.get(device)
        draw = f" with {amps:.1f} A drawn" if amps is not None else ""
        if level == "critical":
            body = (
                f"{device} reads {volts:.2f} V{draw}, under the {self.critical:g} V critical "
                f"level for {minutes(self.sustain)}. Equipment may shut down soon.\n"
                "Swap or charge the battery, or park the mount."
            )
            return make_alert(
                now,
                self.session,
                "battery_critical",
                "emergency",
                "Battery critical",
                body,
                device=device,
            )
        body = f"{device} reads {volts:.2f} V{draw}, under the {self.low:g} V low level."
        if self.critical is not None:
            body += f" Critical at {self.critical:g} V."
        return make_alert(
            now, self.session, "battery_low", "high", "Battery low", body, device=device
        )

    # -- outputs -----------------------------------------------------------------------------

    def _output(self, event: Event) -> list[Event]:
        d, ts = event.data, event.ts
        key = (d.get("device") or "", d.get("output") or "")
        on, current = d.get("on"), d.get("current")
        prev = self._outputs.get(key)
        state = {
            "label": d.get("label") or key[1],
            "on": on,
            "current": current,
            "drawing": False,
            "zero_since": None,
        }
        self._outputs[key] = state
        if on and current is not None and current >= DRAWING:
            state["drawing"] = True
        elif prev is not None and on and prev["on"]:
            state["drawing"] = prev["drawing"]
            if prev["drawing"] and current is not None and current < IDLE:
                state["zero_since"] = prev["zero_since"] or ts
            state["peak"] = prev.get("peak")
        if state["drawing"] and current is not None and current >= DRAWING:
            state["peak"] = current
        if prev is not None and on is False and prev["on"]:
            if prev["drawing"] or prev["current"] is None:
                state["peak"] = prev.get("peak")
                self._hold(ts, key, "power_output_off", state, ts)
        if on and (current is None or current >= DRAWING):
            self._pending.pop(key, None)
            if key in self._faulted:
                del self._faulted[key]
                return [self._back(ts, key, state)]
        return []

    def _hold(
        self, ts: float, key: tuple[str, str], rule: str, state: dict[str, Any], since: float
    ) -> None:
        if not self.session or key in self._faulted or key in self._pending:
            return
        if self._shutdown_at is not None and ts - self._shutdown_at < SHUTDOWN_WINDOW:
            return
        device, label, peak = key[0], state["label"], state.get("peak")
        was = f" (it was drawing {peak:.2f} A)" if peak is not None else ""
        if rule == "power_output_off":
            title = f"Power output off: {label}"
            body = (
                f"{device}: output “{label}” switched off at {hhmm(since)} during the "
                f"session{was}.\nIf you didn't turn it off, check the box and what it powers."
            )
        else:
            title = f"No current on {label}"
            body = (
                f"{device}: output “{label}” is on but has drawn no current since "
                f"{hhmm(since)}{was}.\nA blown fuse, a loose cable, or the device switched off?"
            )
        self._pending[key] = make_alert(
            ts, self.session, rule, "high", title, body, device=device, output=key[1]
        )

    def _back(self, ts: float, key: tuple[str, str], state: dict[str, Any]) -> Event:
        current = state["current"]
        drawing = f" and drawing {current:.2f} A" if current is not None else ""
        return make_alert(
            ts,
            self.session,
            "power_output_back",
            "normal",
            f"Power output back: {state['label']}",
            f"{key[0]}: “{state['label']}” is on{drawing} again.",
            immediate=True,
            device=key[0],
            output=key[1],
        )

    # -- devices coming and going ------------------------------------------------------------

    def _shutting_down(self, ts: float) -> None:
        self._shutdown_at = ts
        self._pending.clear()
        for state in self._outputs.values():
            state["zero_since"] = None

    def _drop_device(self, device: str) -> None:
        self._volts.pop(device, None)
        for key in [k for k in self._below if k[0] == device]:
            del self._below[key]
        for table in (self._outputs, self._pending):
            for key in [k for k in table if k[0] == device]:
                del table[key]

    def _gone(self, voltage: set[str], outputs: set[str]) -> None:
        for device in [dev for dev in self._volts if dev not in voltage]:
            self._volts.pop(device, None)
            for key in [k for k in self._below if k[0] == device]:
                del self._below[key]
        for table in (self._outputs, self._pending):
            for key in [k for k in table if k[0] not in outputs]:
                del table[key]
