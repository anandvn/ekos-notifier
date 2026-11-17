"""One Ekos Notifier per database: two would send every alert twice.

``serve()`` holds an exclusive ``flock`` on ``<database>.lock`` (with its PID inside) for as
long as it runs; the OS releases it if the process dies, so a crash never leaves it stuck.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path


class AlreadyRunning(RuntimeError):
    pass


def lock_path(db_path: str | Path) -> Path:
    return Path(db_path).with_suffix(".lock")


class InstanceLock:
    def __init__(self, db_path: str | Path) -> None:
        self.path = lock_path(db_path)
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pid = os.pread(fd, 32, 0).decode(errors="replace").strip() or "?"
            os.close(fd)
            raise AlreadyRunning(
                f"Ekos Notifier is already running (PID {pid}) on this database. Stop it first "
                "(Ctrl-C in its terminal, or: systemctl --user stop ekos-notifier)."
            ) from None
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"{os.getpid()}\n".encode(), 0)
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)  # closing drops the lock
            self._fd = None


def running_pid(db_path: str | Path) -> int | None:
    """The PID holding the lock, or None if nobody runs on this database."""
    lock = InstanceLock(db_path)
    try:
        lock.acquire()
    except AlreadyRunning:
        try:
            return int(lock.path.read_text().strip())
        except (OSError, ValueError):
            return -1
    lock.release()
    return None
