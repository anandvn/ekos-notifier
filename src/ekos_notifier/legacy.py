"""Carry data over from the project's old name, Town Crier (renamed 2026-11-17).

Before the rename, the config lived in ``~/.config/town-crier/`` and the database in
``~/.local/share/town-crier/town-crier.db`` (plus its ``-wal`` and ``-shm`` files). ``migrate()``
moves them to the new names once, when the new folder doesn't exist yet. The database is moved
only when no process has it open (an old service still running), since its WAL files must move
with it. Nothing is ever overwritten.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from ekos_notifier.i18n import _

OLD, NEW = "town-crier", "ekos-notifier"
DB_SUFFIXES = ("", "-wal", "-shm", "-journal")


def _base(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or str(Path.home() / default))


def _open_by_any_process(folder: Path, proc: Path) -> bool:
    folder = folder.resolve()
    try:
        pids = [p for p in proc.iterdir() if p.name.isdigit()]
    except OSError:
        return False
    for pid in pids:
        try:
            fds = list((pid / "fd").iterdir())
        except OSError:
            continue  # gone, or not ours
        for fd in fds:
            try:
                if Path(os.readlink(fd)).parent == folder:
                    return True
            except OSError:
                continue
    return False


def migrate(say: Callable[[str], None] = print, proc: Path = Path("/proc")) -> None:
    config = _base("XDG_CONFIG_HOME", ".config")
    if (config / OLD).is_dir() and not (config / NEW).exists():
        (config / OLD).rename(config / NEW)
        say(
            _("Moved {config} to {config2} (Town Crier is now Ekos Notifier).").format(
                config=config / OLD, config2=config / NEW
            )
        )

    data = _base("XDG_DATA_HOME", ".local/share")
    old, new = data / OLD, data / NEW
    if not old.is_dir() or new.exists():
        return
    if _open_by_any_process(old, proc):
        say(
            _(
                "Not moving {old} yet: an old Town Crier process still has its "
                "database open. Stop it, then run Ekos Notifier again."
            ).format(old=old)
        )
        return
    old.rename(new)
    for suffix in DB_SUFFIXES:
        f = new / f"{OLD}.db{suffix}"
        if f.exists():
            f.rename(new / f"{NEW}.db{suffix}")
    say(_("Moved {old} to {new} (Town Crier is now Ekos Notifier).").format(old=old, new=new))
