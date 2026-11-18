"""INDI XML elements → ``Event``s (source ``indi``). Shared by the live client and replays.

The INDI server streams top-level elements: ``defXXXVector`` (a property and its current
values, sent on connect and when a device defines it), ``setXXXVector`` (new values),
``delProperty`` (gone, e.g. the device disconnected) and ``message``. The decoder keeps each
device's driver, interfaces and connection state, and maps properties to canonical metrics with
presets (by interface or driver) and the user's own mappings.

Kinds:

- ``indi.device``: ``device``, ``driver``, ``interfaces`` (names), ``connected``; when a device
  appears or its connection changes.
- ``metric``: ``metric``, ``value``, ``device``, ``via`` (the preset's name, or "config"). Sent
  when the value moves past ``DEADBAND``, or for an update ``HEARTBEAT`` seconds after the last
  one sent. INDI itself only sends a value when it changes (a settled camera cooler goes quiet,
  seen live), so a reading stays current until ``indi.capabilities`` says it's gone; age alone
  doesn't make it stale. ``env.dewpoint`` is derived (Magnus
  formula) for a device that has temperature and humidity but no dew point of its own.
- ``indi.capabilities``: ``metrics`` = {metric: [devices]}, whenever that set changes (devices
  connecting and disconnecting during the night), for "not available on this rig".
- ``power.output``: ``device``, ``output`` (the element, e.g. ``POWER_CHANNEL_1``), ``kind`` (``dc``
  or ``dew``), ``label`` (the box's own name for it, e.g. "Camera"), ``on``, ``current`` (A) and
  ``duty`` (%, dew), each None when the box doesn't report it. From libindi's standard
  PowerInterface properties (``POWER_CHANNELS``/``POWER_CURRENTS``/``POWER_LABELS`` and the
  ``DEW_*`` set). Sent when the switch, label or duty changes, the current moves past
  ``DEADBAND``, or for an update ``HEARTBEAT`` seconds after the last. A box with outputs adds
  ``power.outputs`` to ``indi.capabilities``.
- ``mount.coords`` (``ra`` hours, ``dec`` degrees, JNow, from a mount's EQUATORIAL_EOD_COORD) on a
  move of more than ~3' or once a minute; ``site.location`` (``latitude``, ``longitude`` east,
  ``elevation``, ``via`` "indi") from its GEOGRAPHIC_COORD when it changes.

Driver debug and other ``message`` elements, BLOBs and everything unmapped are ignored.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass, field

from ekos_notifier.events import Event
from ekos_notifier.indi.presets import INTERFACES, Preset

SOURCE = "indi"
HEARTBEAT = 60.0
DEADBAND = {
    "temperature": 0.1,
    "humidity": 0.5,
    "dewpoint": 0.1,
    "voltage": 0.05,
    "current": 0.05,
    "watts": 0.5,
}
# libindi PowerInterface outputs: kind -> (switches, currents, labels, duty cycles). Elements are
# named per output (POWER_CHANNEL_1, DEW_CHANNEL_2, …) and shared across the four properties.
OUTPUTS = {
    "dc": ("POWER_CHANNELS", "POWER_CURRENTS", "POWER_LABELS", None),
    "dew": ("DEW_CHANNELS", "DEW_CURRENTS", "DEW_LABELS", "DEW_DUTY_CYCLES"),
}
OUTPUT_PROPS = {p: kind for kind, props in OUTPUTS.items() for p in props if p}
SEXAGESIMAL = re.compile(r"^\s*(-?)(\d+)[:\s](\d+)(?:[:\s](\d+(?:\.\d*)?))?\s*$")


@dataclass(frozen=True)
class Mapping:
    """A user's own mapping from config: ``metric: "Device / PROPERTY.ELEMENT"``."""

    metric: str
    device: str
    prop: str
    element: str

    @classmethod
    def parse(cls, metric: str, target: str) -> Mapping:
        device, sep, rest = str(target).rpartition("/")
        prop, dot, element = rest.strip().partition(".")
        if not sep or not dot or not device.strip() or not prop or not element:
            raise ValueError(f"{metric}: expected 'Device / PROPERTY.ELEMENT', got {target!r}")
        return cls(metric, device.strip(), prop, element.strip())


