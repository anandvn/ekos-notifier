"""Disk low (docs/design.md, "Alerts", High).

During a session, the free space where Ekos saves frames (the folder of the last saved frame, else
the home folder) is checked every ``CHECK_EVERY`` seconds and after each frame. Under
``alerts.disk_low_gb`` (default 10 GB) it sends **Disk low**, with how many more frames fit at
the size of the recent ones and how long that is at the session's pace. Once until there's
``RECOVERED_GB`` more than the limit again (files moved off), which sends **Disk space OK**
(Normal, immediate).

The checks go through ``free_bytes`` and ``file_size`` so replays and tests don't touch the disk.
Ekos's paths are the ones KStars sees; under Flatpak with home access (StellarMate) they're the
same on the host, and a path that doesn't exist is checked at its nearest existing folder.
"""

from __future__ import annotations

import os
import shutil
from collections import deque
from collections.abc import Callable, Iterable
from pathlib import Path

from ekos_notifier.alerts import SessionRule, make_alert
from ekos_notifier.events import Event
from ekos_notifier.i18n import _, ngettext

CHECK_EVERY = 60.0
RECOVERED_GB = 1.0
GB = 1e9


def free_bytes(path: str) -> int | None:
    """Free space for the user on the filesystem holding ``path`` (or its nearest parent)."""
    p = Path(path)
    for candidate in (p, *p.parents):
        try:
            if candidate.exists():
                return shutil.disk_usage(candidate).free
        except OSError:
            return None
    return None


def file_size(path: str) -> int | None:
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def _hours(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    if minutes >= 60:
        return _("{hours} h {minutes} min").format(
            hours=minutes // 60, minutes=f"{minutes % 60:02d}"
        )
    return _("{minutes} min").format(minutes=minutes)


class DiskRule(SessionRule):
    SETTINGS = ("low",)

    def __init__(
        self,
        low_gb: float = 10.0,
        *,
        free: Callable[[str], int | None] = free_bytes,
        size: Callable[[str], int | None] = file_size,
        home: str | None = None,
    ) -> None:
        super().__init__()
        self.low = low_gb * GB
        self._free = free
        self._size = size
        self._folder = home if home is not None else str(Path.home())
        self._checked: float | None = None
        self._sizes: deque[int] = deque(maxlen=10)
        self._frames: deque[float] = deque(maxlen=20)  # when recent frames were saved
        self._raised = False

    def resume(self, session_id: str | None, alerts: Iterable[Event] = ()) -> None:
        super().resume(session_id, alerts)
        for a in alerts:
            if a.data.get("rule") == "disk_low":
                self._raised = True
            elif a.data.get("rule") == "disk_ok":
                self._raised = False

    def on_session_closed(self) -> None:
        self._checked = None
        self._frames.clear()

    def on_event(self, event: Event) -> list[Event]:
        if event.kind == "capture.frame" and event.data.get("file"):
            path = str(event.data["file"])
            self._folder = str(Path(path).parent)
            if (size := self._size(path)) is not None and size > 0:
                self._sizes.append(size)
            if event.data.get("type") == "light":
                self._frames.append(event.ts)
            self._checked = None  # check now
        return []

    def on_tick(self, now: float) -> list[Event]:
        if self.low <= 0 or (self._checked is not None and now - self._checked < CHECK_EVERY):
            return []
        self._checked = now
        free = self._free(self._folder)
        if free is None:
            return []
        if not self._raised and free < self.low:
            self._raised = True
            return [self._alert(now, free)]
        if self._raised and free >= self.low + RECOVERED_GB * GB:
            self._raised = False
            body = _("{gb} GB free on {folder}.").format(gb=f"{free / GB:.1f}", folder=self._folder)
            return [
                make_alert(
                    now, self.session, "disk_ok", "normal", _("Disk space OK"), body, immediate=True
                )
            ]
        return []

    def preflight(self, now: float) -> list[tuple[bool, str]]:
        if self.low <= 0 or (free := self._free(self._folder)) is None:
            return []
        if free < self.low:
            text = _("{gb} GB free on {folder}, under {limit} GB").format(
                gb=f"{free / GB:.1f}", folder=self._folder, limit=f"{self.low / GB:g}"
            )
            return [(False, text)]
        return [(True, _("{gb} GB free").format(gb=f"{free / GB:.0f}"))]

    def _alert(self, now: float, free: int) -> Event:
        lines = [
            _("{gb} GB free on {folder} (limit {limit} GB).").format(
                gb=f"{free / GB:.1f}", folder=self._folder, limit=f"{self.low / GB:g}"
            )
        ]
        if self._sizes:
            size = sum(self._sizes) / len(self._sizes)
            frames = int(free // size)
            values = {"frames": frames, "mb": f"{size / 1e6:.0f}"}
            if len(self._frames) >= 2:
                pace = (self._frames[-1] - self._frames[0]) / (len(self._frames) - 1)
                room = ngettext(
                    "Room for about {frames} more frame at {mb} MB, {duration} at tonight's pace.",
                    "Room for about {frames} more frames at {mb} MB, {duration} at tonight's pace.",
                    frames,
                ).format(**values, duration=_hours(frames * pace))
            else:
                room = ngettext(
                    "Room for about {frames} more frame at {mb} MB.",
                    "Room for about {frames} more frames at {mb} MB.",
                    frames,
                ).format(**values)
            lines.append(room)
        lines.append(_("Move files off the disk, or Ekos will stop saving frames."))
        return make_alert(now, self.session, "disk_low", "high", _("Disk low"), "\n".join(lines))
