"""The hourly digest (docs/design.md, "Hourly digest and message wording").

While a session is active, one status message per interval (default 60 min, on the clock hour):

- **Title carries the verdict:** "23:00 — All's well" (Quiet: sent silently) or "23:00 —
  Attention: Guiding poor" (Normal) when a problem was raised in the interval and not cleared
  in it, or a problem raised earlier is still open (e.g. guiding lost, not yet recovered). With
  no problem and no light frame in the interval it says so instead of "All's well" ("No light
  frames since 22:14", still Quiet: the Scheduler waiting for a target is no problem).
- **Body:** target · filter · job progress; lights this interval and tonight (per filter,
  integration); calibration frames; guide RMS (median) and failed dithers; median HFR when Ekos
  measures it; and everything since the last digest: alerts (sent or held) and milestones such
  as a completed meridian flip.

A digest due less than ``min_session`` seconds after the session started is skipped (the
"Starting session" message just said it all). From INDI: camera temperature, ambient, humidity,
dew margin and power (voltage, its low this interval, current).
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta

from ekos_notifier import sources
from ekos_notifier.alerts import SessionRule, hhmm, make_alert
from ekos_notifier.ekos.translation import NUMBER, localized
from ekos_notifier.events import Event
from ekos_notifier.i18n import N_, _, ngettext, word

INTERVAL = 3600.0
MIN_SESSION = 600.0
# A problem alert and the alert that clears it.
CLEARED_BY = {
    "guiding_lost": "guiding_recovered",
    "guiding_poor": "guiding_poor_cleared",
    "capture_stalled": "capture_running",
    "ekos_stopped_mount_unparked": "mount_unparked_cleared",
    "battery_low": "battery_ok",
    "battery_critical": "battery_ok",
    "power_output_off": "power_output_back",
    "power_output_dead": "power_output_back",
    "dew_risk": "dew_risk_cleared",
    "cooler_cant_hold": "cooler_ok",
    "cooler_cant_reach": "cooler_ok",
    "disk_low": "disk_ok",
    "plate_solve_failed": "plate_solve_ok",
    "clouds": "clouds_cleared",
    "meridian_flip_failed": "meridian_flip_done",
    "meridian_flip_overdue": "meridian_flip_done",
    "weather_alert": "weather_ok",
}
CLEARS: dict[str, tuple[str, ...]] = {}
for _problem, _clear in CLEARED_BY.items():
    CLEARS[_clear] = (*CLEARS.get(_clear, ()), _problem)
# Problems with no all-clear alert of their own: open until the rig is working again (seen live:
# "All's well" with nothing captured since an abort).
CAPTURE_WORKING = {"capturing", "image_received", "in_progress", "changing_filter", "dithering"}
OPEN_UNTIL = {
    "capture_aborted": "capture running",
    "scheduler_aborted": "scheduler running",
    "autofocus_failed": "focus complete",
}
NOT_LISTED = {"digest", "session_started"}
JOB = re.compile(r"Job '(.+?)' ")
CAMERA = re.compile(r"^\[([^\]]+)\] ")
JOB_PLAN = re.compile(rf"Job requires ({NUMBER})-second (.+?) images, has (\d+)/(\d+) frames")
MILESTONES = {
    "Meridian flip completed OK": N_("meridian flip complete"),
    "Dithering failed": None,  # counted, not listed
}


def _duration(seconds: float) -> str:
    minutes = max(1, int(round(seconds / 60)))
    if minutes >= 60:
        return _("{hours} h {minutes} min").format(
            hours=minutes // 60, minutes=f"{minutes % 60:02d}"
        )
    return _("{minutes} min").format(minutes=minutes)


def _lights(frames: Iterable[tuple[str, float]]) -> tuple[str, float]:
    counts = Counter(frames)
    total = sum(n * exp for (_, exp), n in counts.items())
    parts = [f"{f} {n}×{exp:g}s" for (f, exp), n in sorted(counts.items())]
    return ", ".join(parts), total


class DigestRule(SessionRule):
    SETTINGS = ("enabled", "interval", "align", "min_session")

    def __init__(
        self,
        *,
        enabled: bool = True,
        interval: float = INTERVAL,
        align_to_clock: bool = True,
        min_session: float = MIN_SESSION,
    ) -> None:
        super().__init__()
        self.enabled = enabled and interval > 0
        self.interval = interval
        self.align = align_to_clock
        self.min_session = min_session
        self._started: float | None = None
        self._next: float | None = None
        self._open: dict[str, str] = {}  # problem rule -> title, until cleared
        self._tonight: list[tuple[str, float]] = []
        self._last_light: float | None = None
        self._reset_interval(None)
        self._target: str | None = None
        self._filter: str | None = None
        self._job: tuple[str, float, int, int] | None = None  # filter, exposure, done, planned
        self._capture_target: str | None = None  # Ekos Capture's targetName (manual sessions)
        self._camera: str | None = None  # the imaging camera, from Ekos Capture
        self._metrics: dict[tuple[str, str], float] = {}  # (metric, device) -> latest value
        self._secondary: set[str] = set()  # devices whose readings are secondary (sources.py)
        self.outlook: Callable[[float], str] = lambda now: ""  # "Flip in … · Dawn in …" (sky)

    def rescheduled(self, now: float) -> None:
        """The interval settings changed (a config reload): the next digest from the new ones."""
        if self._next is not None:
            self._next = self._boundary_after(now)

    # -- session -----------------------------------------------------------------------------

    def resume(
        self,
        session_id: str | None,
        alerts: Iterable[Event] = (),
        started_at: float | None = None,
    ) -> None:
        if session_id != self.session:
            self._started = started_at  # a restart mid-session knows the real start
            self._next = None
            self._tonight = []
            self._last_light = None
            self._open = {}
            # Problems still open from before the restart; Ekos's snapshot on reconnect closes
            # the ones that aren't (capture running again, focus complete…).
            for a in alerts:
                rule = a.data.get("rule", "")
                if rule in CLEARS:
                    for problem in CLEARS[rule]:
                        self._open.pop(problem, None)
                elif rule in CLEARED_BY or rule in OPEN_UNTIL:
                    self._open[rule] = a.data.get("title", rule)
        super().resume(session_id, alerts)

    def on_session_closed(self) -> None:
        self._started = self._next = None

    # -- events ------------------------------------------------------------------------------

    def note_alerts(self, alerts: Iterable[Event]) -> None:
        """Alerts raised by the other rules, for "since last" and the verdict."""
        for a in alerts:
            rule = a.data.get("rule", "")
            if rule in NOT_LISTED or a.kind != "alert" or a.data.get("off"):
                continue  # turned off in messages: left out of the digest too
            self._since.append((a.ts, a.data.get("title", rule)))
            if rule in CLEARS:
                for problem in CLEARS[rule]:
                    self._open.pop(problem, None)
                    self._raised.pop(problem, None)
            elif a.data.get("default_priority", a.data.get("priority")) in ("emergency", "high"):
                self._raised[rule] = a.data.get("title", rule)
                if rule in CLEARED_BY or rule in OPEN_UNTIL:
                    self._open[rule] = a.data.get("title", rule)

    def on_event(self, event: Event) -> list[Event]:
        kind, d = event.kind, event.data
        if kind == "session.started":
            self._started = event.ts
            self._reset_interval(event.ts)
            self._next = self._boundary_after(event.ts)
        elif self._next is None and self.session:
            # Resumed after a restart: this interval's numbers start now, but the session's age
            # (for skipping a digest right after it started) comes from resume().
            if self._started is None:
                self._started = event.ts
            self._reset_interval(event.ts)
            self._next = self._boundary_after(event.ts)
        if (kind == "capture.state" and d.get("state") in CAPTURE_WORKING) or (
            kind == "capture.frame" and d.get("type") == "light"
        ):
            self._close("capture_aborted")
        elif kind == "scheduler.state" and d.get("state") == "running":
            self._close("scheduler_aborted")
        elif kind == "focus.state" and d.get("state") == "complete":
            self._close("autofocus_failed")
        if kind == "capture.frame" and d.get("type"):
            if d["type"] == "light":
                frame = (d.get("filter") or word("no filter"), float(d.get("exposure") or 0))
                self._frames.append(frame)
                self._tonight.append(frame)
                self._last_light = event.ts
                if isinstance(d.get("hfr"), (int, float)) and d["hfr"] > 0:
                    self._hfr.append(float(d["hfr"]))
                if self._job and (frame[0], frame[1]) == self._job[:2]:
                    f, exp, done, planned = self._job
                    self._job = (f, exp, done + 1, planned)
            else:
                self._calibration[d["type"]] += 1
            if d.get("filter"):
                self._filter = d["filter"]
        elif kind == "metric" and isinstance(d.get("value"), (int, float)):
            sources.note(self._secondary, d)
            self._metrics[(d["metric"], d.get("device") or "")] = float(d["value"])
            if d["metric"] == "power.voltage":
                low = self._volts_low
                self._volts_low = float(d["value"]) if low is None else min(low, float(d["value"]))
        elif kind == "indi.capabilities":
            available = {(m, dev) for m, devices in d.get("metrics", {}).items() for dev in devices}
            self._metrics = {k: v for k, v in self._metrics.items() if k in available}
        elif kind == "capture.camera" and d.get("camera"):
            self._camera = d["camera"]
        elif kind == "capture.target" and d.get("target"):
            self._capture_target = d["target"]
        elif kind == "guide.rms" and d.get("ra") is not None and d.get("dec") is not None:
            self._rms.append((float(d["ra"]) ** 2 + float(d["dec"]) ** 2) ** 0.5)
        elif kind == "phd2.settle" and d.get("ok") is False:
            self._dither_fails += 1
        elif kind == "guide.state" and d.get("state") == "dithering_error":
            self._dither_fails += 1
        elif kind == "ekos.log":
            text = str(d.get("text", ""))
            if self._camera is None and d.get("module") == "Capture" and (m := CAMERA.match(text)):
                self._camera = m.group(1)  # "[CCD Simulator] Capturing…": Capture's camera
            if d.get("module") == "Scheduler" and (m := JOB.search(text)):
                self._target = m.group(1)
            elif m := JOB_PLAN.search(text):
                self._filter = m.group(2)
                exposure = localized(m.group(1), d)
                self._job = (
                    None
                    if exposure is None
                    else (m.group(2), exposure, int(m.group(3)), int(m.group(4)))
                )
            for needle, label in MILESTONES.items():
                if needle in text and label:
                    self._since.append((event.ts, _(label)))
        return []

    def on_tick(self, now: float) -> list[Event]:
        if self._next is None and self.session:  # resumed, and no event yet: schedule from now
            if self._started is None:
                self._started = now
            self._reset_interval(now)
            self._next = self._boundary_after(now)
        if not self.enabled or self._next is None or now < self._next:
            return []
        due, self._next = self._next, self._boundary_after(self._next)
        if self._started is not None and due - self._started < self.min_session:
            return []  # the session just started: the next one will say more
        digest = self._digest(due, now)
        self._reset_interval(due)
        return [digest]

    # -- helpers -------------------------------------------------------------------------------

    def _metric(self, metric: str, device: str | None = None) -> float | None:
        if device is not None:
            return self._metrics.get((metric, device))
        by_device = {dev: v for (m, dev), v in sorted(self._metrics.items()) if m == metric}
        values = list(sources.prefer_direct(by_device, self._secondary).values())
        return values[0] if values else None

    def _conditions(self) -> str:
        """Camera temperature, ambient, humidity, dew margin and power, from INDI."""
        parts = []
        cameras = [dev for (m, dev) in self._metrics if m == "camera.temperature"]
        camera = (
            self._camera if self._camera in cameras else (cameras[0] if len(cameras) == 1 else None)
        )
        if camera is not None:
            temperature = self._metric("camera.temperature", camera)
            parts.append(_("Camera {celsius} \u00b0C").format(celsius=f"{temperature:.1f}"))
        ambient = self._metric("env.temperature")
        humidity = self._metric("env.humidity")
        dew = self._metric("env.dewpoint")
        if ambient is not None:
            parts.append(_("Ambient {celsius} \u00b0C").format(celsius=f"{ambient:.1f}"))
        if humidity is not None:
            parts.append(_("RH {percent}%").format(percent=f"{humidity:.0f}"))
        if ambient is not None and dew is not None:
            parts.append(_("Dew margin {celsius} \u00b0C").format(celsius=f"{ambient - dew:.1f}"))
        volts = self._metric("power.voltage")
        if volts is not None:
            power = _("Power {volts} V").format(volts=f"{volts:.1f}")
            if self._volts_low is not None and self._volts_low <= volts - 0.1:
                power += " " + _("(low {volts} V)").format(volts=f"{self._volts_low:.1f}")
            amps = self._metric("power.current")
            parts.append(power + (f", {amps:.1f} A" if amps is not None else ""))
        return " \u00b7 ".join(parts) + "." if parts else ""

    def _close(self, rule: str) -> None:
        """A problem without an all-clear alert is over: the rig is working again."""
        self._open.pop(rule, None)

    def _reset_interval(self, ts: float | None) -> None:
        self._since_ts = ts
        self._frames: list[tuple[str, float]] = []
        self._calibration: Counter[str] = Counter()
        self._rms: list[float] = []
        self._hfr: list[float] = []
        self._dither_fails = 0
        self._since: list[tuple[float, str]] = []
        self._raised: dict[str, str] = {}
        self._volts_low: float | None = None  # lowest battery voltage this interval

    def _boundary_after(self, ts: float) -> float:
        if not self.align:
            return ts + self.interval
        local = datetime.fromtimestamp(ts)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        step = timedelta(seconds=self.interval)
        boundary = midnight
        while boundary.timestamp() <= ts:
            boundary += step
        return boundary.timestamp()

    def _digest(self, due: float, now: float) -> Event:
        problems = list(dict.fromkeys([*self._raised.values(), *self._open.values()]))
        if problems:
            verdict = _("Attention: {problems}").format(problems=", ".join(problems))
        elif self._frames:
            verdict = _("All's well")
        elif self._last_light is not None:  # seen live: "All's well" with capture long stopped
            verdict = _("No light frames since {time}").format(time=hhmm(self._last_light))
        else:
            verdict = _("No light frames yet")
        title = f"{hhmm(due)} — {verdict}"
        lines = []
        where = [p for p in (self._target or self._capture_target, self._filter) if p]
        if self._job:
            f, exp, done, planned = self._job
            where.append(_("job {done}/{planned}").format(done=min(done, planned), planned=planned))
        if where:
            lines.append(" · ".join(where))
        if self._frames:
            parts, total = _lights(self._frames)
            n = len(self._frames)
            lights = ngettext(
                "{n} light ({frames}), {duration}", "{n} lights ({frames}), {duration}", n
            ).format(n=n, frames=parts, duration=_duration(total))
        else:
            lights = _("no light frames")
        if self._since_ts:
            lines.append(
                _("Last {duration}: {lights}.").format(
                    duration=_duration(due - self._since_ts), lights=lights
                )
            )
        else:
            lines.append(_("This interval: {lights}.").format(lights=lights))
        if self._calibration:
            lines.append(
                _("Calibration: {frames}.").format(
                    frames=", ".join(f"{n} {word(t)}" for t, n in sorted(self._calibration.items()))
                )
            )
        if len(self._tonight) > len(self._frames):  # the first digest would just repeat itself
            _parts, total = _lights(self._tonight)
            n = len(self._tonight)
            lines.append(
                ngettext(
                    "Tonight: {n} light, {duration}.", "Tonight: {n} lights, {duration}.", n
                ).format(n=n, duration=_duration(total))
            )
        guiding = []
        if self._rms:
            guiding.append(
                _("Guide RMS {arcsec}″ (median)").format(
                    arcsec=f"{statistics.median(self._rms):.2f}"
                )
            )
        if self._dither_fails:
            n = self._dither_fails
            guiding.append(ngettext("{n} dither failed", "{n} dithers failed", n).format(n=n))
        if self._hfr:
            guiding.append(
                _("HFR {hfr} (median)").format(hfr=f"{statistics.median(self._hfr):.2f}")
            )
        if guiding:
            lines.append(", ".join(guiding) + ".")
        conditions = self._conditions()
        if conditions:
            lines.append(conditions)
        outlook = self.outlook(due)
        if outlook:
            lines.append(outlook)
        if self._since:
            events = "; ".join(f"{hhmm(t)} {what}" for t, what in sorted(self._since))
            lines.append(_("Since last: {events}.").format(events=events))
        still = [t for r, t in self._open.items() if r not in self._raised]
        if still:
            lines.append(_("Still open: {problems}.").format(problems=", ".join(still)))
        priority = "normal" if problems else "quiet"
        return make_alert(
            now, self.session, "digest", priority, title, "\n".join(lines), immediate=True
        )
