"""Device presets: YAML files mapping a device's INDI properties to canonical metrics.

A preset matches a device by its INDI interface (``interface: weather``) or driver executable
(``driver: indi_pegasus_ppba``), and lists ``metric: PROPERTY.ELEMENT``. Presets are data, added
by pull request without Python; each carries a ``status`` (verified, community, untested). A
preset applies only to the properties a device actually has, so one for a whole interface is
safe on devices that lack some of them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import yaml

# INDI's DRIVER_INTERFACE bits (libindi BaseDevice), checked against the simulators.
INTERFACES = {
    "telescope": 1 << 0,
    "ccd": 1 << 1,
    "guider": 1 << 2,
    "focuser": 1 << 3,
    "filter": 1 << 4,
    "dome": 1 << 5,
    "gps": 1 << 6,
    "weather": 1 << 7,
    "ao": 1 << 8,
    "dustcap": 1 << 9,
    "lightbox": 1 << 10,
    "detector": 1 << 11,
    "rotator": 1 << 12,
    "aux": 1 << 15,
    "output": 1 << 16,
    "input": 1 << 17,
    "power": 1 << 18,  # libindi 2.x PowerInterface (power boxes, dew controllers)
}


@dataclass(frozen=True)
class Preset:
    name: str
    status: str
    metrics: dict[str, tuple[str, str]]  # metric -> (property, element)
    interface: str | None = None
    driver: str | None = None
    source: str = field(default="", compare=False)

    def matches(self, driver_exec: str | None, interfaces: int) -> bool:
        if self.driver is not None:
            return driver_exec == self.driver
        if self.interface is not None:
            return bool(interfaces & INTERFACES[self.interface])
        return False


def parse(data: dict, source: str = "") -> Preset:
    match = data.get("match") or {}
    interface, driver = match.get("interface"), match.get("driver")
    if interface is not None and interface not in INTERFACES:
        raise ValueError(f"{source}: unknown interface {interface!r}")
    if (interface is None) == (driver is None):
        raise ValueError(f"{source}: match needs exactly one of 'interface' or 'driver'")
    metrics = {}
    for metric, target in (data.get("metrics") or {}).items():
        prop, sep, element = str(target).partition(".")
        if not sep or not element:
            raise ValueError(f"{source}: {metric}: expected PROPERTY.ELEMENT, got {target!r}")
        metrics[str(metric)] = (prop, element)
    return Preset(
        name=str(data.get("name") or source),
        status=str(data.get("status") or "untested"),
        metrics=metrics,
        interface=interface,
        driver=driver,
        source=source,
    )


def load_builtin() -> list[Preset]:
    out = []
    for item in sorted(
        resources.files("ekos_notifier.indi").joinpath("presets").iterdir(), key=str
    ):
        if item.name.endswith(".yaml"):
            out.append(parse(yaml.safe_load(item.read_text()), item.name))
    # Driver-specific presets first: they know the device better than an interface-wide one.
    return sorted(out, key=lambda p: p.driver is None)


def load_file(path: str | Path) -> Preset:
    return parse(yaml.safe_load(Path(path).read_text()), str(path))
