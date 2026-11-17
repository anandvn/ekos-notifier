from datetime import datetime

import pytest

from ekos_notifier.config import ConfigError, load
from ekos_notifier.digest import DigestRule
from ekos_notifier.events import Event
from ekos_notifier.rules import Rules

T = datetime(2026, 11, 17, 21, 30).timestamp()  # session starts at 21:30 local


def ev(offset, kind, **data):
    return Event(T + offset, kind, data, "ekos", "s1")


def light(offset, f="Ha", exp=300.0, hfr=-1):
    return ev(offset, "capture.frame", type="light", filter=f, exposure=exp, hfr=hfr)


def run(events, until, step=10, **digest):
    rules = Rules(digest=DigestRule(**digest))
    out = []
    t = 0
    for e in sorted(events, key=lambda e: e.ts):
        while T + t < e.ts:
            out += rules.tick(T + t)
            t += step
        out += rules.handle(e)
    while t <= until:
        out += rules.tick(T + t)
        t += step
    return [a for a in out if a.kind == "alert" and a.data["rule"] == "digest"]


START = [ev(0, "session.started", night="2026-11-17", reason="scheduler")]


def test_on_the_hour_all_is_well_and_quiet():
    events = START + [
        ev(5, "ekos.log", module="Scheduler", text="Job 'IC 410' capture is in progress..."),
        ev(
            6,
            "ekos.log",
            module="Capture",
            text="[CCD Simulator] Job requires 300.000-second Ha images, has 0/12 frames captured "
            "and will be processed.",
        ),
    ]
    events += [light(300 * i) for i in range(1, 6)]  # 5 frames by 21:55
    events += [ev(60 * i, "guide.rms", ra=0.5, dec=0.4) for i in range(1, 30)]
    (d,) = run(events, until=1900)  # 22:00 is 1800 s after 21:30
    assert d.data["title"] == "22:00 — All's well"
    assert d.data["priority"] == "quiet" and d.data["immediate"]
    body = d.data["body"].splitlines()
    assert body[0] == "IC 410 · Ha · job 5/12"
    assert body[1] == "Last 30 min: 5 lights (Ha 5×300s), 25 min."
    assert body[2] == "Guide RMS 0.64″ (median)."  # √(0.5² + 0.4²)


def test_attention_lists_problems_and_open_ones_carry_over():
    events = START + [light(300), ev(400, "guide.state", state="guiding")]
    rules_alerts = [
        # Raised by other rules; the digest sees them through Rules.
        ev(500, "guide.state", state="aborted"),
    ]
    got = run(events + rules_alerts, until=1800 + 3600 + 10)
    first, second = got
    assert first.data["title"] == "22:00 — Attention: Guiding lost"
    assert first.data["priority"] == "normal"
    assert "Guiding lost" in first.data["body"]
    # Still not guiding an hour later: still Attention, and listed as open.
    assert second.data["title"] == "23:00 — Attention: Guiding lost"
    assert "Still open: Guiding lost." in second.data["body"]


def test_a_problem_cleared_in_the_hour_is_listed_but_all_is_well():
    events = START + [
        ev(100, "guide.state", state="guiding"),
        ev(300, "guide.state", state="aborted"),
        ev(900, "guide.state", state="guiding"),
    ]
    (d,) = run(events, until=1810)
    assert d.data["title"] == "22:00 — All's well"
    assert "Guiding lost" in d.data["body"] and "Guiding recovered" in d.data["body"]


def test_skipped_right_after_the_session_starts():
    start = [
        Event(T + 1500, "session.started", {"night": "n", "reason": "scheduler"}, "ekos", "s1")
    ]
    got = run(start + [light(1700)], until=1800 + 3600 + 10)
    assert [d.data["title"][:5] for d in got] == ["23:00"]  # 22:00 was 5 min in: skipped


def test_every_n_minutes_from_session_start_or_off():
    events = START + [light(300 * i) for i in range(1, 12)]
    got = run(events, until=3700, interval=1800, align_to_clock=False)
    assert [round(d.ts - T) for d in got] == [1800, 3600]
    assert run(events, until=3700, enabled=False) == []


def test_milestones_dither_failures_and_hfr():
    events = START + [
        light(300, hfr=2.1),
        light(600, hfr=2.3),
        ev(700, "phd2.settle", ok=False, frames=30, dropped=2, error="timed out"),
        ev(800, "ekos.log", module="Capture", text="Meridian flip completed OK."),
    ]
    (d,) = run(events, until=1810)
    body = d.data["body"]
    assert "1 dither failed, HFR 2.20 (median)." in body
    assert "Since last: 21:43 meridian flip complete." in body


def test_digest_config(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text("digest:\n  enabled: false\n  interval_minutes: 30\n  align_to_clock: false\n")
    cfg = load(f)
    assert (cfg.digest_enabled, cfg.digest_interval_minutes, cfg.digest_align_to_clock) == (
        False,
        30.0,
        False,
    )
    f.write_text("digest:\n  interval_minutes: 0\n")
    with pytest.raises(ConfigError, match="interval_minutes"):
        load(f)
    f.write_text("digest:\n  enabled: maybe\n")
    with pytest.raises(ConfigError, match="digest.enabled"):
        load(f)


def test_old_crier_section_still_read(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text("crier:\n  interval_minutes: 30\n")
    assert load(f).digest_interval_minutes == 30.0


def test_a_restart_mid_session_keeps_the_session_age():
    # Seen live: after a restart the digest treated the restart as the session start and would
    # have skipped the next digests as "too soon".
    rules = Rules(digest=DigestRule(interval=300))
    rules.resume("s1", [], started_at=T - 3600)  # session started an hour before the restart
    out = []
    for t in range(0, 400, 10):
        if t == 50:
            out += rules.handle(light(50))
        out += rules.tick(T + t)
    digests = [a for a in out if a.data.get("rule") == "digest"]
    assert [round(d.ts - T) for d in digests] == [300]  # 21:35, not skipped
    assert "Last 5 min: 1 light (" in digests[0].data["body"]


def test_capture_aborted_stays_open_until_capture_runs_again():
    # Seen live: "All's well" at 23:30 with nothing captured since an abort at 23:23.
    events = START + [
        light(300),
        ev(600, "capture.state", state="capturing"),
        ev(
            700,
            "ekos.log",
            module="Capture",
            text="[CCD Simulator] Autofocus failed. Aborting exposure...",
        ),
        ev(700, "capture.state", state="aborted"),
    ]
    resumed = [ev(4000, "capture.state", state="capturing")]
    resumed += [light(4060 + 60 * i, exp=60.0) for i in range(25)]
    first, second, third = run(events + resumed, until=5410, interval=1800)
    assert first.data["title"] == "22:00 \u2014 Attention: Capture aborted"
    assert second.data["title"] == "22:30 \u2014 Attention: Capture aborted"
    assert "Still open: Capture aborted." in second.data["body"]
    assert third.data["title"] == "23:00 \u2014 All's well"  # capture ran again at 22:36


def test_open_problems_survive_a_restart():
    aborted = Event(
        T - 600,
        "alert",
        {
            "rule": "capture_aborted",
            "priority": "high",
            "title": "Capture aborted",
            "body": "",
            "immediate": False,
        },
        "rules",
        "s1",
    )
    rules = Rules(digest=DigestRule(interval=300))
    rules.resume("s1", [aborted], started_at=T - 3600)
    out = []
    for t in range(0, 310, 10):
        out += rules.tick(T + t)
    (d,) = [a for a in out if a.data.get("rule") == "digest"]
    assert d.data["title"] == "21:35 \u2014 Attention: Capture aborted"
