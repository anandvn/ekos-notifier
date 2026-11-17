"""Just enough astronomy for the night's timing: sidereal time, the Sun's position, altitudes, and
when an altitude is crossed. Pure Python (Meeus's low-precision formulas); good to about a
minute, which is all an alert "30 minutes before" needs.

Angles in degrees, right ascension in hours, times as Unix seconds (UTC). Longitude is east
positive (KStars and INDI's ``GEOGRAPHIC_COORD`` use 0–360 east; both work).
"""

from __future__ import annotations

import math
from collections.abc import Callable

J2000 = 2451545.0
SIDEREAL = 1.00273790935  # sidereal seconds per solar second
ASTRONOMICAL = -18.0  # the Sun's altitude at astronomical dawn and dusk


def julian_day(ts: float) -> float:
    return ts / 86400.0 + 2440587.5


def gmst_hours(ts: float) -> float:
    """Greenwich mean sidereal time."""
    d = julian_day(ts) - J2000
    t = d / 36525.0
    gmst = 280.46061837 + 360.98564736629 * d + 0.000387933 * t * t - t**3 / 38710000.0
    return (gmst % 360.0) / 15.0


def lst_hours(ts: float, longitude: float) -> float:
    return (gmst_hours(ts) + longitude / 15.0) % 24.0


def hour_angle(ts: float, longitude: float, ra_hours: float) -> float:
    """Hour angle in hours, -12..12 (negative: east of the meridian, still rising)."""
    return (lst_hours(ts, longitude) - ra_hours + 12.0) % 24.0 - 12.0


def altitude(ts: float, latitude: float, longitude: float, ra_hours: float, dec: float) -> float:
    ha = math.radians(hour_angle(ts, longitude, ra_hours) * 15.0)
    lat, d = math.radians(latitude), math.radians(dec)
    s = math.sin(lat) * math.sin(d) + math.cos(lat) * math.cos(d) * math.cos(ha)
    return math.degrees(math.asin(max(-1.0, min(1.0, s))))


def sun_radec(ts: float) -> tuple[float, float]:
    """The Sun's apparent RA (hours) and Dec (degrees), Meeus chapter 25 (low precision)."""
    t = (julian_day(ts) - J2000) / 36525.0
    l0 = 280.46646 + 36000.76983 * t + 0.0003032 * t * t
    m = math.radians(357.52911 + 35999.05029 * t - 0.0001537 * t * t)
    c = (
        (1.914602 - 0.004817 * t - 0.000014 * t * t) * math.sin(m)
        + (0.019993 - 0.000101 * t) * math.sin(2 * m)
        + 0.000289 * math.sin(3 * m)
    )
    omega = math.radians(125.04 - 1934.136 * t)
    lam = math.radians(l0 + c - 0.00569 - 0.00478 * math.sin(omega))
    eps = math.radians(
        23.0 + (26.0 + (21.448 - 46.815 * t) / 60.0) / 60.0 + 0.00256 * math.cos(omega)
    )
    ra = math.degrees(math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))) % 360.0
    dec = math.degrees(math.asin(math.sin(eps) * math.sin(lam)))
    return ra / 15.0, dec


def sun_altitude(ts: float, latitude: float, longitude: float) -> float:
    ra, dec = sun_radec(ts)
    return altitude(ts, latitude, longitude, ra, dec)


def next_crossing(
    f: Callable[[float], float],
    level: float,
    start: float,
    *,
    rising: bool,
    within: float = 86400.0,
    step: float = 600.0,
) -> float | None:
    """The first time after ``start`` (within ``within`` s) when ``f`` crosses ``level`` going up
    (``rising``) or down, to about a second. None if it doesn't."""
    t0, v0 = start, f(start) - level
    end = start + within
    while t0 < end:
        t1 = min(t0 + step, end)
        v1 = f(t1) - level
        if (v0 < 0 <= v1) if rising else (v0 > 0 >= v1):
            lo, hi = t0, t1
            while hi - lo > 1.0:
                mid = (lo + hi) / 2
                vm = f(mid) - level
                if (vm < 0) == (v0 < 0):
                    lo = mid
                else:
                    hi = mid
            return hi
        t0, v0 = t1, v1
    return None


def astronomical_dawn(start: float, latitude: float, longitude: float) -> float | None:
    """The next astronomical dawn (Sun rising through -18°) after ``start``; None in polar
    seasons when the Sun never gets that low or high."""
    return next_crossing(
        lambda t: sun_altitude(t, latitude, longitude), ASTRONOMICAL, start, rising=True
    )


def sets_below(
    start: float, latitude: float, longitude: float, ra: float, dec: float, limit: float
) -> float | None:
    """When a target at RA/Dec next sinks below ``limit`` degrees; None if not within 24 h
    (circumpolar above the limit, or already below and rising)."""
    return next_crossing(
        lambda t: altitude(t, latitude, longitude, ra, dec), limit, start, rising=False
    )


def meridian_flip(start: float, longitude: float, ra: float, offset_degrees: float) -> float | None:
    """When the target's hour angle reaches the flip offset (Ekos flips there), if it's still
    east of that point (hour angle below the offset); None if it has passed it."""
    ha = hour_angle(start, longitude, ra)
    offset = offset_degrees / 15.0
    if ha >= offset:
        return None
    return start + (offset - ha) * 3600.0 / SIDEREAL