@dataclass
class Device:
    name: str
    driver: str | None = None
    interfaces: int = 0
    connected: bool | None = None
    numbers: dict[str, dict[str, float]] = field(default_factory=dict)  # property -> element -> v
    switches: dict[str, dict[str, bool]] = field(default_factory=dict)  # outputs' on/off only
    texts: dict[str, dict[str, str]] = field(default_factory=dict)  # outputs' labels only

    def clear(self) -> None:
        self.numbers.clear()
        self.switches.clear()
        self.texts.clear()


def number(text: str | None) -> float | None:
    if text is None:
        return None
    text = text.strip()
    try:
        return float(text)
    except ValueError:
        pass
    m = SEXAGESIMAL.match(text)
    if not m:
        return None
    sign, d, mi, s = m.groups()
    value = int(d) + int(mi) / 60 + (float(s) if s else 0.0) / 3600
    return -value if sign else value


def dewpoint(temperature: float, humidity: float) -> float | None:
    """Magnus formula (Sonntag 1990 constants), °C. None outside 1–100 % RH."""
    if not 0 < humidity <= 100:
        return None
    a, b = 17.62, 243.12
    gamma = math.log(humidity / 100) + a * temperature / (b + temperature)
    return b * gamma / (a - gamma)


def _interface_names(bits: int) -> list[str]:
    return [name for name, bit in INTERFACES.items() if bits & bit]


