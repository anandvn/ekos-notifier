"""PHD2 event-server messages → ``Event``s (source ``phd2``).

PHD2 (TCP 4400) sends one JSON object per line. On connect it sends ``Version``, then replays
its current state (lock position, star, calibration, ``StartGuiding``…) and ends the burst with
``AppState``; events in that burst are marked ``"snapshot": True``. After that it's a live
stream. ``AppState`` is only sent on connect, so the decoder tracks the state from the events
that follow, as PHD2's own docs tell clients to.

Kinds:

- ``phd2.connected`` (``version``) on ``Version``. The adapter adds ``phd2.lost`` when the
  connection drops.
- ``phd2.state``: ``stopped | selected | calibrating | guiding | lost_lock | paused | looping``,
  only when it changes.
- ``phd2.step``: one guide step: ``frame``, ``dx``/``dy`` and ``ra``/``dec`` (raw distance) in
  pixels, ``ra_pulse``/``dec_pulse`` in ms (signed: + is West/North), ``snr``, ``mass``,
  ``hfd`` (px), ``error`` (PHD2's star error code, omitted when 0).
- ``phd2.star_lost``: ``frame``, ``snr``, ``mass``, ``avg_dist``, ``error``, ``status``.
- ``phd2.settle``: ``ok``, ``error``, ``frames``, ``dropped``; the result of a settle after
  guiding starts or a dither.
- ``phd2.dither``: ``dx``, ``dy`` (px).
- ``phd2.calibration``: ``result`` ``started | complete | failed``, ``mount``, ``reason``.
- ``phd2.alert``: ``message``, ``type`` (info | question | warning | error).
- ``phd2.lock_shift_limit``: the lock position hit the edge of the camera frame.

Per-step calibration progress, ``Settling`` progress, ``ConfigurationChange`` and lock-position
bookkeeping aren't recorded.
"""

from __future__ import annotations

import logging
from typing import Any

from ekos_notifier.events import Event

log = logging.getLogger(__name__)

SOURCE = "phd2"
APP_STATES = {
    "Stopped": "stopped",
    "Selected": "selected",
    "Calibrating": "calibrating",
    "Guiding": "guiding",
    "LostLock": "lost_lock",
    "Paused": "paused",
    "Looping": "looping",
}
IGNORED = {
    "Calibrating",
    "Settling",
    "SettleBegin",
    "ConfigurationChange",
    "LockPositionSet",
    "LockPositionLost",
    "StarSelected",
}


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _pulse(duration: Any, direction: Any, positive: str) -> float | None:
    ms = _num(duration)
    if ms is None:
        return None
    return ms if direction == positive else -ms


class Phd2Decoder:
    """Stateful: one per connection (call ``reset()`` when reconnecting)."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.state: str | None = None
        self.version: str | None = None
        self._in_burst = False
        self._before_pause: str | None = None

    def lost(self, ts: float, reason: str) -> list[Event]:
        """The connection dropped (or PHD2 quit)."""
        connected = self.version is not None
        self.reset()
        return [Event(ts, "phd2.lost", {"reason": reason}, SOURCE)] if connected else []

    def feed(self, ts: float, msg: dict[str, Any]) -> list[Event]:
        name = msg.get("Event")
        if not isinstance(name, str):
            return []  # an RPC reply (we send none) or junk
        out: list[Event] = []
        snapshot = self._in_burst

        def send(kind: str, data: dict[str, Any]) -> None:
            if snapshot:
                data["snapshot"] = True
            out.append(Event(ts, kind, data, SOURCE))

        def state(new: str) -> None:
            if new != self.state:
                self.state = new
                send("phd2.state", {"state": new})

        if name == "Version":
            self.reset()
            self.version = str(msg.get("PHDVersion", "")) + str(msg.get("PHDSubver") or "")
            self._in_burst = True
            out.append(Event(ts, "phd2.connected", {"version": self.version}, SOURCE))
        elif name == "AppState":
            self._in_burst = False
            app = msg.get("State")
            state(APP_STATES.get(app, str(app).lower()))
        elif name == "GuideStep":
            step = {
                "frame": msg.get("Frame"),
                "dx": _num(msg.get("dx")),
                "dy": _num(msg.get("dy")),
                "ra": _num(msg.get("RADistanceRaw")),
                "dec": _num(msg.get("DECDistanceRaw")),
                "ra_pulse": _pulse(msg.get("RADuration"), msg.get("RADirection"), "West"),
                "dec_pulse": _pulse(msg.get("DECDuration"), msg.get("DECDirection"), "North"),
                "snr": _num(msg.get("SNR")),
                "mass": _num(msg.get("StarMass")),
                "hfd": _num(msg.get("HFD")),
            }
            if msg.get("ErrorCode"):
                step["error"] = msg["ErrorCode"]
            state("guiding")
            send("phd2.step", {k: v for k, v in step.items() if v is not None})
        elif name == "StarLost":
            if self.state == "guiding":
                state("lost_lock")
            send(
                "phd2.star_lost",
                {
                    "frame": msg.get("Frame"),
                    "snr": _num(msg.get("SNR")),
                    "mass": _num(msg.get("StarMass")),
                    "avg_dist": _num(msg.get("AvgDist")),
                    "error": msg.get("ErrorCode"),
                    "status": msg.get("Status"),
                },
            )
        elif name == "StartGuiding":
            state("guiding")
        elif name == "GuidingStopped" or name == "LoopingExposuresStopped":
            state("stopped")
        elif name == "LoopingExposures":
            state("looping")
        elif name == "Paused":
            self._before_pause = self.state
            state("paused")
        elif name == "Resumed":
            state(self._before_pause or "guiding")
        elif name == "StartCalibration":
            state("calibrating")
            send("phd2.calibration", {"result": "started", "mount": msg.get("Mount")})
        elif name == "CalibrationComplete":
            send("phd2.calibration", {"result": "complete", "mount": msg.get("Mount")})
        elif name == "CalibrationFailed":
            send(
                "phd2.calibration",
                {"result": "failed", "mount": msg.get("Mount"), "reason": msg.get("Reason")},
            )
        elif name == "SettleDone":
            status = msg.get("Status")
            data = {
                "ok": status == 0,
                "frames": msg.get("TotalFrames"),
                "dropped": msg.get("DroppedFrames"),
            }
            if status != 0:
                data["error"] = msg.get("Error")
            send("phd2.settle", data)
        elif name == "GuidingDithered":
            send("phd2.dither", {"dx": _num(msg.get("dx")), "dy": _num(msg.get("dy"))})
        elif name == "Alert":
            send("phd2.alert", {"message": msg.get("Msg"), "type": msg.get("Type")})
        elif name == "LockPositionShiftLimitReached":
            send("phd2.lock_shift_limit", {})
        elif name not in IGNORED:
            log.debug("PHD2 event not recorded: %s", name)
        return out
