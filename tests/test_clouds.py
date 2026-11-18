import pytest

from ekos_notifier.clouds import CloudsRule
from ekos_notifier.events import Event


def ev(ts, kind, sid="s1", **data):
    return Event(ts, kind, data, "ekos", sid)


START = ev(0, "session.started", night="2026-11-17")


def steps(start, end, snr, every=3):
    return [ev(t, "phd2.step", snr=snr) for t in range(start, end, every)]


def light(ts, stars, hfr=2.0, f="Ha", exp=300.0):
    return ev(ts, "capture.frame", type="light", filter=f, exposure=exp, star_count=stars, hfr=hfr)


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


def guiding(t=1):
    return ev(t, "phd2.state", state="guiding")


def test_the_guide_star_fades_then_comes_back():
    events = [START, guiding(), *steps(3, 600, 40.0), *steps(600, 900, 12.0)]
    events += steps(900, 1500, 39.0)
    out = timeline(CloudsRule(), events, until=1500)
    assert [a.data["rule"] for a in out] == ["clouds", "clouds_cleared"]
    alert, cleared = out
    assert alert.data["priority"] == "high" and alert.data["title"] == "Clouds"
    assert "SNR now 12, 40 before" in alert.data["body"]
    assert 720 <= alert.ts <= 760  # faded for 2 min (the 60 s median catches up first)
    assert cleared.data["immediate"] and cleared.ts >= 900 + 300


def test_a_brief_dip_is_silent():
    events = [START, guiding(), *steps(3, 600, 40.0), *steps(600, 660, 10.0)]
    events += steps(660, 1200, 40.0)
    assert timeline(CloudsRule(), events, until=1200) == []


def test_a_star_lost_while_bright_is_not_clouds():
    # Seen live: a runaway mount walked the star off the frame at full SNR.
    events = [START, guiding(), *steps(3, 600, 40.0), ev(601, "phd2.state", state="lost_lock")]
    events += [
        ev(602 + i, "phd2.star_lost", snr=0.0, status="Star lost - low mass") for i in range(60)
    ]
    events += [ev(700, "phd2.state", state="stopped")]
    assert timeline(CloudsRule(), events, until=1500) == []


def test_fading_then_lost_and_guiding_stopped_still_alerts():
    events = [START, guiding(), *steps(3, 600, 40.0), *steps(600, 660, 8.0)]
    events += [ev(661, "phd2.state", state="lost_lock")]
    events += [ev(662 + i, "phd2.star_lost", snr=0.0) for i in range(0, 60, 2)]
    events += [ev(722, "phd2.state", state="stopped")]
    [alert] = timeline(CloudsRule(), events, until=1000)
    assert "PHD2 lost the star" in alert.data["body"] and "SNR now lost" in alert.data["body"]


def test_frames_losing_stars():
    frames = [light(300 * i, 400 + i) for i in range(1, 5)]
    frames += [light(1500, 120, hfr=3.1), light(1800, 90, hfr=3.4)]
    [alert] = timeline(CloudsRule(), [START, *frames], until=1900)
    assert alert.ts >= 1800
    assert "Last frame" in alert.data["body"] and "90 stars, typically 402" in alert.data["body"]
    assert "HFR 3.40, typically 2.00 (2 frames in a row)" in alert.data["body"]
    # Back: the next frame's stars, and no guide star to wait for.
    out = timeline(CloudsRule(), [START, *frames, light(2100, 395)], until=2200)
    assert [a.data["rule"] for a in out] == ["clouds", "clouds_cleared"]


def test_a_new_target_has_its_own_stars():
    frames = [light(300 * i, 2000) for i in range(1, 5)]
    frames += [ev(1300, "capture.target", target="M 31")]
    frames += [light(1500, 300), light(1800, 310), light(2100, 290), light(2400, 120)]
    assert timeline(CloudsRule(), [START, *frames], until=2500) == []


def test_unmeasured_frames_and_off_and_outside_a_session():
    frames = [light(300 * i, None, hfr=None) for i in range(1, 8)]
    assert timeline(CloudsRule(), [START, *frames], until=2500) == []
    fade = [START, guiding(), *steps(3, 600, 40.0), *steps(600, 900, 12.0)]
    assert timeline(CloudsRule(drop=0), fade, until=1000) == []
    outside = [ev(e.ts, e.kind, sid=None, **e.data) for e in fade[1:]]
    assert timeline(CloudsRule(), outside, until=1000) == []


def test_a_new_star_starts_a_new_baseline():
    events = [START, guiding(), *steps(3, 600, 40.0), ev(601, "phd2.state", state="selected")]
    events += [guiding(602), *steps(603, 1200, 15.0)]  # a fainter star, guiding fine
    assert timeline(CloudsRule(), events, until=1200) == []


def test_resume_does_not_repeat_the_alert():
    rule = CloudsRule()
    rule.resume("s1", [ev(0, "alert", rule="clouds")])
    events = [guiding(), *steps(3, 200, 40.0), *steps(200, 900, 12.0)]
    assert timeline(rule, events, until=1000) == []  # still cloudy: no second alert


def test_config_and_service(tmp_path):
    from ekos_notifier.config import ConfigError, load
    from ekos_notifier.service import build_rules

    path = tmp_path / "config.yaml"
    path.write_text("alerts:\n  clouds_drop_percent: 70\n")
    config = load(path)
    assert config.clouds_drop_percent == 70.0
    [rule] = [r for r in build_rules(config, live=False).session_rules if isinstance(r, CloudsRule)]
    assert abs(rule.below - 0.3) < 1e-9
    path.write_text("alerts:\n  clouds_drop_percent: lots\n")
    with pytest.raises(ConfigError, match="clouds_drop_percent"):
        load(path)
