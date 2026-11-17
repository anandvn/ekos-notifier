from datetime import datetime, timezone

import pytest

from ekos_notifier import ephemeris
from ekos_notifier.config import load
from ekos_notifier.digest import DigestRule
from ekos_notifier.ekos.settings import ObservingLimits
from ekos_notifier.events import Event
from ekos_notifier.rules import Rules
from ekos_notifier.sky import SkyRule


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc).timestamp()


GREENWICH = (51.4778, 0.0)


def test_sidereal_time_and_sun_match_meeus():
    # Meeus, Astronomical Algorithms, examples 12.a and 25.a.
    assert ephemeris.gmst_hours(utc(1987, 4, 10)) == pytest.approx(
        13 + 10 / 60 + 46.3668 / 3600, abs=1e-5
    )
    ra, dec = ephemeris.sun_radec(utc(1992, 10, 13))
    assert ra == pytest.approx(13 + 13 / 60 + 31.4 / 3600, abs=1e-3)
    assert dec == pytest.approx(-(7 + 47 / 60 + 6 / 3600), abs=0.01)


def test_astronomical_dawn():
    dawn = ephemeris.astronomical_dawn(utc(2026, 11, 17, 0), *GREENWICH)
    assert abs(dawn - utc(2026, 11, 17, 5, 23)) < 180  # almanac: about 05:23 UTC
    # Midsummer in Scotland: the Sun never reaches -18°, so there's no astronomical dawn.
    assert ephemeris.astronomical_dawn(utc(2026, 6, 21, 0), 57.0, -4.0) is None


def test_setting_and_flip_times():
    start = utc(2026, 11, 17, 0)
    lat, lon = GREENWICH
    ra, dec = 5.0, 10.0  # still east of the meridian at midnight UTC
    sets = ephemeris.sets_below(start, lat, lon, ra, dec, 20.0)
    assert ephemeris.altitude(sets, lat, lon, ra, dec) == pytest.approx(20.0, abs=0.01)
    assert ephemeris.hour_angle(sets, lon, ra) > 0  # setting, west of the meridian
    flip = ephemeris.meridian_flip(start, lon, ra, 5.0)
    assert ephemeris.hour_angle(flip, lon, ra) == pytest.approx(5.0 / 15, abs=1e-3)
    assert ephemeris.meridian_flip(flip + 60, lon, ra, 5.0) is None  # past it


def ev(ts, kind, **data):
    return Event(ts, kind, data, "ekos", "s1")


def run(events, start, until, **sky):
    rules = Rules(sky=SkyRule(**sky), digest=DigestRule(enabled=False))
    out = []
    t = start
    for e in sorted(events, key=lambda e: e.ts):
        while t < e.ts:
            out += rules.tick(t)
            t += 60
        out += rules.handle(e)
    while t <= until:
        out += rules.tick(t)
        t += 60
    return [a for a in out if a.kind == "alert" and a.data["rule"] != "session_started"]


def session(start, ra, dec, site_via="kstars"):
    return [
        ev(start, "session.started", night="n", reason="scheduler"),
        ev(start, "site.location", latitude=GREENWICH[0], longitude=GREENWICH[1], via=site_via),
        ev(start, "mount.coords", ra=ra, dec=dec, device="Mount"),
        ev(start, "mount.state", state="tracking"),
        ev(start, "ekos.log", module="Scheduler", text="Job 'NGC 1' capture is in progress..."),
    ]


def test_twilight_target_low_and_flip_alerts():
    start = utc(2026, 11, 17, 0)
    ra, dec = 5.0, 10.0  # flips 01:35, dawn 05:23, below 20° at 05:56 (UTC, Greenwich)
    limits = ObservingLimits(flip_enabled=True, flip_offset_degrees=5.0, min_altitude=20.0)
    got = run(session(start, ra, dec), start, utc(2026, 11, 17, 6), limits=lambda: limits)
    by_rule = {a.data["rule"]: a for a in got}
    assert [a.data["rule"] for a in got] == ["meridian_flip_soon", "twilight_coming", "target_low"]
    flip = ephemeris.meridian_flip(start, 0.0, ra, 5.0)
    sets = ephemeris.sets_below(start, *GREENWICH, ra, dec, 20.0)
    dawn = ephemeris.astronomical_dawn(start, *GREENWICH)
    assert 0 <= by_rule["meridian_flip_soon"].ts - (flip - 900) < 300  # recomputed every 5 min
    assert by_rule["meridian_flip_soon"].data["priority"] == "quiet"
    assert not by_rule["meridian_flip_soon"].data["immediate"]  # held for the digest
    assert 0 <= by_rule["target_low"].ts - (sets - 1800) < 300
    assert "NGC 1 sinks below 20°" in by_rule["target_low"].data["body"]
    assert 0 <= by_rule["twilight_coming"].ts - (dawn - 2700) < 60
    assert by_rule["twilight_coming"].data["immediate"]


def test_not_tracking_or_no_site_means_no_target_alerts():
    start = utc(2026, 11, 17, 0)
    events = session(start, 5.0, 10.0)[:-2] + [ev(start, "mount.state", state="parked")]
    got = run(events, start, utc(2026, 11, 17, 6), limits=lambda: ObservingLimits(min_altitude=20))
    assert [a.data["rule"] for a in got] == ["twilight_coming"]  # the Sun doesn't need a mount
    no_site = [e for e in session(start, 5.0, 10.0) if e.kind != "site.location"]
    assert run(no_site, start, utc(2026, 11, 17, 6)) == []


def test_kstars_site_wins_over_the_mounts():
    sky = SkyRule()
    sky.resume("s1")
    sky.handle(ev(1.0, "site.location", latitude=-33.87, longitude=151.21, via="kstars"))
    sky.handle(ev(2.0, "site.location", latitude=51.48, longitude=0.0, via="indi"))
    assert sky._site == (-33.87, 151.21)


def test_digest_gets_the_outlook_line():
    start = utc(2026, 11, 17, 0, 30)
    rules = Rules(
        sky=SkyRule(limits=lambda: ObservingLimits(flip_enabled=True, flip_offset_degrees=5.0)),
        digest=DigestRule(interval=1800, align_to_clock=False, min_session=0),
    )
    for e in session(start, 5.0, 10.0):
        rules.handle(e)
    out = rules.tick(start + 1800)
    (d,) = [a for a in out if a.data.get("rule") == "digest"]
    line = d.data["body"].splitlines()[-1]
    assert line.startswith("Flip in ") and "NGC 1 below 10° in" in line and "Dawn in" in line


def test_warning_minutes_in_config(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text("alerts:\n  twilight_warning_minutes: 60\n  target_low_warning_minutes: 20\n")
    cfg = load(f)
    assert (cfg.twilight_warning_minutes, cfg.target_low_warning_minutes) == (60.0, 20.0)
