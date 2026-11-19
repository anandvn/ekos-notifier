import asyncio
import json
import os
import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

from test_indi import WEATHER, connection, driver, numbers

from ekos_notifier import doctor, systemd
from ekos_notifier.config import load
from ekos_notifier.doctor import FAIL, NOTE, OK, WARN, Check, EkosState
from ekos_notifier.ekos.bus import Candidate, KStarsProcess
from ekos_notifier.ekos.settings import GuiderSettings
from ekos_notifier.store import MIGRATIONS, Store


def statuses(checks):
    return [c.status for c in checks]


def text(checks):
    return "\n".join(f"{c.text} {c.fix or ''}" for c in checks)


# -- install --------------------------------------------------------------------------------


def test_install_flags_a_venv_whose_python_is_gone(tmp_path):
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").symlink_to(tmp_path / "python3.13")  # removed by an OS update
    checks = doctor.check_install(which=lambda _: "/x/ekos-notifier", venv=venv)
    assert statuses(checks) == [OK, FAIL]
    assert "install.sh" in checks[1].fix


def test_install_notes_a_command_not_on_path(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/bash")
    checks = doctor.check_install(which=lambda _: None, venv=tmp_path / "none")
    assert statuses(checks) == [OK, NOTE]
    assert "~/.bashrc" in checks[1].fix


# -- config ---------------------------------------------------------------------------------


def test_no_config_file_is_a_warning():
    cfg, checks = doctor.check_config()
    assert cfg is not None and statuses(checks) == [WARN]
    assert "config init" in checks[0].fix


def test_config_with_an_error_fails(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text("notify: [unclosed\n")
    cfg, checks = doctor.check_config(str(f))
    assert cfg is None and statuses(checks) == [FAIL]


def test_config_notifiers_are_listed_with_keys_hidden_and_a_readable_file_flagged(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text('notify:\n  - "pover://usersecretkey@appsecrettoken"\n  - "nope://x"\n')
    f.chmod(0o644)
    _, checks = doctor.check_config(str(f))
    assert statuses(checks) == [OK, FAIL, OK, NOTE, WARN]
    assert "secret" not in text(checks)
    assert "?" not in checks[2].text  # Apprise's query string left out
    assert checks[4].fix == f"chmod 600 {f}"
    f.chmod(0o600)
    _, checks = doctor.check_config(str(f))
    assert WARN not in statuses(checks)


def test_config_without_notifiers_warns(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text("power:\n  battery: agm_12v\n")
    _, checks = doctor.check_config(str(f))
    assert statuses(checks) == [OK, WARN]


# -- service --------------------------------------------------------------------------------


def runner(active="active", pid="42"):
    def run(cmd):
        if "is-active" in cmd:
            return SimpleNamespace(stdout=active + "\n")
        if "MainPID" in cmd:
            return SimpleNamespace(stdout=pid + "\n")
        raise AssertionError(cmd)

    return run


def unit(tmp_path, python, text=None):
    d = tmp_path / "units"
    d.mkdir(exist_ok=True)
    (d / systemd.UNIT).write_text(text or systemd.unit_text(python=python))
    return d


def test_service_not_installed(tmp_path):
    checks = doctor.check_service(
        tmp_path / "db", runner(), tmp_path / "units", running_pid=lambda _: 7
    )
    assert statuses(checks) == [WARN, NOTE]
    assert "install-service" in checks[0].fix and "PID 7" in checks[1].text


def test_service_running(tmp_path):
    units = unit(tmp_path, os.path.realpath(os.sys.executable))
    checks = doctor.check_service(tmp_path / "db", runner(), units, running_pid=lambda _: 42)
    assert checks == [Check(OK, "running (PID 42)")]


def test_service_whose_python_is_gone(tmp_path):
    units = unit(tmp_path, str(tmp_path / "venv/bin/python"))
    checks = doctor.check_service(tmp_path / "db", runner("failed", "0"), units, lambda _: None)
    assert statuses(checks) == [FAIL, FAIL]
    assert "Python" in checks[0].text and "journalctl" in checks[1].fix


def test_an_old_unit_is_flagged_and_a_terminal_copy_noted(tmp_path):
    python = os.path.realpath(os.sys.executable)
    old = systemd.unit_text(python=python).replace("ExecReload=/bin/kill -HUP $MAINPID\n", "")
    units = unit(tmp_path, python, old)
    checks = doctor.check_service(tmp_path / "db", runner("inactive", "0"), units, lambda _: 9)
    assert statuses(checks) == [WARN, WARN, NOTE]
    assert checks[0].fix == "ekos-notifier install-service"
    assert "inactive" in checks[1].text and "PID 9" in checks[2].text


def test_a_unit_with_a_config_matches_its_own_text(tmp_path):
    python = os.path.realpath(os.sys.executable)
    units = unit(tmp_path, python, systemd.unit_text(python=python, config="/my/c.yaml"))
    checks = doctor.check_service(tmp_path / "db", runner(), units, lambda _: 42)
    assert statuses(checks) == [OK]


# -- database -------------------------------------------------------------------------------


def test_database(tmp_path):
    db = tmp_path / "e.db"
    assert statuses(doctor.check_database(db)) == [NOTE]
    Store(db).close()
    checks = doctor.check_database(db)
    assert statuses(checks) == [OK] and "0 sessions" in checks[0].text
    con = sqlite3.connect(db)
    con.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
    con.close()
    checks = doctor.check_database(db)
    assert statuses(checks) == [FAIL] and "newer" in checks[0].text


# -- D-Bus and KStars -----------------------------------------------------------------------


def test_no_session_bus():
    state, checks = asyncio.run(doctor.check_dbus(find=lambda: []))
    assert state == EkosState() and statuses(checks) == [FAIL]


def test_unreachable_session_bus(tmp_path):
    bad = [Candidate("systemd user bus", f"unix:path={tmp_path}/bus")]
    state, checks = asyncio.run(doctor.check_dbus(find=lambda: bad))
    assert not state.kstars and statuses(checks) == [FAIL]
    assert "systemd user bus" in checks[0].text


def flatpak(tmp_path, context):
    root = tmp_path / "root"
    root.mkdir()
    (root / ".flatpak-info").write_text("[Application]\nname=org.kde.kstars\n\n" + context)
    return KStarsProcess(1352, {}, root)


def test_kstars_native_and_flatpak(tmp_path):
    assert doctor.check_kstars(lambda: []) == []
    native = KStarsProcess(10, {}, tmp_path / "nothing")
    assert statuses(doctor.check_kstars(lambda: [native])) == [OK]
    good = flatpak(
        tmp_path,
        "[Context]\nshared=ipc;network;\nsockets=x11;session-bus;\nfilesystems=host;\n",
    )
    assert statuses(doctor.check_kstars(lambda: [good])) == [OK]
    assert statuses(doctor.check_kstars(lambda: [native, good])) == [WARN, OK, OK]


def test_a_flatpak_without_network_or_home(tmp_path):
    k = flatpak(tmp_path, "[Context]\nshared=ipc;\nsockets=session-bus;\nfilesystems=~/.indi;\n")
    checks = doctor.check_kstars(lambda: [k])
    assert statuses(checks) == [FAIL, NOTE]
    assert "network" in checks[0].text and "--share=network" in checks[0].fix


# -- INDI -----------------------------------------------------------------------------------


def elements(*xml):
    async def collect(host, port, seconds):
        assert (host, port) == ("localhost", 7624)
        return [ET.fromstring(x) for x in xml]

    return collect


def test_indi_lists_devices_and_what_is_recognised():
    checks = asyncio.run(
        doctor.check_indi(None, EkosState(True, "success"), lambda: None, elements(*WEATHER))
    )
    assert statuses(checks) == [OK, OK]
    assert "1 device" in checks[0].text
    assert checks[1].text == "recognised: Weather (dew point, humidity, temperature)"


def test_indi_uses_the_config_mappings(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text('indi:\n  metrics:\n    power.voltage: "Box / POWER_SENSORS.SENSOR_VOLTAGE"\n')
    box = [
        connection("Box", True),
        driver("Box", "indi_mybox", 0),
        numbers("Box", "POWER_SENSORS", "def", SENSOR_VOLTAGE=12.6),
    ]
    checks = asyncio.run(doctor.check_indi(load(f), EkosState(), lambda: None, elements(*box)))
    assert checks[1].text == "recognised: Box (battery voltage)"
    checks = asyncio.run(doctor.check_indi(None, EkosState(), lambda: None, elements(*box)))
    assert statuses(checks) == [OK, NOTE] and "indi-dump" in checks[1].fix


def test_indi_unreachable():
    async def refused(host, port, seconds):
        raise ConnectionRefusedError

    idle = asyncio.run(doctor.check_indi(None, EkosState(True), lambda: None, refused))
    assert statuses(idle) == [NOTE]
    running = asyncio.run(
        doctor.check_indi(None, EkosState(True, "success"), lambda: ("rig", 7625), refused)
    )
    assert statuses(running) == [FAIL] and "rig:7625" in running[0].text


# -- PHD2 -----------------------------------------------------------------------------------


def test_phd2_version_from_a_fake_server():
    async def scenario():
        async def serve(reader, writer):
            writer.write(b"garbage\n")
            writer.write(json.dumps({"Event": "Version", "PHDVersion": "2.6.14"}).encode() + b"\n")
            await writer.drain()

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            return port, await doctor.check_phd2(lambda: GuiderSettings("phd2", "127.0.0.1", port))
        finally:
            server.close()

    port, checks = asyncio.run(scenario())
    assert checks == [Check(OK, f"PHD2 2.6.14 on 127.0.0.1:{port}")]


def test_phd2_checks():
    async def refused(host, port):
        raise ConnectionRefusedError

    internal = asyncio.run(doctor.check_phd2(lambda: GuiderSettings("internal"), refused))
    assert internal == [Check(OK, "Ekos uses its internal guider: PHD2 isn't needed")]
    wanted = asyncio.run(doctor.check_phd2(lambda: GuiderSettings("phd2"), refused))
    assert statuses(wanted) == [WARN] and "Enable Server" in wanted[0].fix
    unknown = asyncio.run(doctor.check_phd2(lambda: GuiderSettings("unknown"), refused))
    assert statuses(unknown) == [NOTE]


# -- clock and disk -------------------------------------------------------------------------


def test_clock():
    def answer(stdout):
        return lambda cmd: SimpleNamespace(stdout=stdout)

    def missing(cmd):
        raise FileNotFoundError

    assert statuses(doctor.check_clock(answer("yes\n"))) == [OK]
    assert statuses(doctor.check_clock(answer("no\n"))) == [WARN]
    assert statuses(doctor.check_clock(answer(""))) == [NOTE]
    assert statuses(doctor.check_clock(missing)) == [NOTE]


def test_disk(tmp_path):
    cfg = load(None)
    ok = doctor.check_disk(cfg, tmp_path / "db", free=lambda p: 50e9, folder="/frames")
    assert ok == [Check(OK, "50 GB free in /frames, where Ekos saved its last frame")]
    low = doctor.check_disk(cfg, tmp_path / "db", free=lambda p: 3e9)
    assert statuses(low) == [WARN] and "home folder" in low[0].text


def test_the_last_frame_folder_comes_from_the_database(tmp_path):
    db = tmp_path / "e.db"
    assert doctor.last_frame_folder(db) is None
    Store(db).close()
    con = sqlite3.connect(db)
    con.execute("INSERT INTO events (id, ts, kind, data, source) VALUES (1, 0, 'x', '{}', 't')")
    for ts, file in ((1, "/a/old.fits"), (2, "/b/new.fits"), (3, None)):
        con.execute("INSERT INTO frames (event_id, ts, file) VALUES (1, ?, ?)", (ts, file))
    con.commit()
    con.close()
    assert doctor.last_frame_folder(db) == str(Path("/b"))


# -- the whole run --------------------------------------------------------------------------


def test_run_prints_each_area_and_counts_problems(monkeypatch, tmp_path):
    async def dbus():
        return EkosState(), [Check(WARN, "KStars isn't running", "start KStars")]

    async def nothing(*args):
        return []

    monkeypatch.setattr(doctor, "check_install", lambda: [Check(OK, "installed")])
    monkeypatch.setattr(doctor, "check_config", lambda p: (None, [Check(FAIL, "bad", "fix it")]))
    monkeypatch.setattr(doctor, "check_service", lambda db: [Check(OK, "running")])
    monkeypatch.setattr(doctor, "check_database", lambda db: [])
    monkeypatch.setattr(doctor, "check_dbus", dbus)
    monkeypatch.setattr(doctor, "check_kstars", lambda: [])
    monkeypatch.setattr(doctor, "check_indi", nothing)
    monkeypatch.setattr(doctor, "check_phd2", nothing)
    monkeypatch.setattr(doctor, "check_clock", lambda: [Check(OK, "synced")])
    monkeypatch.setattr(doctor, "check_disk", lambda cfg, db: [Check(OK, "space")])
    lines = []
    assert asyncio.run(doctor.run(None, tmp_path / "db", lines.append)) == 1
    out = "\n".join(lines)
    assert "Database" not in out  # an area with nothing to say is left out
    assert "  FAIL  bad\n        → fix it" in out
    assert lines[-1] == "1 problem and 1 warning: the → lines say what to do."


def test_doctor_creates_no_lock_file(tmp_path):
    doctor.check_service(tmp_path / "e.db", runner(), tmp_path / "units")
    assert list(tmp_path.iterdir()) == []


def test_kstars_in_another_language(tmp_path, monkeypatch):
    from test_translation import write_mo

    from ekos_notifier.ekos import translation

    german = translation.NumberFormat(",", ".")
    monkeypatch.setattr(translation, "number_format", lambda name: german)

    root = tmp_path / "root"
    write_mo(
        root / "usr/share/locale/de/LC_MESSAGES/kstars.mo",
        {"Solver Failed.": "Lösung fehlgeschlagen."},
    )
    k = KStarsProcess(10, {"LANG": "de_DE.UTF-8"}, root)
    checks = doctor.check_kstars(lambda: [k])
    assert statuses(checks) == [OK, OK, OK] and "'de'" in checks[0].text
    assert "numbers in 'de_DE.UTF-8' (decimal ',')" in checks[1].text
    broken = tmp_path / "broken"
    (broken / "usr/share/locale/de/LC_MESSAGES").mkdir(parents=True)
    (broken / "usr/share/locale/de/LC_MESSAGES/kstars.mo").write_bytes(b"not a catalog")
    checks = doctor.check_kstars(lambda: [KStarsProcess(10, {"LANG": "de_DE.UTF-8"}, broken)])
    assert statuses(checks) == [WARN, OK, OK]


def test_kstars_number_locale_unknown_here(monkeypatch):
    from ekos_notifier.ekos import translation

    monkeypatch.setattr(translation, "number_format", lambda name: None)
    checks = doctor.check_numbers(KStarsProcess(10, {"LANG": "fr_FR.UTF-8"}, Path("/")))
    assert statuses(checks) == [WARN] and "fr_FR" in checks[0].fix
