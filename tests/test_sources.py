"""Several devices reporting the same reading, and drivers' placeholder zeros.

Seen live (2026-11-20, a Pegasus PPBM and OpenWeatherMap on the same rig): OpenWeatherMap's
regional 94 % humidity raised Dew risk while the PPBM at the rig read 68-88 %, the digest showed
OpenWeatherMap's numbers (it sorts first), and both drivers reported 0 before their first real
reading (OpenWeatherMap for 13 minutes, while its fetch failed).
"""

from test_indi import connection, driver, feed, metrics, numbers

from ekos_notifier.conditions import DewRule
from ekos_notifier.digest import DigestRule
from ekos_notifier.events import Event
from ekos_notifier.indi.decode import IndiDecoder
from ekos_notifier.indi.presets import load_builtin
from ekos_notifier.pipeline import Pipeline
from ekos_notifier.rules import Rules
from ekos_notifier.sources import Marker, prefer_direct
from ekos_notifier.stats import SessionStats

PPBM, OWM = "Pegasus PPBM", "OpenWeatherMap"


def device(t, name, drv):
    return Event(t, "indi.device", {"device": name, "driver": drv, "connected": True}, "indi")


def reading(t, name, metric, value):
    return Event(t, "metric", {"metric": metric, "value": value, "device": name}, "indi")


def env(t, name, temp, dew, rh):
    return [
        reading(t, name, "env.temperature", temp),
        reading(t, name, "env.dewpoint", dew),
        reading(t, name, "env.humidity", rh),
    ]


DEVICES = [
    device(0, PPBM, "indi_pegasus_ppba"),
    device(0, OWM, "indi_openweathermap_weather"),
]


def test_readings_from_a_secondary_driver_are_marked():
    marker = Marker()
    out = [
        marker.mark(e)
        for e in [*DEVICES, *env(1, PPBM, 23.9, 17.2, 68), *env(1, OWM, 22.2, 21.1, 94)]
    ]
    assert [e.data["device"] for e in out if e.data.get("secondary")] == [OWM, OWM, OWM]


def test_direct_sensors_win_and_secondary_ones_fill_in():
    assert prefer_direct({PPBM: 68, OWM: 94}, {OWM}) == {PPBM: 68}
    assert prefer_direct({OWM: 94}, {OWM}) == {OWM: 94}


def run(events, until):
    out = []
    pipeline = Pipeline(out.append, rules=Rules(dew=DewRule()))
    pipeline.emit(Event(0, "ekos.state", {"state": "success"}, "ekos"))
    pipeline.emit(Event(0, "scheduler.state", {"state": "running"}, "ekos"))
    for e in events:
        pipeline.emit(e)
    for t in range(0, until, 30):
        pipeline.tick(float(t))
    return [e.data["title"] for e in out if e.kind == "alert"]


def test_regional_weather_doesnt_raise_dew_risk_when_the_rig_is_dry():
    events = [*DEVICES]
    for t in range(1, 900, 60):  # 15 minutes: the PPBM dry, OpenWeatherMap humid
        events += env(t, PPBM, 23.9, 17.2, 68) + env(t, OWM, 22.2, 21.1, 94)
    assert "Dew risk" not in run(events, 960)


def test_regional_weather_fills_in_when_its_the_only_source():
    events = [device(0, OWM, "indi_openweathermap_weather")]
    for t in range(1, 900, 60):
        events += env(t, OWM, 22.2, 21.1, 94)
    assert "Dew risk" in run(events, 960)


def test_the_digest_and_summary_use_the_rigs_sensor():
    marker = Marker()
    events = [
        marker.mark(e)
        for e in [*DEVICES, *env(1, OWM, 22.2, 21.1, 94), *env(2, PPBM, 23.9, 17.2, 68)]
    ]
    digest, stats = DigestRule(), SessionStats()
    for e in events:
        digest.on_event(e)
        stats.note(e)
    assert digest._conditions() == "Ambient 23.9 °C · RH 68% · Dew margin 6.7 °C."  # noqa: SLF001
    assert "Ambient 23.9 °C, RH 68%." in stats.lines()


# -- placeholder zeros ----------------------------------------------------------------------


def weather(name, tag, **values):
    return numbers(name, "WEATHER_PARAMETERS", tag, **values)


def decode(*xml):
    decoder = IndiDecoder(load_builtin())
    start = [connection("W", True), driver("W", "indi_openweathermap_weather", 128)]
    return metrics(feed(decoder, *start, *xml))


def test_a_drivers_start_up_zeros_arent_readings():
    out = decode(
        weather("W", "def", WEATHER_TEMPERATURE=0, WEATHER_HUMIDITY=0, WEATHER_DEWPOINT=0),
        weather("W", "set", WEATHER_TEMPERATURE=0, WEATHER_HUMIDITY=0, WEATHER_DEWPOINT=0),
        weather("W", "set", WEATHER_TEMPERATURE=22.2, WEATHER_HUMIDITY=94, WEATHER_DEWPOINT=21.1),
    )
    assert out == [
        ("env.temperature", "W", 22.2),
        ("env.humidity", "W", 94.0),
        ("env.dewpoint", "W", 21.1),
    ]


def test_a_failed_fetch_after_real_readings_isnt_a_reading_either():
    out = decode(
        weather("W", "def", WEATHER_TEMPERATURE=5, WEATHER_HUMIDITY=60, WEATHER_DEWPOINT=-2),
        weather("W", "set", WEATHER_TEMPERATURE=0, WEATHER_HUMIDITY=0, WEATHER_DEWPOINT=0),
    )
    assert out == [
        ("env.temperature", "W", 5.0),
        ("env.humidity", "W", 60.0),
        ("env.dewpoint", "W", -2.0),
    ]


def test_a_real_zero_still_counts():
    out = decode(
        weather("W", "def", WEATHER_TEMPERATURE=1.5, WEATHER_HUMIDITY=80),
        weather("W", "set", WEATHER_TEMPERATURE=0.0, WEATHER_HUMIDITY=82),  # a winter night
    )
    assert ("env.temperature", "W", 0.0) in out


def test_a_temperature_beside_a_missing_humidity_still_counts():
    # The weather simulator reports 15 °C with its humidity left at 0.
    out = decode(weather("W", "def", WEATHER_TEMPERATURE=15, WEATHER_HUMIDITY=0))
    assert out == [("env.temperature", "W", 15.0)]
