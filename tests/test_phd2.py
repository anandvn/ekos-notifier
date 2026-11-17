import asyncio
import json
from pathlib import Path

from town_crier import replay
from town_crier.ekos.settings import UNKNOWN, GuiderSettings
from town_crier.phd2.adapter import Phd2Adapter
from town_crier.phd2.decode import Phd2Decoder

FIXTURE = Path(__file__).parent / "fixtures/replay/sim-m31-scheduler-kstars-3.8.5-flatpak.ndjson.gz"

VERSION = {"Event": "Version", "PHDVersion": "2.6.14", "PHDSubver": "", "MsgVersion": 1}
STEP = {
    "Event": "GuideStep",
    "Frame": 7,
    "dx": 1.065,
    "dy": -0.343,
    "RADistanceRaw": 1.058,
    "DECDistanceRaw": -0.37,
    "RADuration": 207,
    "RADirection": "East",
    "DECDuration": 50,
    "DECDirection": "North",
    "StarMass": 5318,
    "SNR": 50.55,
    "HFD": 3.55,
    "ErrorCode": 0,
}


def feed(decoder, *msgs, t=0.0):
    out = []
    for i, m in enumerate(msgs):
        out += decoder.feed(t + i, m)
    return out


def kinds(events):
    return [(e.kind, e.data.get("state") or e.data.get("result")) for e in events]


def test_connect_burst_is_a_snapshot_then_live():
    d = Phd2Decoder()
    out = feed(
        d,
        VERSION,
        {"Event": "LockPositionSet", "X": 1, "Y": 2},
        {"Event": "CalibrationComplete", "Mount": "m"},
        {"Event": "StartGuiding"},
        {"Event": "AppState", "State": "Guiding"},
        STEP,
    )
    assert kinds(out) == [
        ("phd2.connected", None),
        ("phd2.calibration", "complete"),
        ("phd2.state", "guiding"),
        ("phd2.step", None),
    ]
    assert [e.data.get("snapshot", False) for e in out] == [False, True, True, False]
    assert out[0].data == {"version": "2.6.14"} and {e.source for e in out} == {"phd2"}


def test_guide_step_fields():
    d = Phd2Decoder()
    (state, step) = feed(d, STEP)
    assert state.data == {"state": "guiding"}
    assert step.data == {
        "frame": 7,
        "dx": 1.065,
        "dy": -0.343,
        "ra": 1.058,
        "dec": -0.37,
        "ra_pulse": -207.0,  # East is negative
        "dec_pulse": 50.0,
        "snr": 50.55,
        "mass": 5318.0,
        "hfd": 3.55,
    }
    assert feed(d, {**STEP, "ErrorCode": 1})[0].data["error"] == 1


def test_state_is_tracked_from_events_and_sent_only_on_change():
    d = Phd2Decoder()
    out = feed(
        d,
        {"Event": "AppState", "State": "Stopped"},
        {"Event": "LoopingExposures", "Frame": 1},
        {"Event": "LoopingExposures", "Frame": 2},
        {"Event": "StartCalibration", "Mount": "m"},
        {"Event": "CalibrationFailed", "Mount": "m", "Reason": "star did not move enough"},
        {"Event": "StartGuiding"},
        STEP,
        STEP,
        {
            "Event": "StarLost",
            "Frame": 9,
            "SNR": 2.1,
            "StarMass": 80,
            "ErrorCode": 2,
            "Status": "Star lost - low SNR",
        },
        STEP,
        {"Event": "Paused"},
        {"Event": "Resumed"},
        {"Event": "GuidingStopped"},
    )
    assert kinds(out) == [
        ("phd2.state", "stopped"),
        ("phd2.state", "looping"),
        ("phd2.state", "calibrating"),
        ("phd2.calibration", "started"),
        ("phd2.calibration", "failed"),
        ("phd2.state", "guiding"),
        ("phd2.step", None),
        ("phd2.step", None),
        ("phd2.state", "lost_lock"),
        ("phd2.star_lost", None),
        ("phd2.state", "guiding"),
        ("phd2.step", None),
        ("phd2.state", "paused"),
        ("phd2.state", "guiding"),
        ("phd2.state", "stopped"),
    ]
    assert out[4].data["reason"] == "star did not move enough"
    assert out[9].data["status"] == "Star lost - low SNR" and out[9].data["snr"] == 2.1


def test_settle_dither_alert_and_noise():
    d = Phd2Decoder()
    out = feed(
        d,
        {"Event": "SettleDone", "Status": 0, "TotalFrames": 3, "DroppedFrames": 0},
        {
            "Event": "SettleDone",
            "Status": 1,
            "Error": "timed-out waiting for guider to settle",
            "TotalFrames": 30,
            "DroppedFrames": 2,
        },
        {"Event": "GuidingDithered", "dx": 3.1, "dy": -2.0},
        {"Event": "Alert", "Msg": "Calibration lost", "Type": "warning"},
        {"Event": "Settling", "Distance": 1.2},
        {"Event": "ConfigurationChange"},
        {"Event": "SomethingNew"},
        {"jsonrpc": "2.0", "result": 0, "id": 1},
    )
    assert [e.data for e in out] == [
        {"ok": True, "frames": 3, "dropped": 0},
        {
            "ok": False,
            "frames": 30,
            "dropped": 2,
            "error": "timed-out waiting for guider to settle",
        },
        {"dx": 3.1, "dy": -2.0},
        {"message": "Calibration lost", "type": "warning"},
    ]


