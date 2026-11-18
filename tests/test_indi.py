import asyncio
import sqlite3
import xml.etree.ElementTree as ET

import pytest

from ekos_notifier import replay
from ekos_notifier.config import ConfigError, load
from ekos_notifier.events import Event
from ekos_notifier.indi import dump
from ekos_notifier.indi.adapter import IndiAdapter
from ekos_notifier.indi.decode import HEARTBEAT, IndiDecoder, Mapping, dewpoint, number
from ekos_notifier.indi.presets import load_builtin, parse
from ekos_notifier.samples import SAMPLE_NIGHT
from ekos_notifier.store import Store

FIXTURE = SAMPLE_NIGHT


def driver(device, exe, interface):
    return (
        f'<defTextVector device="{device}" name="DRIVER_INFO">'
        f'<defText name="DRIVER_NAME">{device}</defText>'
        f'<defText name="DRIVER_EXEC">{exe}</defText>'
        f'<defText name="DRIVER_INTERFACE">{interface}</defText></defTextVector>'
    )


def connection(device, on, tag="def"):
    c, d = ("On", "Off") if on else ("Off", "On")
    one = "defSwitch" if tag == "def" else "oneSwitch"
    return (
        f'<{tag}SwitchVector device="{device}" name="CONNECTION">'
        f'<{one} name="CONNECT">{c}</{one}><{one} name="DISCONNECT">{d}</{one}>'
        f"</{tag}SwitchVector>"
    )


def numbers(device, prop, tag="set", **values):
    one = "defNumber" if tag == "def" else "oneNumber"
    body = "".join(f'<{one} name="{k}">{v}</{one}>' for k, v in values.items())
    return f'<{tag}NumberVector device="{device}" name="{prop}">{body}</{tag}NumberVector>'


def feed(decoder, *xml, t0=0.0, step=1.0):
    out = []
    for i, x in enumerate(xml):
        out += decoder.feed(t0 + i * step, ET.fromstring(x))
    return out


def metrics(events):
    return [
        (e.data["metric"], e.data["device"], e.data["value"]) for e in events if e.kind == "metric"
    ]


WEATHER = [
    connection("Weather", True),
    driver("Weather", "indi_simulator_weather", 128),
    # The trap vectors: thresholds and simulator knobs, not readings.
    numbers("Weather", "WEATHER_TEMPERATURE", "def", MIN_OK=-10, MAX_OK=30, PERC_WARN=15),
    numbers("Weather", "WEATHER_HUMIDITY", "def", MIN_OK=0, MAX_OK=100, PERC_WARN=15),
    numbers("Weather", "WEATHER_CONTROL", "def", Temperature=99, Humidity=99),
    numbers("Weather", "WEATHER_PARAMETERS", "def", WEATHER_TEMPERATURE=10, WEATHER_HUMIDITY=80),
]


def test_weather_station_readings_not_its_thresholds_and_a_derived_dew_point():
    d = IndiDecoder(load_builtin())
    out = feed(d, *WEATHER)
    assert metrics(out) == [
        ("env.temperature", "Weather", 10.0),
        ("env.humidity", "Weather", 80.0),
        ("env.dewpoint", "Weather", round(dewpoint(10, 80), 2)),
    ]
    assert [e.data for e in out if e.kind == "indi.device"] == [
        {
            "device": "Weather",
            "driver": "indi_simulator_weather",
            "interfaces": ["weather"],
            "connected": True,
        }
    ]
    caps = [e.data["metrics"] for e in out if e.kind == "indi.capabilities"][-1]
    assert caps == {k: ["Weather"] for k in ("env.dewpoint", "env.humidity", "env.temperature")}


def test_dewpoint_formula():
    assert dewpoint(20.0, 50.0) == pytest.approx(9.26, abs=0.05)
    assert dewpoint(10.0, 0.0) is None  # the simulator's 0 % humidity


