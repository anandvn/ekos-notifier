from dbus_fast import Variant

from town_crier.ekos import states
from town_crier.ekos.decode import decode_name_owner_changed, decode_signal, plain

T = 1000.0


def one(path, member, args):
    events = decode_signal(T, path, member, args)
    assert len(events) == 1, events
    return events[0]


def test_plain_unwraps_both_variant_shapes():
    live = {
        "hfr": Variant("d", 2.5),
        "filter": Variant("s", "Red"),
        "n": Variant("v", Variant("i", 1)),
    }
    recorded = {"hfr": {"type": "d", "data": 2.5}, "filter": {"type": "s", "data": "Red"}}
    assert plain(live) == {"hfr": 2.5, "filter": "Red", "n": 1}
    assert plain(recorded) == {"hfr": 2.5, "filter": "Red"}


def test_status_struct_and_bare_int_decode_the_same():
    assert one("/KStars/Ekos/Mount", "newParkStatus", [[4]]).data["state"] == "unparked"
    assert one("/KStars/Ekos/Mount", "newParkStatus", [4]).data["state"] == "unparked"


def test_capture_state_keeps_train():
    e = one("/KStars/Ekos/Capture", "newStatus", [[18], "Primary", 0])
    assert (e.kind, e.data["state"], e.data["train"]) == (
        "capture.state",
        "meridian_flip",
        "Primary",
    )


def test_unknown_code_is_named_not_dropped():
    e = one("/KStars/Ekos/Scheduler", "newStatus", [[42]])
    assert (e.data["state"], e.data["code"]) == ("unknown_42", 42)


def test_frame_unmeasured_hfr_becomes_none():
    info = {
        "type": Variant("i", 0),
        "filter": Variant("s", "Ha"),
        "exposure": Variant("d", 60.0),
        "hfr": Variant("d", -1.0),
        "starCount": Variant("i", 0),
        "eccentricity": Variant("d", -1.0),
        "filename": Variant("s", "/x/a.fits"),
    }
    e = one("/KStars/Ekos/Capture", "captureComplete", [info, "Primary"])
    assert e.kind == "capture.frame"
    assert e.data["type"] == "light" and e.data["filter"] == "Ha" and e.data["exposure"] == 60.0
    assert e.data["hfr"] is None and e.data["star_count"] is None and e.data["eccentricity"] is None
    assert e.data["train"] == "Primary"


def test_frame_measured_hfr_kept():
    info = {"type": 3, "hfr": 2.1, "starCount": 57, "filter": "Red"}
    e = one("/KStars/Ekos/Capture", "captureComplete", [info, "Primary"])
    assert (e.data["type"], e.data["hfr"], e.data["star_count"]) == ("flat", 2.1, 57)


def test_focus_hfr_minus_one_means_no_stars():
    e = one("/KStars/Ekos/Focus", "newHFR", [-1.0, 75000, True, "Primary"])
    assert e.data == {"hfr": None, "position": 75000, "in_autofocus": True, "train": "Primary"}


def test_log_lines_are_context_events():
    e = one("/KStars/Ekos/Scheduler", "newLog", ["Job 'M 31' is focusing."])
    assert e.kind == "ekos.log" and e.data == {
        "module": "Scheduler",
        "text": "Job 'M 31' is focusing.",
    }
    assert one("/KStars/Ekos", "newLog", ["INDI services stopped."]).data["module"] == "Ekos"


def test_indi_device_disconnect():
    e = one("/KStars/INDI/GenericDevice/3", "Disconnected", [])
    assert (e.kind, e.data) == ("device.disconnected", {"object": "/KStars/INDI/GenericDevice/3"})


def test_ignored_signals():
    assert decode_signal(T, "/KStars/Ekos", "settleStatusChanged", [[2]]) == []
    assert decode_signal(T, "/KStars/Ekos/Observatory", "newStatus", [[0]]) == []
    assert decode_signal(T, "/KStars/SimClock", "timeChanged", []) == []
    assert decode_signal(T, "/KStars/EkosX", "newStatus", [[0]]) == []


def test_name_owner_changed():
    assert [e.kind for e in decode_name_owner_changed(T, ["org.kde.kstars", "", ":1.5"])] == [
        "kstars.appeared"
    ]
    assert [e.kind for e in decode_name_owner_changed(T, ["org.kde.kstars", ":1.5", ""])] == [
        "kstars.vanished"
    ]
    assert decode_name_owner_changed(T, ["org.kde.other", "", ":1.6"]) == []


def test_verified_codes_exist_in_tables():
    for table, codes in states.VERIFIED.items():
        assert codes <= set(states.TABLES[table]), table