def test_lost_only_after_a_connection():
    d = Phd2Decoder()
    assert d.lost(1.0, "refused") == []
    feed(d, VERSION, {"Event": "AppState", "State": "Guiding"})
    (lost,) = d.lost(5.0, "PHD2 closed the connection")
    assert (lost.kind, lost.data) == ("phd2.lost", {"reason": "PHD2 closed the connection"})
    assert d.state is None and d.lost(6.0, "again") == []


def test_m31_guiding_story():
    _, records = replay.read(FIXTURE)
    events = list(replay.phd2_events(records))
    steps = [e for e in events if e.kind == "phd2.step"]
    assert len(steps) == 2595 and all("snr" in s.data for s in steps)
    states = [e.data["state"] for e in events if e.kind == "phd2.state"]
    # Calibrate and guide, stop for the flip, recalibrate and guide, pause for flats, stop.
    assert states == [
        "stopped",
        "looping",
        "calibrating",
        "guiding",
        "stopped",
        "looping",
        "calibrating",
        "guiding",
        "paused",
        "guiding",
        "stopped",
    ]
    assert [e.data["ok"] for e in events if e.kind == "phd2.settle"] == [True, True]
    alerts = [e.data["message"] for e in events if e.kind == "phd2.alert"]
    assert alerts[0].startswith("INDI camera disconnected")


def test_merged_replay_keeps_file_order():
    _, records = replay.read(FIXTURE)
    events = list(replay.events(records))
    assert {e.source for e in events} == {"ekos", "phd2"}
    assert all(a.ts <= b.ts + 1.0 for a, b in zip(events, events[1:], strict=False))


def test_adapter_reads_a_live_stream_and_reconnects():
    lines = [VERSION, {"Event": "AppState", "State": "Guiding"}, STEP]

    async def scenario():
        connections = 0

        async def phd2(reader, writer):
            nonlocal connections
            connections += 1
            for m in lines:
                writer.write(json.dumps(m).encode() + b"\r\n")
            writer.write(b"not json\n")  # skipped, not fatal
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(phd2, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        out = []
        adapter = Phd2Adapter(
            out.append,
            port=port,
            settings=lambda: UNKNOWN,
            clock=lambda: 1.0,
            backoff=(0.01,),
        )
        task = asyncio.create_task(adapter.run())
        while connections < 2 or len(out) < 9:
            await asyncio.sleep(0.01)
        task.cancel()
        server.close()
        return out

    out = asyncio.run(asyncio.wait_for(scenario(), 5))
    assert out[0].kind == "ekos.guider" and out[0].data["guider"] == "unknown"
    assert [e.kind for e in out[1:9]] == [
        "phd2.connected",
        "phd2.state",
        "phd2.step",
        "phd2.lost",
        "phd2.connected",
        "phd2.state",
        "phd2.step",
        "phd2.lost",
    ]


def test_adapter_waits_quietly_when_phd2_is_not_running(caplog):
    async def scenario():
        out = []
        # Port 9 (discard) on localhost: nothing listens, so connect is refused.
        adapter = Phd2Adapter(out.append, port=9, settings=lambda: UNKNOWN, backoff=(0.01,))
        task = asyncio.create_task(adapter.run())
        await asyncio.sleep(0.2)
        task.cancel()
        return out

    caplog.set_level("INFO", logger="town_crier.phd2.adapter")
    assert [e.kind for e in asyncio.run(scenario())] == ["ekos.guider"]
    assert caplog.text.count("PHD2 not reachable") == 1


def test_adapter_leaves_phd2_alone_while_ekos_uses_another_guider():
    async def scenario():
        connections = 0

        async def phd2(reader, writer):
            nonlocal connections
            connections += 1
            writer.write(json.dumps(VERSION).encode() + b"\n")
            await writer.drain()

        server = await asyncio.start_server(phd2, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        setting = {"now": GuiderSettings("internal")}
        out = []
        adapter = Phd2Adapter(
            out.append, settings=lambda: setting["now"], backoff=(0.01,), recheck=0.02
        )
        task = asyncio.create_task(adapter.run())
        await asyncio.sleep(0.2)
        before = connections
        # The user switches Ekos to PHD2 (on a non-default port) mid-night.
        setting["now"] = GuiderSettings("phd2", "127.0.0.1", port)
        while not any(e.kind == "phd2.connected" for e in out):
            await asyncio.sleep(0.01)
        task.cancel()
        server.close()
        return before, out

    before, out = asyncio.run(asyncio.wait_for(scenario(), 5))
    assert before == 0
    guider = [e.data["guider"] for e in out if e.kind == "ekos.guider"]
    assert guider == ["internal", "phd2"]