class IndiDecoder:
    def __init__(self, presets: Iterable[Preset] = (), mappings: Iterable[Mapping] = ()) -> None:
        self.presets = list(presets)
        self.mappings = list(mappings)
        self.reset()

    def reset(self) -> None:
        self.devices: dict[str, Device] = {}
        self._last: dict[tuple[str, str], tuple[float, float]] = {}  # (device, metric) -> ts, v
        self._capabilities: dict[str, list[str]] = {}
        self._pointing: dict[str, tuple[float, float, float]] = {}  # device -> ts, ra, dec
        self._site: dict[str, tuple[float, float, float | None]] = {}
        self._outputs: dict[tuple[str, str], tuple[float, tuple]] = {}  # (device, output) -> ts, s

    def lost(self, ts: float) -> list[Event]:
        """The server went away: every metric is gone with it."""
        self.devices.clear()
        self._last.clear()
        self._outputs.clear()
        return self._capabilities_changed(ts)

    # -- feeding -------------------------------------------------------------------------------

    def feed(self, ts: float, el: ET.Element) -> list[Event]:
        tag = el.tag
        name = el.get("device")
        if not name or tag in ("message", "getProperties"):
            return []
        dev = self.devices.get(name)
        out: list[Event] = []
        if dev is None:
            dev = self.devices[name] = Device(name)
        prop = el.get("name")
        if tag == "delProperty":
            if prop is None:
                del self.devices[name]
                self._forget(name)
            else:
                dev.numbers.pop(prop, None)
                dev.switches.pop(prop, None)
                dev.texts.pop(prop, None)
                self._forget(name, prop)
            return out + self._capabilities_changed(ts)
        if tag.endswith("TextVector") and prop == "DRIVER_INFO" and tag[:3] in ("def", "set"):
            out += self._driver_info(ts, dev, el)
        elif tag.endswith("SwitchVector") and prop == "CONNECTION" and tag[:3] in ("def", "set"):
            out += self._connection(ts, dev, el)
        elif prop in OUTPUT_PROPS and tag[:3] in ("def", "set") and tag.endswith("SwitchVector"):
            dev.switches.setdefault(prop, {}).update(
                {one.get("name"): (one.text or "").strip() == "On" for one in el if one.get("name")}
            )
            out += self._output_events(ts, dev, OUTPUT_PROPS[prop])
            if tag.startswith("def"):
                out += self._capabilities_changed(ts)
        elif prop in OUTPUT_PROPS and tag[:3] in ("def", "set") and tag.endswith("TextVector"):
            dev.texts.setdefault(prop, {}).update(
                {one.get("name"): (one.text or "").strip() for one in el if one.get("name")}
            )
            out += self._output_events(ts, dev, OUTPUT_PROPS[prop])
        elif tag.endswith("NumberVector") and tag[:3] in ("def", "set") and prop:
            values = {
                one.get("name"): number(one.text)
                for one in el
                if one.tag in ("defNumber", "oneNumber") and one.get("name")
            }
            dev.numbers.setdefault(prop, {}).update(
                {k: v for k, v in values.items() if v is not None}
            )
            out += self._metrics(ts, dev, prop)
            if prop in OUTPUT_PROPS:
                out += self._output_events(ts, dev, OUTPUT_PROPS[prop])
            if dev.interfaces & INTERFACES["telescope"]:
                out += self._mount(ts, dev, prop)
            if tag.startswith("def"):
                out += self._capabilities_changed(ts)
        return out

    # -- helpers -------------------------------------------------------------------------------

    def _driver_info(self, ts: float, dev: Device, el: ET.Element) -> list[Event]:
        texts = {one.get("name"): (one.text or "").strip() for one in el}
        driver = texts.get("DRIVER_EXEC") or dev.driver
        try:
            interfaces = int(texts.get("DRIVER_INTERFACE") or dev.interfaces)
        except ValueError:
            interfaces = dev.interfaces
        if (driver, interfaces) == (dev.driver, dev.interfaces):
            return []
        dev.driver, dev.interfaces = driver, interfaces
        return [self._device_event(ts, dev)] + self._capabilities_changed(ts)

    def _connection(self, ts: float, dev: Device, el: ET.Element) -> list[Event]:
        states = {one.get("name"): (one.text or "").strip() == "On" for one in el}
        if "CONNECT" not in states:
            return []
        connected = states["CONNECT"]
        if connected == dev.connected:
            return []
        dev.connected = connected
        # On connect INDI sends CONNECTION before DRIVER_INFO: announce the device once its
        # driver is known (DRIVER_INFO does it then).
        out = [self._device_event(ts, dev)] if dev.driver is not None else []
        if not connected:
            dev.clear()  # its readings are gone (INDI deletes them too)
            self._forget(dev.name)
            out += self._capabilities_changed(ts)
        return out

    def _device_event(self, ts: float, dev: Device) -> Event:
        data = {
            "device": dev.name,
            "driver": dev.driver,
            "interfaces": _interface_names(dev.interfaces),
            "connected": dev.connected,
        }
        return Event(ts, "indi.device", data, SOURCE)

    def _sources(self, dev: Device) -> list[tuple[str, str, str, str]]:
        """(metric, property, element, via) this device provides, by mapping or preset."""
        out = [
            (m.metric, m.prop, m.element, "config") for m in self.mappings if m.device == dev.name
        ]
        mapped = {metric for metric, *_ in out}
        for preset in self.presets:
            if preset.matches(dev.driver, dev.interfaces):
                for metric, (prop, element) in preset.metrics.items():
                    if metric not in mapped:
                        mapped.add(metric)
                        out.append((metric, prop, element, preset.name))
        return out

    def _available(self, dev: Device) -> dict[str, str]:
        """metric -> via, for the values this device has right now (including derived)."""
        have = {
            metric: via
            for metric, prop, element, via in self._sources(dev)
            if element in dev.numbers.get(prop, {})
        }
        if "env.dewpoint" not in have and (dev.name, "env.dewpoint") in self._last:
            have["env.dewpoint"] = "derived"  # only once a dew point could actually be worked out
        if any(dev.switches.get(p) or dev.numbers.get(p) for p in OUTPUT_PROPS):
            have["power.outputs"] = "power interface"
        return have

    def _metrics(self, ts: float, dev: Device, prop: str) -> list[Event]:
        out = []
        values: dict[str, tuple[float, str]] = {}
        for metric, p, element, via in self._sources(dev):
            value = dev.numbers.get(p, {}).get(element)
            if value is not None:
                values[metric] = (value, via)
                if p == prop:
                    out += self._emit(ts, dev.name, metric, value, via)
        if "env.dewpoint" not in values and {"env.temperature", "env.humidity"} <= values.keys():
            sources = {m for m, p, *_ in self._sources(dev) if p == prop}
            if sources & {"env.temperature", "env.humidity"}:
                dp = dewpoint(values["env.temperature"][0], values["env.humidity"][0])
                if dp is not None:
                    first = (dev.name, "env.dewpoint") not in self._last
                    out += self._emit(ts, dev.name, "env.dewpoint", round(dp, 2), "derived")
                    if first:
                        out += self._capabilities_changed(ts)
        return out

    def _output_events(self, ts: float, dev: Device, kind: str) -> list[Event]:
        """``power.output`` for each of this kind's outputs whose state is news."""
        switches, currents, labels, duties = OUTPUTS[kind]
        sw = dev.switches.get(switches, {})
        amps = dev.numbers.get(currents, {})
        names = dev.texts.get(labels, {})
        duty = dev.numbers.get(duties, {}) if duties else {}
        out = []
        for output in sorted({*sw, *amps}):
            state = (sw.get(output), names.get(output), amps.get(output), duty.get(output))
            key = (dev.name, output)
            last = self._outputs.get(key)
            if last is not None and ts - last[0] < HEARTBEAT:
                was = last[1]
                moved = (
                    state[2] is not None
                    and was[2] is not None
                    and abs(state[2] - was[2]) >= DEADBAND["current"]
                ) or ((state[2] is None) != (was[2] is None))
                if not moved and (state[0], state[1], state[3]) == (was[0], was[1], was[3]):
                    continue
            self._outputs[key] = (ts, state)
            on, label, current, dc = state
            data = {
                "device": dev.name,
                "output": output,
                "kind": kind,
                "label": label or output,
                "on": on,
                "current": None if current is None else round(current, 3),
                "duty": dc,
            }
            out.append(Event(ts, "power.output", data, SOURCE))
        return out

    def _mount(self, ts: float, dev: Device, prop: str) -> list[Event]:
        """A mount's pointing (EQUATORIAL_EOD_COORD) and site (GEOGRAPHIC_COORD)."""
        values = dev.numbers.get(prop, {})
        if prop == "EQUATORIAL_EOD_COORD" and "RA" in values and "DEC" in values:
            ra, dec = values["RA"], values["DEC"]
            last = self._pointing.get(dev.name)
            # Tracking jitter isn't news: only a move of more than ~3' (or a minute's heartbeat).
            if last is not None and ts - last[0] < HEARTBEAT:
                if abs(dec - last[2]) < 0.05 and abs((ra - last[1] + 12) % 24 - 12) * 15 < 0.05:
                    return []
            self._pointing[dev.name] = (ts, ra, dec)
            return [Event(ts, "mount.coords", {"ra": ra, "dec": dec, "device": dev.name}, SOURCE)]
        if prop == "GEOGRAPHIC_COORD" and "LAT" in values and "LONG" in values:
            site = (values["LAT"], (values["LONG"] + 180.0) % 360.0 - 180.0, values.get("ELEV"))
            if self._site.get(dev.name) == site:
                return []
            self._site[dev.name] = site
            data = {"latitude": site[0], "longitude": site[1], "elevation": site[2], "via": "indi"}
            return [Event(ts, "site.location", data, SOURCE)]
        return []

    def _emit(self, ts: float, device: str, metric: str, value: float, via: str) -> list[Event]:
        last = self._last.get((device, metric))
        band = DEADBAND.get(metric.rsplit(".", 1)[-1], 0.0)
        if last is not None and ts - last[0] < HEARTBEAT and abs(value - last[1]) < band:
            return []
        if last is not None and ts - last[0] < HEARTBEAT and band == 0.0 and value == last[1]:
            return []
        self._last[(device, metric)] = (ts, value)
        data = {"metric": metric, "value": value, "device": device, "via": via}
        return [Event(ts, "metric", data, SOURCE)]

    def _forget(self, device: str, prop: str | None = None) -> None:
        if prop is None or prop in OUTPUT_PROPS:
            for key in [k for k in self._outputs if k[0] == device]:
                del self._outputs[key]
        for key in [k for k in self._last if k[0] == device]:
            if prop is None or any(
                metric == key[1] and p == prop for metric, p, *_ in self._all_sources(device)
            ):
                del self._last[key]

    def _all_sources(self, device: str) -> list[tuple[str, str, str, str]]:
        dev = self.devices.get(device)
        return self._sources(dev) if dev else []

    def _capabilities_changed(self, ts: float) -> list[Event]:
        caps: dict[str, list[str]] = {}
        for dev in self.devices.values():
            for metric in self._available(dev):
                caps.setdefault(metric, []).append(dev.name)
        caps = {m: sorted(d) for m, d in sorted(caps.items())}
        if caps == self._capabilities:
            return []
        self._capabilities = caps
        return [Event(ts, "indi.capabilities", {"metrics": caps}, SOURCE)]
