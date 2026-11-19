"""Read replay recordings (``ekos-notifier-replay/0``, see tests/fixtures/replay/README.md)."""

from __future__ import annotations

import gzip
import json
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from ekos_notifier.ekos import translation
from ekos_notifier.ekos.decode import decode_name_owner_changed, decode_signal
from ekos_notifier.events import Event
from ekos_notifier.indi.decode import IndiDecoder
from ekos_notifier.indi.presets import load_builtin
from ekos_notifier.phd2.decode import Phd2Decoder

FORMAT = "ekos-notifier-replay/0"
LEGACY_FORMATS = {"town-crier-replay/0"}  # recordings made before the rename


def read(path: str | Path) -> tuple[dict[str, Any], Iterator[dict[str, Any]]]:
    """The meta record and an iterator over the other records, in file order."""
    f = gzip.open(path, "rt", encoding="utf-8")
    meta = json.loads(f.readline())
    if meta.get("format") != FORMAT and meta.get("format") not in LEGACY_FORMATS:
        f.close()
        raise ValueError(f"{path}: not a {FORMAT} recording (format={meta.get('format')!r})")

    def records() -> Iterator[dict[str, Any]]:
        with f:
            for line in f:
                if line.strip():
                    yield json.loads(line)

    return meta, records()


def events(
    records: Iterable[dict[str, Any]],
    indi: IndiDecoder | None = None,
    translator: translation.BackTranslator | None = None,
    numbers: translation.NumberFormat = translation.ENGLISH,
) -> Iterator[Event]:
    """Every source in the recording, in file order, through the live adapters' decoders.
    INDI elements use the built-in presets unless ``indi`` (a decoder) is given. Ekos's log goes
    through ``translator`` as the live adapter's does, for a recording made in another language
    (its meta ``language``; see ``translation.for_language``), and carries the number format
    KStars wrote it in (``numbers``: the meta's, see ``meta_numbers``)."""
    phd2 = Phd2Decoder()
    indi = indi or IndiDecoder(load_builtin())
    for rec in records:
        src = rec.get("src")
        if src == "dbus":
            for e in _dbus(rec):
                yield translation.to_english(translation.with_numbers(e, numbers), translator)
        elif src == "indi" and rec.get("xml"):
            try:
                element = ET.fromstring(rec["xml"])
            except ET.ParseError:
                continue
            yield from indi.feed(rec["t"], element)
        elif src == "phd2" and isinstance(rec.get("msg"), dict):
            yield from phd2.feed(rec["t"], rec["msg"])
        elif src == "recorder" and rec.get("stream") == "phd2" and rec.get("event") != "connected":
            yield from phd2.lost(rec["t"], rec.get("error") or "PHD2 closed the connection")


def meta_numbers(meta: dict[str, Any]) -> translation.NumberFormat:
    """The number format a recording's KStars wrote in (English for recordings without one)."""
    n = meta.get("numbers")
    if isinstance(n, dict) and n.get("decimal"):
        return translation.NumberFormat(n["decimal"], n.get("group", ""))
    return translation.ENGLISH


def ekos_events(records: Iterable[dict[str, Any]]) -> Iterator[Event]:
    """Run recorded D-Bus signals through the same decoder the live adapter uses."""
    for rec in records:
        if rec.get("src") == "dbus":
            yield from _dbus(rec)


def phd2_events(records: Iterable[dict[str, Any]]) -> Iterator[Event]:
    """Run recorded PHD2 messages through the same decoder the live adapter uses."""
    return (e for e in events(records) if e.source == "phd2")


def _dbus(rec: dict[str, Any]) -> Iterator[Event]:
    msg = rec.get("msg", {})
    if msg.get("type") != "signal":
        return
    args = msg.get("payload", {}).get("data", [])
    if msg.get("member") == "NameOwnerChanged":
        yield from decode_name_owner_changed(rec["t"], args)
    elif (msg.get("path") or "").startswith("/KStars"):
        yield from decode_signal(rec["t"], msg["path"], msg.get("member", ""), args)
