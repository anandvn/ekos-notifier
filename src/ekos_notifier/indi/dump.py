"""``ekos-notifier indi-dump``: every INDI device's properties, for a "support my device" issue.

Connects read-only (``getProperties`` only), collects the property definitions for a few
seconds, and writes them as XML with the site location removed (``GEOGRAPHIC_COORD``, and
sexagesimal coordinates in text). Prints what was recognised and the numeric readings that look
like a temperature, humidity, dew point, voltage or current, as candidates for a config mapping.
"""

from __future__ import annotations

import asyncio
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from ekos_notifier.i18n import _
from ekos_notifier.indi.adapter import GET_PROPERTIES
from ekos_notifier.indi.decode import OUTPUT_PROPS, IndiDecoder
from ekos_notifier.indi.presets import INTERFACES, Preset

LOCATION = {"GEOGRAPHIC_COORD"}
# "CURRENT" only as a word end ("SENSOR_CURRENT"), not "ALIGNMENT_POINTSET_CURRENT_ENTRY" (live).
LOOKS_LIKE = re.compile(r"TEMP|HUMID|DEW|VOLT|CURRENT(?![_A-Z])|AMP(?![_A-Z])|POWER", re.I)
# Thresholds, limits, set points and simulator knobs (WEATHER_CONTROL), not readings.
NOT_READINGS = re.compile(
    r"MIN|MAX|LIMIT|WARN|ALERT|THRESHOLD|OFFSET|TARGET|SETPOINT|RAMP|CONTROL|SETTINGS|^SIM_",
    re.I,
)


async def collect(
    host: str, port: int, seconds: float = 5.0, quiet: float = 1.5
) -> list[ET.Element]:
    """The def* elements the server sends after getProperties, until it's quiet or time's up."""
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 5.0)
    parser = ET.XMLPullParser(events=("start", "end"))
    parser.feed(b"<stream>")
    elements: list[ET.Element] = []
    depth = 0
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    try:
        writer.write(GET_PROPERTIES)
        await writer.drain()
        while (left := deadline - loop.time()) > 0:
            try:
                chunk = await asyncio.wait_for(reader.read(65536), min(quiet, left))
            except asyncio.TimeoutError:
                if elements:
                    break  # the server has said everything it had
                continue
            if not chunk:
                break
            parser.feed(chunk)
            for event, el in parser.read_events():
                if event == "start":
                    depth += 1
                    continue
                depth -= 1
                if depth == 1 and el.tag.startswith("def"):
                    elements.append(el)
    finally:
        writer.close()
    return elements


HOME = re.compile(r"/home/[^/\s]+")


def scrub(elements: list[ET.Element]) -> list[ET.Element]:
    """Remove the site location (GEOGRAPHIC_COORD values, dd:mm:ss in texts) and the user name
    in home paths (upload directories, seen live)."""
    for el in elements:
        if el.get("name") in LOCATION:
            for child in el:
                child.text = " 0 "
        elif el.tag == "defTextVector":
            for child in el:
                if child.text and re.search(r"-?\d{1,3}[:°]\d{2}[:']\d{2}", child.text):
                    child.text = " (removed) "
                elif child.text:
                    child.text = HOME.sub("/home/user", child.text)
    return elements


def write(elements: list[ET.Element], path: Path) -> None:
    root = ET.Element("indiDump")
    root.extend(elements)
    ET.indent(root)
    path.write_text(ET.tostring(root, encoding="unicode") + "\n")


def summary(elements: list[ET.Element], presets: list[Preset]) -> list[str]:
    decoder = IndiDecoder(presets)
    events = []
    for el in elements:
        events += decoder.feed(0.0, el)
    caps = next((e.data["metrics"] for e in reversed(events) if e.kind == "indi.capabilities"), {})
    lines = []
    for dev in decoder.devices.values():
        what = ", ".join(_names(dev.interfaces)) or _("no interfaces reported")
        state = _("connected") if dev.connected else _("not connected (connect it in Ekos first)")
        lines.append(
            _("{device}: {driver} ({interfaces}), {state}").format(
                device=dev.name,
                driver=dev.driver or _("unknown driver"),
                interfaces=what,
                state=state,
            )
        )
        mine = sorted(m for m, devices in caps.items() if dev.name in devices)
        if mine:
            lines.append("  " + _("recognised: {metrics}").format(metrics=", ".join(mine)))
        known = {(prop, element) for _, prop, element, _ in decoder._sources(dev)}
        for prop, values in sorted(dev.numbers.items()):
            if NOT_READINGS.search(prop) or prop in OUTPUT_PROPS:
                continue  # not a reading, or a power box output (read as power.outputs)
            for element, value in values.items():
                if (prop, element) in known:
                    continue  # already recognised
                if LOOKS_LIKE.search(element) and not NOT_READINGS.search(element):
                    lines.append(
                        "  "
                        + _('candidate: "{device} / {property}.{element}" = {value}').format(
                            device=dev.name, property=prop, element=element, value=f"{value:g}"
                        )
                    )
    return lines


def _names(bits: int) -> list[str]:
    return [name for name, bit in INTERFACES.items() if bits & bit]
