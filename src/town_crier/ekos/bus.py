"""Find the session bus that KStars is on.

Order, per docs/design.md: ``DBUS_SESSION_BUS_ADDRESS``, then the systemd user bus at
``/run/user/<uid>/bus``, then the address in a running ``kstars`` process's environment. A
service started by systemd sees the user bus; desktops that start a private bus per session
(``dbus-launch``, some VNC setups) are only reachable through the last route. Inside a Flatpak
the ``kstars`` process may report a sandbox-only path; trying to connect weeds that out.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Candidate:
    source: str  # shown by `doctor`, e.g. "systemd user bus"
    address: str


def candidates(
    environ: dict[str, str] | None = None,
    uid: int | None = None,
    proc: Path = Path("/proc"),
    run_user: Path = Path("/run/user"),
) -> list[Candidate]:
    environ = dict(os.environ) if environ is None else environ
    uid = os.getuid() if uid is None else uid
    found: list[Candidate] = []

    def add(source: str, address: str | None) -> None:
        if address and all(c.address != address for c in found):
            found.append(Candidate(source, address))

    add("DBUS_SESSION_BUS_ADDRESS", environ.get("DBUS_SESSION_BUS_ADDRESS"))
    user_bus = run_user / str(uid) / "bus"
    if user_bus.exists():
        add("systemd user bus", f"unix:path={user_bus}")
    for pid, address in _kstars_bus_addresses(proc, uid):
        add(f"kstars process {pid}", address)
    return found


def _kstars_bus_addresses(proc: Path, uid: int) -> list[tuple[int, str]]:
    out = []
    try:
        entries = sorted((p for p in proc.iterdir() if p.name.isdigit()), key=lambda p: int(p.name))
    except OSError:
        return out
    for entry in entries:
        try:
            if (entry / "comm").read_text().strip() != "kstars" or entry.stat().st_uid != uid:
                continue
            env = (entry / "environ").read_bytes().split(b"\0")
        except OSError:  # gone, or not ours
            continue
        for item in env:
            if item.startswith(b"DBUS_SESSION_BUS_ADDRESS="):
                out.append((int(entry.name), item.split(b"=", 1)[1].decode(errors="replace")))
    return out