def test_cameras_deadband_heartbeat_and_disconnect():
    d = IndiDecoder(load_builtin())
    out = feed(
        d,
        connection("Cam", True),
        driver("Cam", "indi_asi_ccd", 2),
        numbers("Cam", "CCD_TEMPERATURE", "def", CCD_TEMPERATURE_VALUE=5.0),
    )
    out += feed(d, numbers("Cam", "CCD_TEMPERATURE", CCD_TEMPERATURE_VALUE=5.05), t0=10)  # < 0.1
    out += feed(d, numbers("Cam", "CCD_TEMPERATURE", CCD_TEMPERATURE_VALUE=4.5), t0=20)
    out += feed(d, numbers("Cam", "CCD_TEMPERATURE", CCD_TEMPERATURE_VALUE=4.5), t0=20 + HEARTBEAT)
    assert [v for *_, v in metrics(out)] == [5.0, 4.5, 4.5]
    out = feed(d, connection("Cam", False, "set"), t0=200)
    assert out[0].kind == "indi.device" and out[0].data["connected"] is False
    assert out[-1].kind == "indi.capabilities" and out[-1].data["metrics"] == {}


def test_delete_property_and_reconnect():
    d = IndiDecoder(load_builtin())
    feed(d, *WEATHER)
    out = feed(d, '<delProperty device="Weather" name="WEATHER_PARAMETERS"/>', t0=50)
    assert out[-1].data["metrics"] == {}
    out = feed(d, '<delProperty device="Weather"/>', t0=60)
    assert "Weather" not in d.devices


def test_config_mapping_for_an_unknown_power_box():
    mapping = Mapping.parse("power.voltage", "My Box / POWER_SENSORS.SENSOR_VOLTAGE")
    d = IndiDecoder(load_builtin(), [mapping])
    out = feed(
        d,
        connection("My Box", True),
        driver("My Box", "indi_mybox", 32768),
        numbers("My Box", "POWER_SENSORS", "def", SENSOR_VOLTAGE=12.61, SENSOR_CURRENT=2.1),
    )
    assert metrics(out) == [("power.voltage", "My Box", 12.61)]
    assert [e.data["via"] for e in out if e.kind == "metric"] == ["config"]
    with pytest.raises(ValueError, match="Device / PROPERTY.ELEMENT"):
        Mapping.parse("power.voltage", "POWER_SENSORS.SENSOR_VOLTAGE")


def test_presets_are_data_and_validated():
    presets = load_builtin()
    assert {p.status for p in presets} == {"verified"}
    p = parse({"name": "Box", "match": {"driver": "indi_box"}, "metrics": {"power.voltage": "P.V"}})
    assert p.matches("indi_box", 0) and not p.matches("indi_other", 0xFFFF)
    with pytest.raises(ValueError, match="exactly one"):
        parse({"match": {}, "metrics": {}})
    with pytest.raises(ValueError, match="unknown interface"):
        parse({"match": {"interface": "toaster"}, "metrics": {}})


def test_numbers_and_sexagesimal():
    assert number(" 12.5 ") == 12.5 and number("-12:30:00") == -12.5 and number("n/a") is None


def test_m31_recording():
    _, records = replay.read(FIXTURE)
    events = [e for e in replay.events(records) if e.source == "indi"]
    caps = [e.data["metrics"] for e in events if e.kind == "indi.capabilities"]
    assert caps[-2:] == [{"focuser.temperature": ["Focuser Simulator"]}, {}]  # all disconnected
    most = max(caps, key=len)
    assert most["camera.temperature"] == ["CCD Simulator", "Guide Simulator"]
    assert most["env.temperature"] == ["Weather Simulator"]
    assert "env.dewpoint" not in most  # 0 % humidity: no dew point to derive
    assert sum(e.kind == "metric" for e in events) < 200  # 81 000 INDI elements in


def test_store_keeps_the_source_and_a_frame_gets_the_imaging_camera_temperature(tmp_path):
    s = Store(tmp_path / "tc.db")
    s.append(
        [
            Event(
                1.0, "metric", {"metric": "camera.temperature", "value": -10.0, "device": "Main"}
            ),
            Event(
                1.0, "metric", {"metric": "camera.temperature", "value": 20.0, "device": "Guide"}
            ),
            Event(2.0, "capture.camera", {"camera": "Main"}),
            Event(3.0, "capture.frame", {"type": "light", "filter": "L", "exposure": 60.0}),
        ]
    )
    assert s.samples("camera.temperature", source="Guide") == [(1.0, 20.0)]
    assert s.db.execute("SELECT camera_temp FROM frames").fetchone()[0] == -10.0
    s.close()


