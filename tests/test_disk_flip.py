from datetime import datetime, timezone

from ekos_notifier import ephemeris
from ekos_notifier.config import load
from ekos_notifier.disk import DiskRule, free_bytes
from ekos_notifier.ekos.settings import ObservingLimits
from ekos_notifier.events import Event
from ekos_notifier.sky import FLIP_GRACE, SkyRule

GB = 10**9


def ev(ts, kind, sid="s1", **data):
    return Event(ts, kind, data, "test", sid)


def timeline(rule, events, until=None, step=10.0):
    out, now = [], None
    for e in sorted(events, key=lambda e: e.ts):
        if now is not None:
            while now + step < e.ts:
                now += step
                out += rule.tick(now)
        out += rule.handle(e)
        now = e.ts
    while until is not None and now is not None and now + step <= until:
        now += step
        out += rule.tick(now)
    return out


def rules_of(alerts):
    return [a.data["rule"] for a in alerts]


START = ev(0, "session.started", night="2026-11-17")


# -- disk --------------------------------------------------------------------------------------


class Disk:
    def __init__(self, free):
        self.free = free
        self.asked = []

    def __call__(self, path):
        self.asked.append(path)
        return self.free


def frame(ts, n):
    path = f"/home/rig/Pictures/M_31/Light/Ha/M_31_Light_Ha_{n:03d}.fits"
    return ev(ts, "capture.frame", type="light", exposure=300.0, file=path)


def test_disk_low_with_room_left_at_tonights_pace():
    disk = Disk(50 * GB)
    rule = DiskRule(free=disk, size=lambda path: 32_000_000, home="/home/rig")
    events = [START, *(frame(300 * n, n) for n in range(1, 5))]
    assert timeline(rule, events) == []
    assert disk.asked[-1] == "/home/rig/Pictures/M_31/Light/Ha"  # where Ekos saves
    disk.free = 8 * GB
    [alert] = timeline(rule, [frame(1500, 5)], until=1600)
    assert alert.data["priority"] == "high"
    assert "8.0 GB free on /home/rig/Pictures/M_31/Light/Ha (limit 10 GB)" in alert.data["body"]
    assert "about 250 more frames at 32 MB, 20 h 50 min at tonight's pace" in alert.data["body"]
    disk.free = 7 * GB
    assert timeline(rule, [frame(1800, 6)], until=2000) == []  # once
    disk.free = 10.5 * GB
    assert timeline(rule, [], until=2100) == []  # not 1 GB clear yet
    disk.free = 12 * GB
    [ok] = timeline(rule, [ev(2100, "capture.state", state="capturing")], until=2200)
    assert ok.data["rule"] == "disk_ok" and ok.data["immediate"]


def test_disk_checked_once_a_minute_not_every_tick():
    disk = Disk(50 * GB)
    timeline(DiskRule(free=disk, home="/h"), [START], until=600, step=1.0)
    assert 9 <= len(disk.asked) <= 11


def test_disk_low_only_in_a_session_and_not_when_off():
    assert timeline(DiskRule(free=Disk(GB), home="/h"), [ev(0, "x", sid=None)], until=300) == []
    assert timeline(DiskRule(0, free=Disk(GB), home="/h"), [START], until=300) == []
    assert timeline(DiskRule(free=Disk(None), home="/h"), [START], until=300) == []


def test_free_bytes_uses_the_nearest_existing_folder(tmp_path):
    assert free_bytes(str(tmp_path / "not" / "yet" / "made.fits")) > 0


def test_config_disk_low_gb(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("alerts:\n  disk_low_gb: 25\n")
    assert load(path).disk_low_gb == 25.0


# -- meridian flip -----------------------------------------------------------------------------

SYDNEY = (-33.87, 151.21)
T0 = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc).timestamp()


def sky(flip_enabled=True):
    limits = ObservingLimits(flip_enabled=flip_enabled, flip_offset_degrees=0.0)
    return SkyRule(limits=lambda: limits)


