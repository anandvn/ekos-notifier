"""Turn Ekos D-Bus signals into events.

Pure functions, no D-Bus connection: the live adapter and the replay reader both call
``decode_signal`` with the signal's path, member and arguments, so a recorded night exercises
exactly the code that runs on a rig.

Arguments may arrive in two shapes and are normalised with ``plain``: dbus-fast ``Variant``
objects (live bus) or busctl's ``{"type": ..., "data": ...}`` objects (recordings). Struct
arguments such as Ekos's ``(i)`` status come as one-element lists in both.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from ekos_notifier.ekos import states
from ekos_notifier.events import Event

KSTARS_NAME = "org.kde.kstars"
EKOS_PATH = "/KStars/Ekos"
INDI_PATH = "/KStars/INDI"


def plain(value: Any) -> Any:
    """Strip D-Bus variant wrappers so the value is plain JSON-able Python."""
    signature = getattr(value, "signature", None)
    if signature is not None and hasattr(value, "value"):  # dbus-fast Variant
        return plain(value.value)
    if isinstance(value, dict):
        if set(value) == {"type", "data"} and isinstance(value["type"], str):  # busctl variant
            return plain(value["data"])
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def _code(arg: Any) -> int:
    """Ekos sends states as a struct ``(i)``; accept a bare int too."""
    while isinstance(arg, list):
        arg = arg[0]
    return int(arg)


def _state(table: str, arg: Any, **extra: Any) -> dict[str, Any]:
    code = _code(arg)
    return {"state": states.name(table, code), "code": code, **extra}


def _measured(hfr: Any) -> float | None:
    """Ekos reports -1 when it found no stars or didn't measure."""
    hfr = float(hfr)
    return None if hfr < 0 else hfr


def _frame(args: list[Any]) -> dict[str, Any]:
    info = args[0]
    hfr = _measured(info.get("hfr", -1))
    frame = {
        "type": states.name("frame_type", int(info.get("type", -1))),
        "filter": info.get("filter") or None,
        "exposure": info.get("exposure"),
        "hfr": hfr,
        # Ekos sends starCount 0 alongside hfr -1 when it didn't measure the frame.
        "star_count": info.get("starCount") if hfr is not None else None,
        "eccentricity": _measured(info.get("eccentricity", -1)),
        "median": info.get("median"),
        "file": info.get("filename"),
        "width": info.get("width"),
        "height": info.get("height"),
        "binning": [info.get("binx"), info.get("biny")],
        "train": args[1] if len(args) > 1 else None,
    }
    return frame


Handler = Callable[[list[Any]], tuple[str, dict[str, Any]]]

# (module, member) -> handler returning (event kind, data). The module is the path below
# /KStars/Ekos ("" for /KStars/Ekos itself).
HANDLERS: dict[tuple[str, str], Handler] = {
    ("", "ekosStatusChanged"): lambda a: ("ekos.state", _state("communication", a[0])),
    ("", "indiStatusChanged"): lambda a: ("indi.state", _state("communication", a[0])),
    ("Capture", "newStatus"): lambda a: (
        "capture.state",
        _state("capture", a[0], train=a[1] if len(a) > 1 else None),
    ),
    ("Capture", "captureComplete"): lambda a: ("capture.frame", _frame(a)),
    # In the 3.8.5 introspection, but not emitted during the recorded flip (2026-11-16).
    ("Capture", "meridianFlipStarted"): lambda a: ("capture.flip_started", {}),
    ("Guide", "newStatus"): lambda a: ("guide.state", _state("guide", a[0])),
    # Arcseconds per the KStars source; not yet cross-checked against PHD2's own numbers.
    ("Guide", "newAxisDelta"): lambda a: ("guide.delta", {"ra": a[0], "dec": a[1]}),
    ("Guide", "newAxisSigma"): lambda a: ("guide.rms", {"ra": a[0], "dec": a[1]}),
    ("Focus", "newStatus"): lambda a: (
        "focus.state",
        _state("focus", a[0], train=a[1] if len(a) > 1 else None),
    ),
    ("Focus", "newHFR"): lambda a: (
        "focus.hfr",
        {
            "hfr": _measured(a[0]),
            "position": a[1],
            "in_autofocus": bool(a[2]),
            "train": a[3] if len(a) > 3 else None,
        },
    ),
    ("Align", "newStatus"): lambda a: ("align.state", _state("align", a[0])),
    ("Align", "newSolution"): lambda a: ("align.solution", dict(a[0])),
    ("Mount", "newStatus"): lambda a: ("mount.state", _state("mount", a[0])),
    ("Mount", "newParkStatus"): lambda a: ("mount.park", _state("park", a[0])),
    ("Mount", "pierSideChanged"): lambda a: ("mount.pier_side", _state("pier_side", a[0])),
    ("Mount", "newMeridianFlipSetup"): lambda a: ("mount.flip_setup_changed", {}),
    ("Scheduler", "newStatus"): lambda a: ("scheduler.state", _state("scheduler", a[0])),
}


def decode_signal(ts: float, path: str, member: str, args: list[Any]) -> list[Event]:
    """Events for one signal from KStars; ``[]`` for signals Ekos Notifier doesn't use."""
    args = plain(list(args))
    if path.startswith(INDI_PATH + "/"):
        if member in ("Connected", "Disconnected"):
            kind = "device.connected" if member == "Connected" else "device.disconnected"
            return [Event(ts, kind, {"object": path})]
        return []
    if path != EKOS_PATH and not path.startswith(EKOS_PATH + "/"):
        return []
    module = path[len(EKOS_PATH) + 1 :]
    if member == "newLog" and args:
        # Human-readable and translated: useful context, never a source of state.
        return [Event(ts, "ekos.log", {"module": module or "Ekos", "text": str(args[0])})]
    handler = HANDLERS.get((module, member))
    if handler is None:
        return []
    kind, data = handler(args)
    json.dumps(data)  # fail loudly here, not in the store, if a payload isn't plain
    return [Event(ts, kind, data)]


def decode_name_owner_changed(ts: float, args: list[Any]) -> list[Event]:
    """KStars appearing on, leaving or being replaced on the bus (a crash shows as leaving)."""
    name, old, new = (plain(a) for a in args)
    if name != KSTARS_NAME:
        return []
    events = []
    if old:
        events.append(Event(ts, "kstars.vanished", {"owner": old}))
    if new:
        events.append(Event(ts, "kstars.appeared", {"owner": new}))
    return events
