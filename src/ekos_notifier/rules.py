"""Alert rules: events in, ``alert`` events out (docs/design.md, "Alerts").

Like the session manager, ``Rules`` is pure logic over session-tagged events and the clock, so
replayed nights test it. Alerts are stored as events (kind ``alert``, source ``rules``) and the
notifier sends them from the store.

Alert data: ``rule``, ``priority`` (emergency | high | normal | quiet), ``title``, ``body`` and
``immediate`` (a Normal alert sent at once rather than in the hourly digest).

Rules so far:

- **Starting session** (Normal, immediate) when a session starts, with each rule's pre-flight
  checks (``SessionRule.preflight``): warnings first, then the readings that passed.
- **Session summary** (Normal, immediate) when a session that imaged closes: as soon as the
  Scheduler stops or a manual sequence completes, parked or not, with the night's numbers
  (``ekos_notifier.stats``) and the mount's last known state in the message.
- **Guiding lost** (High) and **Guiding recovered**, **PHD2 warnings** (High, PHD2's own
  warnings and errors, once per message per session), and **Guiding poor** (High, following
  Ekos's own guide-deviation limit): see ``ekos_notifier.guiding``.
- **Capture aborted**, **Scheduler aborted** and **Capture stalled** (High): see
  ``ekos_notifier.capture``.
- **Autofocus failed** (High, two failed runs in a row): see ``ekos_notifier.focus``.
- **Plate solve failed** (High): see ``ekos_notifier.align``.
- **Clouds** (High) and **Clouds passed**: see ``ekos_notifier.clouds``.
- **Twilight coming**, **Target low** (Normal), **Meridian flip soon** (Quiet), and **Meridian
  flip failed** and **Meridian flip overdue** (Emergency): see ``ekos_notifier.sky``.
- **Disk low** (High): see ``ekos_notifier.disk``.
- **Battery critical** (Emergency, also outside a session), **Battery low**, **Power output
  off** and **No current on an output** (High): see ``ekos_notifier.power``.
- **Dew risk**, **Cooler can't hold** and **Cooler can't reach setpoint** (High): see
  ``ekos_notifier.conditions``.
- **Flats reminder** (Normal, with the summary) and **Darks reminder** (off by default): see
  ``ekos_notifier.calibration``.
- **Hourly digest** (Quiet, or Normal with "Attention"), which also sees every other rule's
  alerts: see ``ekos_notifier.digest``.
- **Ekos stopped with the mount unparked** (Emergency): Ekos or INDI stops, or KStars vanishes,
  while the last known mount state was unparked, and Ekos isn't back within ``unparked_grace``
  seconds. Uses the last known state on purpose: the mount's properties vanish with Ekos.
  A later "parked" sends an all-clear.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from typing import Any

from ekos_notifier.alerts import SessionRule
from ekos_notifier.align import PlateSolveRule
from ekos_notifier.calibration import CalibrationRule
from ekos_notifier.capture import CaptureRule
from ekos_notifier.clouds import CloudsRule
from ekos_notifier.conditions import CoolerRule, DewRule
from ekos_notifier.digest import DigestRule
from ekos_notifier.disk import DiskRule
from ekos_notifier.events import Event
from ekos_notifier.focus import AutofocusRule
from ekos_notifier.guiding import RECOVERY, GuidingRule, PoorGuidingRule
from ekos_notifier.i18n import N_, _, word
from ekos_notifier.messages import MessageSettings
from ekos_notifier.power import PowerRule
from ekos_notifier.sky import SkyRule
from ekos_notifier.stats import SessionStats

UNPARKED_GRACE = 120.0
MOVING = {"tracking", "slewing", "moving"}

START_REASONS = {
    "scheduler": N_("the Ekos Scheduler started"),
    "light_frame": N_("the first light frame was saved"),
    "user": N_("you pressed Start"),
}
# Session ends that send no summary: you stopped Ekos yourself (seen live: a summary 10 min after
# your own Stop was noise). A crash ("ekos_lost") still gets one.
QUIET_ENDS = {"ekos_stopped"}
END_REASONS = {
    "scheduler_finished": N_("the Scheduler finished"),
    "scheduler_aborted": N_("the Scheduler stopped (aborted)"),
    "sequence_complete": N_("the capture sequence completed"),
    "ekos_stopped": N_("you stopped Ekos"),
    "ekos_lost": N_("Ekos stopped unexpectedly (a crash, or KStars closed)"),
    "night_ended": N_("the night ended"),
    "user": N_("you pressed End"),
}


def _reason(table: dict[str, str], reason: object) -> str:
    return _(table[reason]) if reason in table else str(reason)


def _clock(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def _duration(seconds: float) -> str:
    minutes = int(round(seconds / 60))
    if minutes >= 60:
        return _("{hours} h {minutes} min").format(
            hours=minutes // 60, minutes=f"{minutes % 60:02d}"
        )
    return _("{minutes} min").format(minutes=minutes)


class Rules:
    def __init__(
        self,
        *,
        unparked_grace: float = UNPARKED_GRACE,
        guiding_recovery: float = RECOVERY,
        guiding_poor_window: float = 600.0,
        deviation_limit: Callable[[], float | None] = lambda: None,
        digest: DigestRule | None = None,
        sky: SkyRule | None = None,
        power: PowerRule | None = None,
        dew: DewRule | None = None,
        cooler: CoolerRule | None = None,
        disk: DiskRule | None = None,
        clouds: CloudsRule | None = None,
        messages: MessageSettings | None = None,
    ) -> None:
        self.unparked_grace = unparked_grace
        self.messages = messages or MessageSettings()
        self.guiding = GuidingRule(guiding_recovery)
        self.digest = digest or DigestRule()
        self.sky = sky or SkyRule()
        self.digest.outlook = self.sky.outlook
        self.session_rules: list[SessionRule] = [
            self.sky,
            CaptureRule(),
            AutofocusRule(),
            PlateSolveRule(),
            clouds or CloudsRule(),
            CalibrationRule(),
            PoorGuidingRule(guiding_poor_window, deviation_limit),
            power or PowerRule(),
            dew or DewRule(),
            cooler or CoolerRule(),
            disk or DiskRule(free=lambda path: None),
        ]
        self._park: str | None = None
        self._mount: str | None = None
        self._gone_at: float | None = None
        self._gone_state: str | None = None
        self._gone_session: str | None = None
        self._unparked_alert_sent = False
        self._started: dict[str, float] = {}
        self._lights: dict[str, dict[tuple[str, float], int]] = defaultdict(
            lambda: defaultdict(int)
        )
        self._calibration: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._stats: dict[str, SessionStats] = defaultdict(SessionStats)

    # -- settings ----------------------------------------------------------------------------

    def reconfigure(self, new: Rules, now: float) -> None:
        """Take the settings of ``new`` (built from a reloaded config), keeping this night's
        state: open alerts, baselines, the digest's counts."""
        schedule = (self.digest.interval, self.digest.align)
        self.messages = new.messages
        self.unparked_grace = new.unparked_grace
        self.guiding.recovery = new.guiding.recovery
        pairs = [
            (self.digest, new.digest),
            *zip(self.session_rules, new.session_rules, strict=True),
        ]
        for old, fresh in pairs:
            if type(old) is not type(fresh):
                raise TypeError(f"rules differ: {type(old).__name__}, {type(fresh).__name__}")
            for name in old.SETTINGS:
                setattr(old, name, getattr(fresh, name))
        if (self.digest.interval, self.digest.align) != schedule:
            self.digest.rescheduled(now)

    # -- events ------------------------------------------------------------------------------

    def resume(
        self,
        session_id: str,
        alerts: Iterable[Event] = (),
        started_at: float | None = None,
        frames: Iterable[Any] = (),
    ) -> None:
        """The service restarted during an active session; ``alerts`` are the ones it already
        raised, so they aren't raised again; ``started_at`` is when the session started and
        ``frames`` (rows with type, filter, exposure) the frames it has, for the summary."""
        if started_at is not None:
            self._started[session_id] = started_at
        for f in frames:
            if f["type"] == "light":
                key = (f["filter"] or "no filter", f["exposure"] or 0.0)
                self._lights[session_id][key] += 1
            elif f["type"]:
                self._calibration[session_id][f["type"]] += 1
        self._stats[session_id].frames(frames)
        self._stats[session_id].alerts(alerts)
        sent = [
            a.data.get("message") or a.data.get("body", "").split("\n")[0]
            for a in alerts
            if a.data.get("rule") == "phd2_warning"
        ]
        self.guiding.resume(session_id, sent)
        for rule in self.session_rules:
            rule.resume(session_id, alerts)
        self.digest.resume(session_id, alerts, started_at)

    def handle(self, event: Event) -> list[Event]:
        out = self.messages.apply(self._handle(event))
        self.digest.note_alerts(out)
        self._note_alerts(out)
        return out + self.messages.apply(self.digest.handle(event))

    def tick(self, now: float) -> list[Event]:
        out = self.messages.apply(self._tick(now))
        self.digest.note_alerts(out)
        self._note_alerts(out)
        return out + self.messages.apply(self.digest.tick(now))

    def _handle(self, event: Event) -> list[Event]:
        out = self._tick(event.ts)
        kind, d, sid = event.kind, event.data, event.session_id
        state = d.get("state")
        if sid:
            self._stats[sid].note(event)

        if kind == "mount.park":
            self._park = state
            if state == "parked" and self._unparked_alert_sent:
                self._unparked_alert_sent = False
                out.append(
                    self._alert(
                        event.ts,
                        sid,
                        "mount_unparked_cleared",
                        "normal",
                        _("Mount parked"),
                        _("The mount is parked again. All clear."),
                        # At once: its Emergency comes after Ekos stops, when there's usually
                        # no session, so no hourly digest to carry it.
                        immediate=True,
                    )
                )
        elif kind == "mount.state":
            self._mount = state
        elif self._ekos_gone(kind, state):
            if self._gone_at is None and self._unparked():
                self._gone_at, self._gone_session = event.ts, sid
                self._gone_state = self._park if self._park == "unparked" else self._mount
        elif kind in ("ekos.state", "indi.state") and state == "success":
            self._gone_at = None
        elif kind == "capture.frame" and sid:
            if d.get("type") == "light":
                self._lights[sid][
                    (d.get("filter") or word("no filter"), d.get("exposure") or 0.0)
                ] += 1
            elif d.get("type"):
                self._calibration[sid][d["type"]] += 1
        elif kind == "session.started":
            self._started[sid] = event.ts
            out.append(
                self._alert(
                    event.ts,
                    sid,
                    "session_started",
                    "normal",
                    _("Starting session"),
                    _("Night of {night}: session started at {time} because {reason}.").format(
                        night=d["night"],
                        time=_clock(event.ts),
                        reason=_reason(START_REASONS, d.get("reason")),
                    )
                    + self._preflight(event.ts),
                    immediate=True,
                )
            )
        elif kind == "session.closed" and d.get("state") != "closed":
            self._stats.pop(sid, None)  # abandoned: nothing to sum up
        elif kind == "session.closed" and d.get("state") == "closed":
            if d.get("reason") in QUIET_ENDS:
                # You stopped Ekos yourself: you're at the rig and know. Forget the counts.
                self._started.pop(sid, None)
                self._lights.pop(sid, None)
                self._calibration.pop(sid, None)
                self._stats.pop(sid, None)
            else:
                out.append(self._summary(event, sid))
        # The other rules after this one, so "Starting session" comes before what it leads to
        # (seen live: "Twilight coming" arrived just before "Starting session").
        out += self.guiding.handle(event)
        for rule in self.session_rules:
            out += rule.handle(event)
        return out

    def _tick(self, now: float) -> list[Event]:
        out = self.guiding.tick(now)
        for rule in self.session_rules:
            out += rule.tick(now)
        return out + self._unparked_tick(now)

    def _unparked_tick(self, now: float) -> list[Event]:
        if (
            self._gone_at is not None
            and not self._unparked_alert_sent
            and now - self._gone_at >= self.unparked_grace
        ):
            self._unparked_alert_sent = True
            gone_at, self._gone_at = self._gone_at, None
            return [
                self._alert(
                    now,
                    self._gone_session,
                    "ekos_stopped_mount_unparked",
                    "emergency",
                    _("Mount left unparked"),
                    _(
                        "Ekos stopped at {time} while the mount was {state}, and it hasn't come"
                        " back. Nothing is watching the mount now.\n"
                        "Park the mount or check the rig."
                    ).format(time=_clock(gone_at), state=word(self._gone_state or "unparked")),
                )
            ]
        return []

    # -- helpers -------------------------------------------------------------------------------

    def _note_alerts(self, alerts: list[Event]) -> None:
        for a in alerts:
            if a.kind == "alert" and a.session_id in self._stats:
                self._stats[a.session_id].alerts([a])

    def _preflight(self, now: float) -> str:
        """The pre-flight checks for "Starting session": what each rule can tell of the rig
        right now. Warnings first; they never stop the session."""
        readings: list[str] = []
        warnings: list[str] = []
        for rule in self.session_rules:
            for ok, text in rule.preflight(now):
                (readings if ok else warnings).append(text)
        out = ""
        if warnings:
            out += "\n" + _("Check: {warnings}.").format(warnings="; ".join(warnings))
        if readings:
            out += "\n" + _("Pre-flight: {readings}.").format(readings=", ".join(readings))
        return out

    @staticmethod
    def _ekos_gone(kind: str, state: Any) -> bool:
        return kind == "kstars.vanished" or (
            kind in ("ekos.state", "indi.state") and state in ("idle", "error")
        )

    def _unparked(self) -> bool:
        if self._park == "parked":
            return False
        return self._park == "unparked" or self._mount in MOVING

    def _mount_line(self) -> str | None:
        if self._park == "parked":
            return _("Mount: parked.")
        if self._unparked():
            return _("Mount: not parked (last seen {state}).").format(
                state=word(self._mount or "unparked")
            )
        return None

    def _summary(self, event: Event, sid: str | None) -> Event:
        d = event.data
        ended = d.get("ended_at", event.ts)
        lines = []
        started = self._started.pop(sid, None)
        reason = _reason(END_REASONS, d.get("reason"))
        if started is not None:
            lines.append(
                _("Session ended at {time} after {duration} because {reason}.").format(
                    time=_clock(ended), duration=_duration(ended - started), reason=reason
                )
            )
        else:
            lines.append(
                _("Session ended at {time} because {reason}.").format(
                    time=_clock(ended), reason=reason
                )
            )
        lights = self._lights.pop(sid, {})
        if lights:
            total = sum(n * exp for (_f, exp), n in lights.items())
            parts = [f"{f} {n}×{exp:g}s" for (f, exp), n in sorted(lights.items())]
            lines.append(
                _("Lights: {frames}. Integration {duration}.").format(
                    frames=", ".join(parts), duration=_duration(total)
                )
            )
        else:
            lines.append(_("No light frames."))
        calibration = self._calibration.pop(sid, {})
        if calibration:
            lines.append(
                _("Calibration: {frames}.").format(
                    frames=", ".join(f"{n} {word(t)}" for t, n in sorted(calibration.items()))
                )
            )
        lines += self._stats.pop(sid, SessionStats()).lines() if sid else []
        mount = self._mount_line()
        if mount:
            lines.append(mount)
        return self._alert(
            event.ts,
            sid,
            "session_summary",
            "normal",
            _("Session summary"),
            "\n".join(lines),
            immediate=True,
        )

    @staticmethod
    def _alert(
        ts: float,
        sid: str | None,
        rule: str,
        priority: str,
        title: str,
        body: str,
        immediate: bool = False,
    ) -> Event:
        return Event(
            ts,
            "alert",
            {
                "rule": rule,
                "priority": priority,
                "title": title,
                "body": body,
                "immediate": immediate,
            },
            "rules",
            sid,
        )
