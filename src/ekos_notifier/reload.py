"""Reloading the config while the service runs (docs/index.md, "Your config file").

``ConfigWatcher`` checks the config file every ``POLL`` seconds and reloads it once a change has
settled (the file unchanged for one more check, so an editor's half-written save isn't read), or
at once on ``trigger()`` (SIGHUP: ``systemctl --user reload ekos-notifier``). A config with an
error is reported and ignored: the service carries on with the settings it had.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from pathlib import Path

from ekos_notifier import config as config_module
from ekos_notifier.config import Config, ConfigError

log = logging.getLogger(__name__)

POLL = 2.0

Stat = tuple[int, int, int] | None


def _stat(path: Path) -> Stat:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


class ConfigWatcher:
    def __init__(
        self,
        path: Path,
        apply: Callable[[Config], None],
        failed: Callable[[str], None] = lambda error: None,
        *,
        poll: float = POLL,
    ) -> None:
        self.path = path
        self.apply = apply
        self.failed = failed
        self.poll = poll
        self._now = asyncio.Event()

    def trigger(self) -> None:
        """Reload now (SIGHUP)."""
        self._now.set()

    async def run(self) -> None:
        seen, pending = _stat(self.path), None
        while True:
            try:
                await asyncio.wait_for(self._now.wait(), self.poll)
            except asyncio.TimeoutError:
                pass
            forced = self._now.is_set()
            self._now.clear()
            stat = _stat(self.path)
            if forced:
                seen, pending = stat, None
                self.reload()
            elif stat != seen:
                if stat == pending:  # unchanged since the last check: the save is done
                    seen, pending = stat, None
                    self.reload()
                else:
                    pending = stat

    def reload(self) -> None:
        if not self.path.exists():
            log.warning("config: %s is gone; keeping the current settings", self.path)
            return
        try:
            new = config_module.load(self.path)
        except ConfigError as exc:
            log.error("config not reloaded, keeping the current settings: %s", exc)
            self.failed(str(exc))
            return
        log.info("config reloaded from %s", self.path)
        for warning in new.warnings:
            log.warning("config: %s", warning)
        self.apply(new)
