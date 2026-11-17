"""Read replay recordings (``town-crier-replay/0``, see tests/fixtures/replay/README.md)."""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from town_crier.ekos.decode import decode_name_owner_changed, decode_signal
from town_crier.events import Event

FORMAT = "town-crier-replay/0"


def read(path: str | Path) -> tuple[dict[str, Any], Iterator[dict[str, Any]]]:
    """The meta record and an iterator over the other records, in file order."""
    f = gzip.open(path, "rt", encoding="utf-8")
    meta = json.loads(f.readline())
    if meta.get("format") != FORMAT:
        f.close()
        raise ValueError(f"{path}: not a {FORMAT} recording (format={meta.get('format')!r})")

    def records() -> Iterator[dict[str, Any]]:
        with f:
            for line in f:
                if line.strip():
                    yield json.loads(line)

    return meta, records()


def ekos_events(records: Iterable[dict[str, Any]]) -> Iterator[Event]:
    """Run recorded D-Bus signals through the same decoder the live adapter uses."""
    for rec in records:
        if rec.get("src") != "dbus":
            continue
        msg = rec.get("msg", {})
        if msg.get("type") != "signal":
            continue
        args = msg.get("payload", {}).get("data", [])
        if msg.get("member") == "NameOwnerChanged":
            yield from decode_name_owner_changed(rec["t"], args)
        elif (msg.get("path") or "").startswith("/KStars"):
            yield from decode_signal(rec["t"], msg["path"], msg.get("member", ""), args)
