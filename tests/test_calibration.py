from datetime import datetime

from ekos_notifier.calibration import CalibrationRule
from ekos_notifier.events import Event
from ekos_notifier.messages import MessageSettings
from ekos_notifier.rules import Rules

EVENING = datetime(2026, 11, 17, 21, 0).timestamp()  # local: the night of the 17th


def ev(ts, kind, sid="s1", **data):
    return Event(EVENING + ts, kind, data, "ekos", sid)


def frame(ts, kind, flt, sid="s1"):
    return ev(ts, "capture.frame", sid=sid, type=kind, filter=flt, exposure=60.0)


def closed(ts, reason="scheduler_finished", sid="s1"):
    return ev(ts, "session.closed", sid=sid, state="closed", reason=reason, night="2026-11-17")


def run(rule, events):
    out = []
    for e in events:
        out += rule.handle(e)
    return out


def test_flats_reminder_names_filters_without_flats():
    events = [
        frame(10, "light", "Ha"),
        frame(20, "light", "OIII"),
        frame(30, "flat", "OIII"),
        frame(40, "light", None),
        closed(100),
    ]
    [alert] = run(CalibrationRule(), events)
    d = alert.data
    assert (d["rule"], d["priority"], d["immediate"]) == ("flats_reminder", "normal", True)
    assert d["filters"] == ["Ha", "no filter"]
    assert d["body"].startswith("No flats yet tonight for Ha, no filter.")


def test_flats_from_an_earlier_session_or_after_midnight_count_and_no_repeats():
    rule = CalibrationRule()
    first = run(rule, [frame(10, "light", "Ha"), closed(100)])
    assert [a.data["filters"] for a in first] == [["Ha"]]
    # Later the same night (after midnight): Ha again and a new filter; flats for SII.
    later = [
        frame(4 * 3600, "light", "Ha", sid="s2"),
        frame(4 * 3600 + 10, "light", "SII", sid="s2"),
        frame(4 * 3600 + 20, "flat", "SII", sid="s2"),
        closed(4 * 3600 + 100, sid="s2"),
    ]
    assert run(rule, later) == []  # Ha already reminded, SII has flats


def test_no_flats_reminder_when_you_stopped_ekos_or_nothing_was_imaged():
    assert run(CalibrationRule(), [frame(10, "light", "Ha"), closed(100, "ekos_stopped")]) == []
    assert run(CalibrationRule(), [closed(100)]) == []
    abandoned = ev(100, "session.closed", state="abandoned", reason="ekos_lost", night="n")
    assert run(CalibrationRule(), [frame(10, "light", "Ha"), abandoned]) == []


def test_darks_reminder_once_a_night_and_off_by_default():
    rule = CalibrationRule()
    events = [
        ev(10, "capture.job", sid=None, job=2, frame_type="dark"),
        ev(900, "capture.job", sid=None, job=3, frame_type="dark"),
    ]
    [alert] = run(rule, events)
    assert alert.data["rule"] == "darks_reminder" and alert.session_id is None
    assert "Cover the telescope" in alert.data["body"]
    assert MessageSettings().apply([alert])[0].data["off"]  # off unless turned on
    turned_on = MessageSettings.parse({"darks_reminder": "normal"}).apply([alert])[0]
    assert not turned_on.data.get("off") and turned_on.data["immediate"]


def test_flats_reminder_follows_the_summary():
    rules = Rules()
    out = []
    for e in [
        ev(0, "session.started", night="2026-11-17", reason="light_frame"),
        frame(10, "light", "Ha"),
        closed(100, "sequence_complete"),
    ]:
        out += rules.handle(e)
    names = [a.data["rule"] for a in out if a.kind == "alert"]
    assert names == ["session_started", "session_summary", "flats_reminder"]


def test_resume_remembers_what_was_reminded():
    rule = CalibrationRule()
    sent = ev(0, "alert", rule="flats_reminder", filters=["Ha"])
    rule.resume("s1", [sent])
    assert run(rule, [frame(10, "light", "Ha"), closed(100)]) == []
