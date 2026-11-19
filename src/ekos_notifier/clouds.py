"""The "Clouds" alert (High, docs/design.md "Alerts").

Clouds dim everything at once, so the rule looks for dimming, not for a lost star: a star lost
while it was still bright is the mount or guiding (seen live: a runaway walked the star off the
frame at full SNR), which "Guiding lost" covers. Two independent signs, during a session:

- **The guide star fades:** PHD2's SNR, median over the last ``RECENT`` seconds, falls by
  ``drop`` (a fraction) from its baseline, the median of the last ``BASELINE`` seconds of clear
  guiding. PHD2 losing the star after it faded keeps the sign up, and so does guiding stopping
  then, until a new star guides well; otherwise a new star starts a new baseline.
- **The frames lose stars:** a light frame's star count (when Ekos measures it, from
  ``captureComplete``) falls by ``drop`` from the median of the previous frames with the same
  filter and exposure on the same target.

**Clouds** is sent when the guide star has faded for ``SUSTAIN`` seconds, or two frames in a
row have lost stars, and names every sign it has (guide SNR, star counts, HFR, PHD2's star
losses). Once until **Clouds passed** (Normal, immediate): the guide star back over the
``CLEAR_AT`` fraction of its baseline for ``CLEAR_AFTER`` seconds and, if frames are measured,
the latest frame's stars back too. ``drop`` 0 turns it off.

The thresholds are first guesses, to be tuned against a real cloudy night: the simulators don't
cloud over (PHD2's simulator SNR holds at ~40; Ekos reports no star counts on them).
"""

from __future__ import annotations

import statistics
from collections import deque
from collections.abc import Iterable

from ekos_notifier.alerts import SessionRule, hhmm, make_alert, minutes
from ekos_notifier.events import Event
from ekos_notifier.i18n import _, ngettext

DROP = 0.5  # how far it falls: by half
RECENT = 60.0
BASELINE = 900.0
MIN_BASELINE = 20  # guide steps before a baseline counts
MIN_RECENT = 3
MIN_FRAMES = 3  # earlier frames before a star count counts
SUSTAIN = 120.0
CLEAR_AT = 0.8
CLEAR_AFTER = 300.0
NEW_STAR = {"stopped", "selected", "calibrating", "looping"}


def _median(values: Iterable[float]) -> float | None:
    values = list(values)
    return statistics.median(values) if values else None


