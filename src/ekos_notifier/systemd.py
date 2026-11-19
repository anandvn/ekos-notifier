"""``ekos-notifier install-service``: a systemd *user* service, so Ekos Notifier starts with the
user's session and restarts if it fails.

The unit runs ``<this Python> -m ekos_notifier run``, the same installation that ran the
command (the install.sh venv, pipx, …). It's wanted by ``default.target``: it starts when the
user's systemd manager does, at login (or at boot with lingering enabled), and waits for KStars
by itself. StellarMate starts KStars as a user service too (``org.kde.kstars.service``), so they
share the user session. Logs go to the journal: ``journalctl --user -u ekos-notifier``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from ekos_notifier.i18n import _

UNIT = "ekos-notifier.service"

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def run(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(cmd), capture_output=True, text=True, check=False)


def unit_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def unit_text(python: str = sys.executable, config: str | None = None) -> str:
    exec_start = f"{python} -m ekos_notifier run"
    if config:
        exec_start += f" --config {config}"
    return f"""[Unit]
Description=Ekos Notifier: alerts, digests and session records for KStars/Ekos
Documentation=https://github.com/anandvn/ekos-notifier

[Service]
Type=simple
ExecStart={exec_start}
# Edits to the config apply by themselves; `systemctl --user reload` applies them at once.
ExecReload=/bin/kill -HUP $MAINPID
# SIGTERM stops it cleanly: everything queued is written before it exits.
Restart=on-failure
RestartSec=10
# 78: an error retrying can't fix (a database from a newer version): stop, don't loop.
RestartPreventExitStatus=78
TimeoutStopSec=30
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
"""


def service_pid(runner: Runner = run) -> int | None:
    """The running service's main process, or None when it isn't running."""
    result = runner(["systemctl", "--user", "show", "-p", "MainPID", "--value", UNIT])
    try:
        pid = int(result.stdout.strip())
    except ValueError:
        return None
    return pid or None


def install(
    start: bool = True,
    config: str | None = None,
    runner: Runner = run,
    *,
    restart: bool = False,
) -> list[str]:
    """Write the unit, enable it (and start it, or with ``restart`` restart the running service
    so it runs the new unit and code). Returns lines to show; raises on failure."""
    path = unit_dir() / UNIT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit_text(config=config))
    lines = [_("Wrote {path}").format(path=path)]
    _systemctl(runner, "daemon-reload")
    if restart:
        _systemctl(runner, "enable", UNIT)
        _systemctl(runner, "restart", UNIT)
        lines.append(
            _("Enabled: it starts with your user session. Restarted it, so it runs this version.")
        )
    else:
        _systemctl(runner, "enable", *(["--now"] if start else []), UNIT)
        lines.append(
            _("Enabled: it starts with your user session and is running now.")
            if start
            else _("Enabled: it starts with your user session.")
        )
    linger = runner(["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger"])
    if linger.returncode == 0 and linger.stdout.strip() == "Linger=no":
        lines.append(
            _(
                "It starts when you log in (as KStars does on StellarMate). To start it at boot"
                " without a login, run: loginctl enable-linger"
            )
        )
    lines.append(_("Logs: journalctl --user -u ekos-notifier -f"))
    return lines


def uninstall(runner: Runner = run) -> list[str]:
    path = unit_dir() / UNIT
    if not path.exists():
        return [_("No {unit} installed.").format(unit=UNIT)]
    _systemctl(runner, "disable", "--now", UNIT)
    path.unlink()
    _systemctl(runner, "daemon-reload")
    return [_("Stopped, disabled and removed {path}.").format(path=path)]


def _systemctl(runner: Runner, *args: str) -> None:
    result = runner(["systemctl", "--user", *args])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            _("systemctl --user {command} failed: {detail}").format(
                command=" ".join(args), detail=detail
            )
        )
