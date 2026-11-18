"""A session's numbers for its summary (docs/design.md, "Normal priority"): median HFR, median
guide RMS, the lowest battery voltage, the ambient temperature and humidity range, the camera's
temperature range, and the problem alerts raised.

``SessionStats`` takes the session's events (and, after a restart, its stored frames), and
``lines()`` words what it has. Each line is left out when its numbers aren't on the rig: no
power box, no lines about power.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from ekos_notifier.events import Event

PROBLEMS = ("emergency", "high")


class Range:
    def __init__(self) -> None:
        self.low: float | None = None
        self.high: float | None = None

    def add(self, value: float) -> None:
        self.low = value if self.low is None else min(self.low, value)
        self.high = value if self.high is None else max(self.high, value)

    def words(self, fmt: str, unit: str) -> str | None:
        if self.low is None or self.high is None:
            return None
        low, high = format(self.low, fmt), format(self.high, fmt)
        text = low if low == high else f"{low} to {high}"
        return text.replace("-", "−") + unit


class SessionStats:
    def __init__(self) -> None:
        self.hfr: list[float] = []
        self.rms: list[float] = []
        self.volts: dict[str, float] = {}  # device -> lowest
        self.ambient = Range()
        self.humidity = Range()
        self.cameras: dict[str, Range] = {}
        self._camera: str | None = None  # Ekos Capture's camera
        self.problems: Counter[str] = Counter()

    def frames(self, rows: Iterable[Mapping[str, Any]]) -> None:
        """Stored frames, after a restart: their HFR and guide RMS."""
        for f in rows:
            if f["type"] != "light":
                continue
            if isinstance(f.get("hfr"), (int, float)) and f["hfr"] > 0:
                self.hfr.append(float(f["hfr"]))
            if isinstance(f.get("guide_rms"), (int, float)):
                self.rms.append(float(f["guide_rms"]))

    def note(self, event: Event) -> None:
        kind, d = event.kind, event.data
        if kind == "capture.frame" and d.get("type") == "light":
            if isinstance(d.get("hfr"), (int, float)) and d["hfr"] > 0:
                self.hfr.append(float(d["hfr"]))
        elif kind == "guide.rms" and d.get("ra") is not None and d.get("dec") is not None:
            self.rms.append((float(d["ra"]) ** 2 + float(d["dec"]) ** 2) ** 0.5)
        elif kind == "capture.camera" and d.get("camera"):
            self._camera = d["camera"]
        elif kind == "metric" and isinstance(d.get("value"), (int, float)):
            value, device = float(d["value"]), d.get("device") or ""
            metric = d.get("metric")
            if metric == "power.voltage":
                self.volts[device] = min(self.volts.get(device, value), value)
            elif metric == "env.temperature":
                self.ambient.add(value)
            elif metric == "env.humidity":
                self.humidity.add(value)
            elif metric == "camera.temperature":
                self.cameras.setdefault(device, Range()).add(value)

    def alerts(self, alerts: Iterable[Event]) -> None:
        """The session's alerts: problems (High or Emergency as raised) are counted."""
        for a in alerts:
            if a.kind != "alert" or a.data.get("off"):
                continue
            if a.data.get("default_priority", a.data.get("priority")) in PROBLEMS:
                self.problems[a.data.get("title", a.data.get("rule", "?"))] += 1

    def lines(self) -> list[str]:
        out = []
        quality = []
        if self.hfr:
            quality.append(f"HFR {statistics.median(self.hfr):.2f}")
        if self.rms:
            quality.append(f"guide RMS {statistics.median(self.rms):.2f}″")
        if quality:
            out.append("Median " + ", ".join(quality) + ".")
        conditions = []
        if (ambient := self.ambient.words(".1f", " °C")) is not None:
            conditions.append(f"ambient {ambient}")
        if (rh := self.humidity.words(".0f", "%")) is not None:
            conditions.append(f"RH {rh}")
        camera = self.cameras.get(self._camera or "")
        if camera is None and len(self.cameras) == 1:
            [camera] = self.cameras.values()
        if camera is not None and (temps := camera.words(".1f", " °C")) is not None:
            conditions.append(f"camera {temps}")
        if self.volts:
            low = min(self.volts.values())
            conditions.append(f"lowest battery {low:.2f} V")
        if conditions:
            text = ", ".join(conditions) + "."
            out.append(text[0].upper() + text[1:])
        if self.problems:
            parts = [f"{t} ×{n}" if n > 1 else t for t, n in self.problems.items()]
            out.append("Problems: " + ", ".join(parts) + ".")
        return out
