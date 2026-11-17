"""The M 31 simulated night (see tests/fixtures/replay/README.md) through the Ekos decoder."""

from collections import Counter
from pathlib import Path

import pytest

from ekos_notifier import replay

FIXTURE = Path(__file__).parent / "fixtures/replay/sim-m31-scheduler-kstars-3.8.5-flatpak.ndjson.gz"


@pytest.fixture(scope="module")
def events():
    meta, records = replay.read(FIXTURE)
    assert meta["kstars"] == "3.8.5 (flatpak)"
    return list(replay.ekos_events(records))


def of(events, kind):
    return [e for e in events if e.kind == kind]


def test_every_state_code_has_a_name(events):
    unknown = {
        (e.kind, e.data["code"])
        for e in events
        if str(e.data.get("state", "")).startswith("unknown")
    }
    assert unknown == set()


def test_frames(events):
    frames = of(events, "capture.frame")
    assert len(frames) == 140
    by = Counter((f.data["type"], f.data["filter"], f.data["exposure"]) for f in frames)
    for filt in ("H_Alpha", "OIII", "SII"):
        assert by[("light", filt, 60.0)] == 12
    for filt in ("Luminance", "Red", "Green", "Blue"):
        assert by[("light", filt, 10.0)] == 20
    assert sum(n for (t, _, _), n in by.items() if t == "flat") == 24
    # KStars 3.8.5 doesn't measure HFR on captured frames here (open question).
    assert all(f.data["hfr"] is None and f.data["star_count"] is None for f in frames)


def test_meridian_flip(events):
    assert [e.data["state"] for e in of(events, "mount.pier_side")] == ["west", "east"]
    assert "meridian_flip" in {e.data["state"] for e in of(events, "capture.state")}


def test_scheduler_ends_by_stopping_ekos(events):
    tail = [
        (e.kind, e.data["state"])
        for e in events
        if e.kind in ("scheduler.state", "indi.state", "ekos.state")
    ]
    assert tail == [
        ("scheduler.state", "running"),
        ("indi.state", "idle"),
        ("ekos.state", "idle"),
        ("scheduler.state", "idle"),
    ]
    # The mount was never parked: the last mount state before Ekos stopped is tracking.
    assert of(events, "mount.park") == []
    assert of(events, "mount.state")[-1].data["state"] == "tracking"


def test_events_are_in_time_order(events):
    ts = [e.ts for e in events]
    assert ts == sorted(ts)
