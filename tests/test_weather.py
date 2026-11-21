"""The Weather alert, from INDI's standard weather interface (issue #54).

The light labels are OpenWeatherMap's, from the dump attached to #54 (indi_openweathermap_weather
1.2): WEATHER_STATUS has one light per critical reading, Ok, Busy (near its limit) or Alert.
"""

from test_indi import connection, driver, feed, numbers

from ekos_notifier.events import Event
from ekos_notifier.indi.decode import IndiDecoder
from ekos_notifier.indi.presets import load_builtin
from ekos_notifier.pipeline import Pipeline
from ekos_notifier.rules import Rules

LABELS = {
    "WEATHER_FORECAST": "Weather",
    "WEATHER_TEMPERATURE": "Temperature (C)",
    "WEATHER_WIND_SPEED": "Wind (m/s)",
    "WEATHER_RAIN_HOUR": "Rain precip (mm)",
    "WEATHER_SNOW_HOUR": "Snow precip (mm)",
}


def lights(device, tag="set", **states):
    one = "defLight" if tag == "def" else "oneLight"
    body = "".join(
        f'<{one} name="{n}"' + (f' label="{LABELS[n]}"' if tag == "def" else "") + f">{s}</{one}>"
        for n, s in states.items()
    )
    return f'<{tag}LightVector device="{device}" name="WEATHER_STATUS">{body}</{tag}LightVector>'


ALL_OK = {n: "Ok" for n in LABELS}


def statuses(events):
    return [
        (e.data["state"], e.data["alerts"], e.data["warnings"])
        for e in events
        if e.kind == "weather.status"
    ]


def test_the_weather_lights_become_a_status():
    decoder = IndiDecoder(load_builtin())
    out = feed(
        decoder,
        connection("OpenWeatherMap", True),
        driver("OpenWeatherMap", "indi_openweathermap_weather", 128),
        numbers(
            "OpenWeatherMap", "WEATHER_PARAMETERS", "def", WEATHER_RAIN_HOUR=0, WEATHER_WIND_SPEED=3
        ),
        lights("OpenWeatherMap", "def", **ALL_OK),
        lights("OpenWeatherMap", WEATHER_WIND_SPEED="Busy"),
        numbers("OpenWeatherMap", "WEATHER_PARAMETERS", WEATHER_RAIN_HOUR=1.2),
        lights("OpenWeatherMap", WEATHER_RAIN_HOUR="Alert", WEATHER_WIND_SPEED="Busy"),
        lights("OpenWeatherMap", WEATHER_RAIN_HOUR="Alert", WEATHER_WIND_SPEED="Busy"),  # no news
    )
    assert statuses(out) == [
        ("ok", [], []),
        ("warning", [], ["Wind (m/s)"]),
        ("alert", ["Rain precip (mm)"], ["Wind (m/s)"]),
    ]
    alert = [e for e in out if e.kind == "weather.status"][-1]
    assert alert.data["values"]["Rain precip (mm)"] == 1.2
    caps = [e for e in out if e.kind == "indi.capabilities"][-1].data["metrics"]
    assert caps["weather.status"] == ["OpenWeatherMap"]


# -- the rule -------------------------------------------------------------------------------


def device(t, name, drv):
    return Event(t, "indi.device", {"device": name, "driver": drv, "connected": True}, "indi")


def status(t, name, **lights):
    """lights: READING=(state, label, value)."""
    rank = {"idle": 0, "ok": 1, "warning": 2, "alert": 3}
    data = {
        "device": name,
        "state": max((st for st, *_ in lights.values()), key=rank.get, default="idle"),
        "alerts": sorted(label for st, label, _v in lights.values() if st == "alert"),
        "warnings": sorted(label for st, label, _v in lights.values() if st == "warning"),
        "values": {label: v for _st, label, v in lights.values() if v is not None},
        "lights": {n: {"state": st, "label": label} for n, (st, label, _v) in lights.items()},
    }
    return Event(t, "weather.status", data, "indi")


OWM, AAG, PPBM = "OpenWeatherMap", "AAG CloudWatcher", "Pegasus PPBM"


def rain(state, mm):
    return {"WEATHER_RAIN_HOUR": (state, "Rain precip (mm)", mm)}


def temperature(state, c):
    return {"WEATHER_TEMPERATURE": (state, "Temperature (C)", c)}


