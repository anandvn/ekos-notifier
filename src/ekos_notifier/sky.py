"""Alerts from the sky's clock (docs/design.md, "Alerts", Normal — manual steps, and Quiet):

- **Twilight coming** (Normal, immediate): ``twilight_warning`` before astronomical dawn (the Sun
  rising through −18°), once per session.
- **Target low** (Normal, immediate): ``target_low_warning`` before the target the mount is
  tracking sinks below the altitude limit: Ekos's mount limit if enabled, else the Scheduler's
  altitude constraint, else ``DEFAULT_MIN_ALTITUDE``. Once per target.
- **Meridian flip soon** (Quiet, held for the digest): ``FLIP_NOTICE`` before Ekos flips (only if
  Ekos is set to flip), at its flip hour angle. Once per target.

The site is KStars's location (``site.location`` from the Ekos adapter), or the mount's
``GEOGRAPHIC_COORD`` from INDI when KStars's isn't known; the target is the mount's pointing
(``mount.coords``, INDI). ``outlook(now)`` gives the digest its "Flip in … · Dawn in …" line.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from ekos_notifier import ephemeris
from ekos_notifier.alerts import SessionRule, hhmm, make_alert
from ekos_notifier.ekos.settings import ObservingLimits
from ekos_notifier.events import Event

DEFAULT_MIN_ALTITUDE = 10.0
FLIP_NOTICE = 900.0
RECOMPUTE = 300.0  # seconds between recomputing a target's setting and flip times
SAME_TARGET = 0.5  # degrees: a pointing change smaller than this is the same target


def _until(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    return f"{minutes // 60} h {minutes % 60:02d} min" if minutes >= 60 else f"{minutes} min"


class SkyRule(SessionRule):
    def __init__(
        self,
        *,
        twilight_warning: float = 2700.0,
        target_low_warning: float = 1800.0,
        limits: Callable[[], ObservingLimits | None] = lambda: None,
    ) -> None:
        super().__init__()
        self.twilight_warning = twilight_warning
        self.target_low_warning = target_low_warning
        self._limits_source = limits
        self.limits = ObservingLimits()
        self._site: tuple[float, float] | None = None
        self._site_from_kstars = False
        self._coords: tuple[float, float] | None = None
        self._target_key: tuple[float, float] | None = None
        self._target_name: str | None = None
        self._tracking = False
        self._dawn: float | None = None
        self._dawn_warned: float | None = None
        self._sets: float | None = None
        self._flip: float | None = None
        self._computed_at: float | None = None
        self._warned: set[str] = set()  # "low", "flip" for the current target

    # -- session -----------------------------------------------------------------------------

    def resume(self, session_id: str | None, alerts: Iterable[Event] = ()) -> None:
        if session_id != self.session:
            try:
                self.limits = self._limits_source() or ObservingLimits()
            except Exception:  # noqa: BLE001 - a settings quirk must not stop the rules
                self.limits = ObservingLimits()
            self._dawn = None
        super().resume(session_id, alerts)

    @property
    def min_altitude(self) -> float:
        return self.limits.min_altitude if self.limits.min_altitude else DEFAULT_MIN_ALTITUDE

    # -- events ------------------------------------------------------------------------------

    def on_event(self, event: Event) -> list[Event]:
        kind, d = event.kind, event.data
        if kind == "site.location" and "latitude" in d and "longitude" in d:
            from_kstars = d.get("via") == "kstars"
            if from_kstars or not self._site_from_kstars:
                site = (float(d["latitude"]), float(d["longitude"]))
                if site != self._site:
                    self._site, self._dawn, self._computed_at = site, None, None
                self._site_from_kstars = self._site_from_kstars or from_kstars
        elif kind == "mount.coords" and "ra" in d and "dec" in d:
            self._coords = (float(d["ra"]), float(d["dec"]))
            key = self._coords
            if self._target_key is None or (
                abs(key[1] - self._target_key[1]) > SAME_TARGET
                or abs((key[0] - self._target_key[0] + 12) % 24 - 12) * 15 > SAME_TARGET
            ):
                self._target_key, self._warned, self._computed_at = key, set(), None
        elif kind == "mount.state":
            self._tracking = d.get("state") == "tracking"
        elif kind == "capture.target" and d.get("target"):
            self._target_name = d["target"]
        elif kind == "ekos.log" and d.get("module") == "Scheduler":
            text = str(d.get("text", ""))
            if text.startswith("Job '") and "'" in text[5:]:
                self._target_name = text[5 : text.index("'", 5)]
        return []

    def on_tick(self, now: float) -> list[Event]:
        if self._site is None:
            return []
        self._compute(now)
        out: list[Event] = []
        if (
            self._dawn is not None
            and self._dawn_warned != self._dawn
            and 0 < self._dawn - now <= self.twilight_warning
        ):
            self._dawn_warned = self._dawn
            out.append(self._twilight(now))
        if self._tracking and self._coords is not None:
            if (
                "low" not in self._warned
                and self._sets is not None
                and 0 < self._sets - now <= self.target_low_warning
            ):
                self._warned.add("low")
                out.append(self._low(now))
            if (
                "flip" not in self._warned
                and self._flip is not None
                and 0 < self._flip - now <= FLIP_NOTICE
            ):
                self._warned.add("flip")
                out.append(self._flip_soon(now))
        return out

    def outlook(self, now: float) -> str:
        """The digest's line: time to the meridian flip and to dawn, when known."""
        if self._site is None:
            return ""
        self._compute(now)
        parts = []
        if self._tracking and self._flip is not None and self._flip > now:
            parts.append(f"Flip in {_until(self._flip - now)}")
        if self._tracking and self._sets is not None and self._sets > now:
            what = self._target_name or "Target"
            parts.append(f"{what} below {self.min_altitude:g}° in {_until(self._sets - now)}")
        if self._dawn is not None and self._dawn > now:
            parts.append(f"Dawn in {_until(self._dawn - now)}")
        return " · ".join(parts) + "." if parts else ""

    # -- helpers -------------------------------------------------------------------------------

    def _compute(self, now: float) -> None:
        assert self._site is not None
        lat, lon = self._site
        if self._dawn is None or self._dawn < now - 3600:
            self._dawn = ephemeris.astronomical_dawn(now, lat, lon)
        if self._coords is None:
            self._sets = self._flip = None
            return
        if self._computed_at is not None and now - self._computed_at < RECOMPUTE:
            return
        self._computed_at = now
        ra, dec = self._coords
        self._sets = ephemeris.sets_below(now, lat, lon, ra, dec, self.min_altitude)
        self._flip = (
            ephemeris.meridian_flip(now, lon, ra, self.limits.flip_offset_degrees)
            if self.limits.flip_enabled
            else None
        )

    def _target(self) -> str:
        return self._target_name or "The target"

    def _twilight(self, now: float) -> Event:
        assert self._dawn is not None
        body = (
            f"Astronomical dawn at {hhmm(self._dawn)}, in {_until(self._dawn - now)}.\n"
            "Plan the last frames, flats and parking."
        )
        return make_alert(
            now, self.session, "twilight_coming", "normal", "Twilight coming", body, immediate=True
        )

    def _low(self, now: float) -> Event:
        assert self._sets is not None and self._site is not None and self._coords is not None
        alt = ephemeris.altitude(now, *self._site, *self._coords)
        body = (
            f"{self._target()} sinks below {self.min_altitude:g}° at {hhmm(self._sets)}, "
            f"in {_until(self._sets - now)} (now {alt:.0f}°).\n"
            "Plan the next target, or flats and parking."
        )
        return make_alert(
            now, self.session, "target_low", "normal", "Target low", body, immediate=True
        )

    def _flip_soon(self, now: float) -> Event:
        assert self._flip is not None
        body = f"Ekos should flip the mount at {hhmm(self._flip)}, in {_until(self._flip - now)}."
        return make_alert(
            now, self.session, "meridian_flip_soon", "quiet", "Meridian flip soon", body
        )
