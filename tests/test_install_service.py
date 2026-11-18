import asyncio
import subprocess
import sys

import pytest

from ekos_notifier import systemd
from ekos_notifier.lock import AlreadyRunning, InstanceLock, running_pid
from ekos_notifier.service import serve


class FakeSystemctl:
    def __init__(self, linger="Linger=no", fail=None):
        self.calls = []
        self.linger = linger
        self.fail = fail

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        if self.fail and self.fail in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "Failed to connect to bus")
        out = self.linger if cmd[0] == "loginctl" else ""
        return subprocess.CompletedProcess(cmd, 0, out, "")


def test_unit_runs_this_python_and_is_wanted_by_the_user_session():
    text = systemd.unit_text(python="/opt/venv/bin/python3", config="/home/u/my.yaml")
    assert "ExecStart=/opt/venv/bin/python3 -m ekos_notifier run --config /home/u/my.yaml" in text
    assert "Restart=on-failure" in text and "WantedBy=default.target" in text


def test_install_enables_and_starts_and_mentions_linger(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    fake = FakeSystemctl()
    lines = systemd.install(runner=fake)
    unit = tmp_path / "systemd/user/ekos-notifier.service"
    assert unit.exists() and f"{sys.executable} -m ekos_notifier run" in unit.read_text()
    assert ["systemctl", "--user", "daemon-reload"] in fake.calls
    assert ["systemctl", "--user", "enable", "--now", "ekos-notifier.service"] in fake.calls
    assert any("loginctl enable-linger" in line for line in lines)
    assert not any(
        "enable-linger" in line for line in systemd.install(runner=FakeSystemctl("Linger=yes"))
    )


def test_install_failure_is_reported_and_uninstall_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with pytest.raises(RuntimeError, match="Failed to connect to bus"):
        systemd.install(runner=FakeSystemctl(fail="enable"))
    fake = FakeSystemctl()
    assert systemd.uninstall(runner=fake)[0].startswith("Stopped, disabled and removed")
    assert ["systemctl", "--user", "disable", "--now", "ekos-notifier.service"] in fake.calls
    assert not (tmp_path / "systemd/user/ekos-notifier.service").exists()
    assert systemd.uninstall(runner=fake) == ["No ekos-notifier.service installed."]


def test_one_instance_per_database(tmp_path):
    db = tmp_path / "tc.db"
    first = InstanceLock(db)
    first.acquire()
    assert running_pid(db) is not None
    with pytest.raises(AlreadyRunning, match="already running"):
        InstanceLock(db).acquire()
    first.release()
    assert running_pid(db) is None


def test_serve_refuses_a_second_copy(tmp_path):
    db = tmp_path / "tc.db"
    held = InstanceLock(db)
    held.acquire()
    with pytest.raises(AlreadyRunning):
        asyncio.run(serve(db, sources=[], stop=asyncio.Event()))
    held.release()


def test_python_dash_m_runs_the_cli():
    out = subprocess.run(
        [sys.executable, "-m", "ekos_notifier", "--version"], capture_output=True, text=True
    )
    assert out.returncode == 0 and out.stdout.startswith("ekos-notifier ")


def test_service_pid_and_restarting_the_running_service(tmp_path, monkeypatch):
    class Show(FakeSystemctl):
        def __init__(self, pid):
            super().__init__()
            self.pid = pid

        def __call__(self, cmd):
            if "show" in cmd:
                self.calls.append(list(cmd))
                return subprocess.CompletedProcess(cmd, 0, f"{self.pid}\n", "")
            return super().__call__(cmd)

    assert systemd.service_pid(Show(4242)) == 4242
    assert systemd.service_pid(Show(0)) is None  # not running
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    fake = FakeSystemctl()
    lines = systemd.install(runner=fake, restart=True)
    assert ["systemctl", "--user", "restart", "ekos-notifier.service"] in fake.calls
    assert ["systemctl", "--user", "enable", "ekos-notifier.service"] in fake.calls
    assert any("Restarted it" in line for line in lines)


@pytest.mark.parametrize(
    ("running", "service", "expect", "warned"),
    [
        (4242, 4242, {"restart": True}, False),  # the service itself: restart it, no warning
        (4242, None, {"start": False}, True),  # another copy, in a terminal: warn
        (None, None, {"start": True}, False),  # nothing running: start it
    ],
)
def test_cli_tells_the_service_from_a_terminal_copy(
    monkeypatch, capsys, running, service, expect, warned
):
    from ekos_notifier import lock
    from ekos_notifier.cli import main

    calls = []
    monkeypatch.setattr(lock, "running_pid", lambda path: running)
    monkeypatch.setattr(systemd, "service_pid", lambda: service)
    monkeypatch.setattr(systemd, "install", lambda **kw: calls.append(kw) or ["ok"])
    assert main(["install-service"]) == 0
    assert len(calls) == 1 and expect.items() <= calls[0].items()
    assert ("probably in a terminal" in capsys.readouterr().out) is warned