def tracking(pier="west", minutes_east=10.0):
    """Settled on a target ``minutes_east`` of the meridian, on ``pier``."""
    ra = (ephemeris.lst_hours(T0, SYDNEY[1]) + minutes_east / 60) % 24
    return [
        ev(T0, "session.started", night="2026-11-17"),
        ev(T0, "site.location", sid=None, latitude=SYDNEY[0], longitude=SYDNEY[1], via="kstars"),
        ev(T0 + 1, "mount.pier_side", state=pier),
        ev(T0 + 1, "mount.coords", ra=ra, dec=-20.0),
        ev(T0 + 2, "mount.state", state="tracking"),
    ]


def mount_log(ts, text):
    return ev(ts, "ekos.log", module="Mount", text=text)


def test_flip_failure_logged_by_ekos_mount():
    rule = sky()
    events = [
        *tracking(),
        mount_log(T0 + 700, "Meridian flip failed, pier side not changed"),
        mount_log(T0 + 701, "meridian flip failed, retrying in 4 minutes"),
    ]
    alerts = [a for a in timeline(rule, events) if a.data["rule"] != "meridian_flip_soon"]
    assert rules_of(alerts) == ["meridian_flip_failed"]
    assert alerts[0].data["priority"] == "emergency"
    assert "“Meridian flip failed, pier side not changed”" in alerts[0].data["body"]
    assert alerts[0].data["body"].splitlines()[-1].endswith("Check the mount, or park it.")
    [done] = timeline(rule, [mount_log(T0 + 950, "Meridian flip completed.")])
    assert done.data["rule"] == "meridian_flip_done"


def test_ekos_retrying_a_post_flip_calibration_is_not_a_failure():
    text = "[CCD Simulator] Post meridian flip calibration error. Restarting..."
    events = [*tracking(), ev(T0 + 700, "ekos.log", module="Capture", text=text)]
    assert "meridian_flip_failed" not in rules_of(timeline(sky(), events))


def test_flip_overdue_when_the_pier_side_never_changes():
    rule = sky()
    alerts = timeline(rule, tracking(), until=T0 + 600 + FLIP_GRACE + 30)
    assert rules_of(alerts) == ["meridian_flip_soon", "meridian_flip_overdue"]
    body = alerts[1].data["body"]
    assert "should have flipped the mount at" in body and "15 min ago" in body
    [done] = timeline(rule, [ev(T0 + 2000, "mount.pier_side", state="east")])
    assert done.data["rule"] == "meridian_flip_done"


def test_flip_overdue_quotes_why_ekos_did_not_flip():
    events = [*tracking(), mount_log(T0 + 610, "Meridian flip inactive (no target set)")]
    alerts = timeline(sky(), events, until=T0 + 600 + FLIP_GRACE + 30)
    assert "Ekos says: “Meridian flip inactive (no target set)”." in alerts[-1].data["body"]


def test_flip_waits_for_the_exposure_in_progress():
    events = [*tracking(), ev(T0 + 3, "capture.frame", type="light", exposure=600.0)]
    alerts = timeline(sky(), events, until=T0 + 600 + FLIP_GRACE + 300)
    assert "meridian_flip_overdue" not in rules_of(alerts)


def test_no_overdue_after_a_flip_or_when_flips_are_off_or_already_past():
    flipped = [*tracking(), ev(T0 + 650, "mount.pier_side", state="east")]
    assert "meridian_flip_overdue" not in rules_of(timeline(sky(), flipped, until=T0 + 3000))
    off = timeline(sky(flip_enabled=False), tracking(), until=T0 + 3000)
    assert "meridian_flip_overdue" not in rules_of(off)
    past = timeline(sky(), tracking(minutes_east=-30), until=T0 + 3000)  # settled after the flip
    assert "meridian_flip_overdue" not in rules_of(past)
    unknown = [e for e in tracking() if e.kind != "mount.pier_side"]
    assert "meridian_flip_overdue" not in rules_of(timeline(sky(), unknown, until=T0 + 3000))