def run(events, until, session=False):
    out = []
    pipeline = Pipeline(out.append, rules=Rules())
    if session:
        events = [
            *events,
            Event(0, "ekos.state", {"state": "success"}, "ekos"),
            Event(0, "scheduler.state", {"state": "running"}, "ekos"),
        ]
    for e in sorted(events, key=lambda e: e.ts):
        pipeline.emit(e)
        pipeline.tick(e.ts)
    for t in range(0, until, 30):
        pipeline.tick(float(t))
    return [e for e in out if e.kind == "alert"]


def test_rain_is_an_alert_at_any_time_and_clears_after_ten_minutes():
    events = [
        device(0, OWM, "indi_openweathermap_weather"),
        status(10, OWM, **rain("ok", 0)),
        status(60, OWM, **rain("alert", 1.2)),  # no session: still sent
        status(300, OWM, **rain("warning", 0.2)),
        status(400, OWM, **rain("alert", 0.9)),  # flickering back: not again
        status(500, OWM, **rain("ok", 0)),
    ]
    alerts = run(events, 1300)
    assert [a.data["title"] for a in alerts] == ["Weather alert", "Weather OK"]
    body = alerts[0].data["body"]
    assert "OpenWeatherMap reports, past its limits: Rain precip (mm) 1.2." in body
    assert "regional weather from the internet" in body
    assert alerts[1].ts >= 500 + 600


def test_a_station_at_the_rig_wins_for_the_readings_it_watches():
    events = [
        device(0, AAG, "indi_aagcloudwatcher_ng"),
        device(0, OWM, "indi_openweathermap_weather"),
        status(10, AAG, **rain("ok", 0)),  # rain measured at the rig: dry
        status(60, OWM, **rain("alert", 1.2)),  # regional rain: doesn't count
    ]
    assert run(events, 300) == []
    events.append(status(120, AAG, **rain("alert", 0.4)))
    alerts = run(events, 300)
    assert [a.data["title"] for a in alerts] == ["Weather alert"]
    assert "AAG CloudWatcher reports" in alerts[0].data["body"]
    assert "regional" not in alerts[0].data["body"]


def test_regional_rain_counts_beside_a_station_that_doesnt_watch_rain():
    # Seen in #54's dump: the Pegasus PPBM's weather status watches temperature only.
    events = [
        device(0, PPBM, "indi_pegasus_ppba"),
        device(0, OWM, "indi_openweathermap_weather"),
        status(10, PPBM, **temperature("ok", 23.8)),
        status(60, OWM, **temperature("alert", 35), **rain("alert", 1.2)),
    ]
    alerts = run(events, 300)
    assert [a.data["title"] for a in alerts] == ["Weather alert"]
    body = alerts[0].data["body"]
    assert "Rain precip (mm) 1.2" in body and "Temperature" not in body  # the PPBM's counts


def test_the_pre_flight_check_reports_the_weather():
    events = [device(0, OWM, "indi_openweathermap_weather")]
    alerts = run([*events, status(-5, OWM, **rain("ok", 0))], 60, session=True)
    start = next(a for a in alerts if a.data["title"] == "Starting session")
    assert "weather OK (OpenWeatherMap)" in start.data["body"]
    alerts = run([*events, status(-5, OWM, **rain("alert", 1.2))], 60, session=True)
    start = next(a for a in alerts if a.data["title"] == "Starting session")
    assert "Check: weather alert from OpenWeatherMap: Rain precip (mm) 1.2." in start.data["body"]


def test_a_live_rain_shower_on_the_weather_simulator():
    """Recorded 2026-11-21: indi_simulator_weather (libindi 2.2.5) with its Precip set to 5 mm for
    75 s. The driver updates its lights before its readings, so the alert waits for the same
    cycle's value: it says 5 mm, not the 0 mm of the cycle before."""
    from ekos_notifier import simulate
    from ekos_notifier.config import Config
    from ekos_notifier.service import build_rules

    night = simulate.recording("tests/fixtures/replay/sim-weather-rain-libindi-2.2.5.ndjson.gz")
    alerts = simulate.run(night, build_rules(Config(), live=False))
    assert [a.data["title"] for a in alerts] == ["Weather alert", "Weather OK"]
    assert "Weather Simulator reports, past its limits: Precip (mm) 5." in alerts[0].data["body"]
    assert "regional" not in alerts[0].data["body"]  # a station, not OpenWeatherMap
    assert alerts[1].ts - alerts[0].ts >= 600