def test_migration_from_schema_3(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    from ekos_notifier.store import MIGRATIONS

    for script in MIGRATIONS[:3]:
        con.executescript(script)
    con.execute("PRAGMA user_version = 3")
    con.execute("INSERT INTO samples (ts, metric, value) VALUES (1, 'guide.rms', 0.5)")
    con.commit()
    con.close()
    s = Store(path)
    assert s.samples("guide.rms") == [(1.0, 0.5)]
    s.close()


def test_indi_config(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text(
        'indi:\n  host: "rig.local"\n  port: 7625\n'
        '  metrics:\n    power.voltage: "Box / POWER_SENSORS.SENSOR_VOLTAGE"\n'
    )
    cfg = load(f)
    assert (cfg.indi_host, cfg.indi_port) == ("rig.local", 7625)
    assert cfg.indi_mappings == [Mapping("power.voltage", "Box", "POWER_SENSORS", "SENSOR_VOLTAGE")]
    f.write_text('indi:\n  metrics:\n    power.voltage: "SENSOR_VOLTAGE"\n')
    with pytest.raises(ConfigError, match="indi.metrics"):
        load(f)


async def fake_indi(stream: str):
    """An INDI server that answers getProperties with ``stream``, sent in odd-sized pieces."""
    got = []

    async def serve(reader, writer):
        got.append(await reader.readline())
        data = stream.encode()
        for i in range(0, len(data), 37):  # elements split across reads
            writer.write(data[i : i + 37])
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1], got


def test_adapter_against_a_fake_server():
    async def scenario():
        server, port, got = await fake_indi(
            "".join(WEATHER) + '<message device="Weather" message="[DEBUG] x"/>'
        )
        out = []
        adapter = IndiAdapter(
            out.append, IndiDecoder(load_builtin()), port=port, server=lambda: None, backoff=(5,)
        )
        task = asyncio.create_task(adapter.run())
        while not any(e.kind == "indi.lost" for e in out):
            await asyncio.sleep(0.01)
        task.cancel()
        server.close()
        return out, got

    out, got = asyncio.run(asyncio.wait_for(scenario(), 5))
    assert got == [b'<getProperties version="1.7"/>\n']  # all it ever sends
    kinds = [e.kind for e in out]
    assert kinds[0] == "indi.connected" and kinds[-1] == "indi.lost"
    assert [m for m, *_ in metrics(out)] == ["env.temperature", "env.humidity", "env.dewpoint"]
    assert out[-2].kind == "indi.capabilities" and out[-2].data["metrics"] == {}


def test_indi_dump_scrubs_the_location_and_lists_candidates(tmp_path):
    stream = (
        "".join(WEATHER)
        + (
            connection("Box", True)
            + driver("Box", "indi_mybox", 32768)
            + numbers("Box", "POWER_SENSORS", "def", SENSOR_VOLTAGE=12.6, SENSOR_CURRENT=2.1)
            + numbers("Box", "POWER_LIMITS", "def", MAX_VOLTAGE=15)
            + numbers("Mount", "GEOGRAPHIC_COORD", "def", LAT=51.4778, LONG=359.9, ELEV=46)
            # False candidates seen live on the simulators:
            + numbers(
                "Mount",
                "ALIGNMENT_POINTSET_CURRENT_ENTRY",
                "def",
                ALIGNMENT_POINTSET_CURRENT_ENTRY=0,
            )
            + numbers("Guide", "SIMULATOR_SETTINGS", "def", SIM_TEMPERATURE=25)
            + '<defTextVector device="Cam" name="UPLOAD_SETTINGS">'
            '<defText name="UPLOAD_DIR">/home/astro-jane/Pictures</defText></defTextVector>'
        )
    )

    async def scenario():
        server, port, _ = await fake_indi(stream)
        elements = await dump.collect("127.0.0.1", port, seconds=3, quiet=0.3)
        server.close()
        return elements

    elements = asyncio.run(scenario())
    out = tmp_path / "dump.xml"
    dump.write(dump.scrub(elements), out)
    text = out.read_text()
    assert "51.4778" not in text and "POWER_SENSORS" in text
    assert "astro-jane" not in text and "/home/user/Pictures" in text
    lines = dump.summary(elements, load_builtin())
    assert '  candidate: "Box / POWER_SENSORS.SENSOR_VOLTAGE" = 12.6' in lines
    assert not any("WEATHER_PARAMETERS" in line for line in lines)  # recognised, not a candidate
    assert not any(
        bad in line
        for line in lines
        for bad in ("POWER_LIMITS", "WEATHER_CONTROL", "POINTSET", "SIM_TEMPERATURE")
    )
    assert '  candidate: "Box / POWER_SENSORS.SENSOR_CURRENT" = 2.1' in lines
    assert any("recognised: env.dewpoint, env.humidity, env.temperature" in line for line in lines)


def test_a_quiet_but_connected_camera_still_counts_until_it_disconnects(tmp_path):
    # Seen live: once the cooler settled INDI sent nothing more, and frames lost their temperature.
    s = Store(tmp_path / "tc.db")
    caps = {"metrics": {"camera.temperature": ["Main"]}}
    s.append(
        [
            Event(1.0, "indi.capabilities", caps),
            Event(
                1.0, "metric", {"metric": "camera.temperature", "value": -10.0, "device": "Main"}
            ),
            Event(2.0, "capture.camera", {"camera": "Main"}),
            Event(3600.0, "capture.frame", {"type": "light", "filter": "L", "exposure": 60.0}),
            Event(3700.0, "indi.capabilities", {"metrics": {}}),
            Event(3800.0, "capture.frame", {"type": "light", "filter": "L", "exposure": 60.0}),
        ]
    )
    temps = [r[0] for r in s.db.execute("SELECT camera_temp FROM frames ORDER BY ts")]
    assert temps == [-10.0, None]
    s.close()


def _log_of(stream: str, caplog) -> list[str]:
    """Serve ``stream`` from a fake INDI server that stays connected, and return what the
    adapter logged about what it recognised."""

    async def scenario():
        async def serve(reader, writer):
            await reader.readline()
            writer.write(stream.encode())
            await writer.drain()
            await asyncio.sleep(10)  # stay connected: the devices are still there

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        adapter = IndiAdapter(
            lambda e: None, IndiDecoder(load_builtin()), port=port, server=lambda: None
        )
        adapter.report_after = 0.1
        task = asyncio.create_task(adapter.run())
        await asyncio.sleep(0.6)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        server.close()

    with caplog.at_level("INFO", logger="ekos_notifier.indi.adapter"):
        asyncio.run(scenario())
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("INDI")]


def test_the_log_says_once_what_was_recognised(caplog):
    import gzip
    from pathlib import Path

    fixture = Path(__file__).parent / "fixtures/indi/power-simulator-libindi-2.2.5.xml.gz"
    lines = _log_of(gzip.decompress(fixture.read_bytes()).decode(), caplog)
    assert len(lines) == 1  # one line once it settles, not one per property
    assert lines[0].startswith("INDI recognised: Power Simulator (")
    assert "battery voltage" in lines[0] and "outputs" in lines[0]


def test_the_log_says_when_nothing_is_recognised(caplog):
    lines = _log_of(numbers("Mount", "EQUATORIAL_EOD_COORD", "def", RA=1, DEC=2), caplog)
    assert len(lines) == 1 and "recognised yet" in lines[0] and "indi-dump" in lines[0]


def test_recognised_names_each_device_in_plain_words():
    from ekos_notifier.indi.adapter import recognised

    caps = {
        "camera.temperature": ["QHY CCD"],
        "env.humidity": ["Box"],
        "power.voltage": ["Box"],
        "something.new": ["Box"],
    }
    assert (
        recognised(caps)
        == "Box (humidity, battery voltage, something.new); QHY CCD (camera temperature)"
    )
