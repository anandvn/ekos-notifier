import os

from ekos_notifier.ekos.bus import Candidate, candidates

UID = os.getuid()


def fake_proc(tmp_path, procs):
    proc = tmp_path / "proc"
    for pid, (comm, env) in procs.items():
        d = proc / str(pid)
        d.mkdir(parents=True)
        (d / "comm").write_text(comm + "\n")
        (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in env.items()) + b"\0")
    return proc


def test_order_and_dedup(tmp_path):
    run_user = tmp_path / "run"
    (run_user / str(UID)).mkdir(parents=True)
    (run_user / str(UID) / "bus").touch()
    user_bus = f"unix:path={run_user / str(UID) / 'bus'}"
    proc = fake_proc(
        tmp_path,
        {
            10: ("bash", {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/ignored"}),
            20: ("kstars", {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/dbus-private"}),
            30: ("kstars", {"DBUS_SESSION_BUS_ADDRESS": user_bus}),  # duplicate of the user bus
        },
    )
    found = candidates({"DBUS_SESSION_BUS_ADDRESS": "unix:path=/env"}, UID, proc, run_user)
    assert found == [
        Candidate("DBUS_SESSION_BUS_ADDRESS", "unix:path=/env"),
        Candidate("systemd user bus", user_bus),
        Candidate("kstars process 20", "unix:path=/tmp/dbus-private"),
    ]


def test_nothing_found(tmp_path):
    assert candidates({}, UID, tmp_path / "noproc", tmp_path / "norun") == []
