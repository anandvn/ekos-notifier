"""Which device's reading counts, when several report the same metric.

A sensor at the rig (a power box's or a weather station's) measures where the telescope is. A
*secondary* source doesn't: OpenWeatherMap's driver reports the region's weather from the
internet, and the meta-weather driver re-reports other weather drivers. Seen live (2026-11-20):
with a Pegasus PPBM at 68-88 % humidity, OpenWeatherMap's 94 % raised Dew risk on its own.

So a secondary source counts only when no direct sensor reports that metric: rules keep every
device's readings, and pick with ``prefer_direct``. ``Marker`` adds ``secondary: true`` to a
secondary device's ``metric`` events, knowing each device's driver from its ``indi.device``
events; the pipeline runs it on every event, so nights stored before this replay right too.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, TypeVar

from ekos_notifier.events import Event

T = TypeVar("T")

SECONDARY_DRIVERS = {
    "indi_openweathermap_weather": "regional weather from the internet",
    "indi_meta_weather": "a copy of other weather drivers' readings",
}


class Marker:
    """Marks the readings of devices whose driver is a secondary source."""

    def __init__(self) -> None:
        self._drivers: dict[str, str] = {}  # device -> driver

    def mark(self, event: Event) -> Event:
        d = event.data
        if event.kind == "indi.device" and d.get("device") and d.get("driver"):
            self._drivers[d["device"]] = d["driver"]
        elif (
            event.kind == "metric"
            and not d.get("secondary")
            and self._drivers.get(d.get("device") or "") in SECONDARY_DRIVERS
        ):
            return replace(event, data={**d, "secondary": True})
        return event


def note(secondary: set[str], data: dict[str, Any]) -> None:
    """Keep ``secondary`` (device names) up to date from a ``metric`` event's data."""
    device = data.get("device") or ""
    if data.get("secondary"):
        secondary.add(device)
    else:
        secondary.discard(device)


def prefer_direct(by_device: dict[str, T], secondary: set[str]) -> dict[str, T]:
    """The direct sensors' readings, or the secondary ones when no direct sensor has one."""
    direct = {device: v for device, v in by_device.items() if device not in secondary}
    return direct or by_device