class CloudsRule(SessionRule):
    SETTINGS = ("drop", "below")

    def __init__(self, drop: float = DROP, *, sustain: float = SUSTAIN) -> None:
        super().__init__()
        self.drop = drop
        self.below = 1.0 - min(drop, 1.0)  # under this fraction of typical
        self.sustain = sustain
        self._baseline: deque[tuple[float, float]] = deque()  # (ts, snr) of clear guiding
        self._recent: deque[tuple[float, float]] = deque()
        self._faded_since: float | None = None
        self._fade_base: float | None = None  # the baseline SNR when it faded
        self._clear_since: float | None = None
        self._phd2: str | None = None
        self._lost = 0  # PHD2 star losses while faded
        self._target: str | None = None
        self._frames: dict[tuple[str, float], list[tuple[int, float | None]]] = {}
        self._dropped: list[tuple[float, int, float, float | None, float | None]] = []
        self._last_frame_ok: bool | None = None
        self._raised = False

    def resume(self, session_id: str | None, alerts: Iterable[Event] = ()) -> None:
        super().resume(session_id, alerts)
        for a in alerts:
            if a.data.get("rule") == "clouds":
                self._raised = True
            elif a.data.get("rule") == "clouds_cleared":
                self._raised = False

    def on_session_closed(self) -> None:
        self._raised = False
        self._new_star()
        self._frames.clear()
        self._dropped.clear()
        self._last_frame_ok = None

    # -- events ------------------------------------------------------------------------------

    def on_event(self, event: Event) -> list[Event]:
        if self.drop <= 0:
            return []
        kind, d, ts = event.kind, event.data, event.ts
        if kind == "phd2.state":
            self._phd2 = d.get("state")
            if self._phd2 in NEW_STAR:
                # Guiding gave up on a faded star: still dim until a new star guides well.
                self._new_star(keep_fade=True)
        elif kind == "phd2.lost":
            self._phd2 = None
            self._new_star(keep_fade=True)
        elif kind == "phd2.step" and isinstance(d.get("snr"), (int, float)):
            self._step(ts, float(d["snr"]))
        elif kind == "phd2.star_lost" and self._faded_since is not None:
            self._lost += 1
        elif kind == "capture.target" and d.get("target"):
            self._new_target(str(d["target"]))
        elif kind == "ekos.log" and d.get("module") == "Scheduler":
            text = str(d.get("text", ""))
            if text.startswith("Job '") and "' " in text:
                self._new_target(text[5 : text.index("' ")])
        elif kind == "capture.frame" and d.get("type") == "light" and self.session:
            self._frame(ts, d)
        return []

    def on_tick(self, now: float) -> list[Event]:
        if self.drop <= 0:
            return []
        if not self._raised:
            faded = self._faded_since is not None and now - self._faded_since >= self.sustain
            frames = len(self._dropped) >= 2
            if faded or frames:
                self._raised = True
                self._clear_since = None
                return [self._alert(now)]
            return []
        guide_clear = self._clear_since is not None and now - self._clear_since >= CLEAR_AFTER
        if guide_clear and self._last_frame_ok is not False:
            self._raised = False
            self._clear_since = None
            return [self._cleared(now)]
        if self._clear_since is None and self._last_frame_ok and not self._baseline:
            # No guide star to judge by (Ekos's own guider): the frames' stars are back.
            self._raised = False
            return [self._cleared(now)]
        return []

    # -- guide star ----------------------------------------------------------------------------

    def _new_star(self, keep_fade: bool = False) -> None:
        self._baseline.clear()
        self._recent.clear()
        self._clear_since = None
        if not keep_fade:
            self._faded_since = self._fade_base = None
            self._lost = 0

    def _step(self, ts: float, snr: float) -> None:
        self._recent.append((ts, snr))
        while self._recent and ts - self._recent[0][0] > RECENT:
            self._recent.popleft()
        base = self._base()
        now = _median(s for _, s in self._recent) if len(self._recent) >= MIN_RECENT else None
        if base is None or now is None:
            self._baseline.append((ts, snr))
            if self._faded_since is not None and len(self._baseline) >= MIN_BASELINE:
                self._faded_since = self._fade_base = None  # a new star guides well
                self._lost = 0
        elif now < self.below * base:
            if self._faded_since is None:
                self._faded_since, self._fade_base = ts, base
            self._clear_since = None
            return  # faded steps never lower the baseline
        else:
            self._faded_since = self._fade_base = None
            self._lost = 0
            if now >= CLEAR_AT * base:
                if self._clear_since is None:
                    self._clear_since = ts
                self._baseline.append((ts, snr))
            else:
                self._clear_since = None
        while self._baseline and ts - self._baseline[0][0] > BASELINE:
            self._baseline.popleft()

    def _base(self) -> float | None:
        if len(self._baseline) < MIN_BASELINE:
            return None
        return _median(s for _, s in self._baseline)

    # -- frames --------------------------------------------------------------------------------

    def _new_target(self, target: str) -> None:
        if target != self._target:
            self._target = target
            self._frames.clear()  # another field has other stars
            self._dropped.clear()

    def _frame(self, ts: float, d: dict) -> None:
        stars = d.get("star_count")
        if not isinstance(stars, int) or isinstance(stars, bool):
            return  # Ekos didn't measure it
        hfr = d.get("hfr") if isinstance(d.get("hfr"), (int, float)) else None
        key = (d.get("filter") or "", float(d.get("exposure") or 0))
        history = self._frames.setdefault(key, [])
        typical = _median(n for n, _ in history[-10:]) if len(history) >= MIN_FRAMES else None
        if typical is not None and stars < self.below * typical:
            hfrs = [h for _, h in history[-10:] if h is not None]
            self._dropped.append((ts, stars, typical, hfr, _median(hfrs)))
            self._last_frame_ok = False
            return  # a clouded frame isn't part of what's typical
        history.append((stars, hfr))
        if typical is not None:
            self._dropped.clear()
            self._last_frame_ok = stars >= CLEAR_AT * typical
        if len(history) > 20:
            del history[:-20]

    # -- messages ------------------------------------------------------------------------------

    def _alert(self, now: float) -> Event:
        lines = []
        since = [t for t in (self._faded_since, *(f[0] for f in self._dropped[:1])) if t]
        start = min(since) if since else now
        base = self._fade_base
        if self._faded_since is not None and base is not None:
            snr = _median(s for _, s in self._recent)
            if snr is not None:
                faded = _(
                    "The guide star has faded since {time}: SNR now {snr}, {before} before."
                ).format(time=hhmm(self._faded_since), snr=f"{snr:.0f}", before=f"{base:.0f}")
            else:
                faded = _(
                    "The guide star has faded since {time}: SNR now lost, {before} before."
                ).format(time=hhmm(self._faded_since), before=f"{base:.0f}")
            lines.append(faded)
            if self._lost:
                lines.append(
                    ngettext(
                        "PHD2 lost the star {n} time.", "PHD2 lost the star {n} times.", self._lost
                    ).format(n=self._lost)
                )
        if self._dropped:
            ts, stars, typical, hfr, typical_hfr = self._dropped[-1]
            text = _("Last frame ({time}): {stars} stars, typically {typical}").format(
                time=hhmm(ts), stars=stars, typical=f"{typical:.0f}"
            )
            if hfr is not None and typical_hfr is not None:
                text += "; " + _("HFR {hfr}, typically {typical}").format(
                    hfr=f"{hfr:.2f}", typical=f"{typical_hfr:.2f}"
                )
            n = len(self._dropped)
            if n > 1:
                text += " " + _("({n} frames in a row)").format(n=n)
            lines.append(text + ".")
        lines.append(
            _("Clouds or dew? Dimming for {duration}. Frames now may be unusable.").format(
                duration=minutes(now - start)
            )
        )
        return make_alert(now, self.session, "clouds", "high", _("Clouds"), "\n".join(lines))

    def _cleared(self, now: float) -> Event:
        if self._baseline:
            body = _("The guide star is bright again ({time}).").format(time=hhmm(now))
        else:
            body = _("The frames' stars are back ({time}).").format(time=hhmm(now))
        return make_alert(
            now,
            self.session,
            "clouds_cleared",
            "normal",
            _("Clouds passed"),
            body,
            immediate=True,
        )
