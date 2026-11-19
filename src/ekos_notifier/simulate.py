"""``ekos-notifier simulate``: replay a night and show the alerts it would send, to tune alerts in
daylight.

The night is one of three things:

- the sample night shipped with the package (``ekos_notifier.samples``), the default;
- a recording (``ekos-notifier-replay/0``);
- a session from the user's own database (``--session last`` or an id), read-only: the events the
  adapters recorded then, through fresh rules. The states known before the session (the site, the
  mount's park state and pier side, the camera, which INDI readings exist) are carried in as
  snapshot readings, as the service would know them.

The rules are built exactly as the service builds them (``service.build_rules``): the user's
config, and Ekos's own limits from KStars's settings when KStars runs here. The disk isn't
checked: a replayed night's frames aren't on this disk. Nothing is written anywhere; with
``send`` the alerts that would go out at once are sent, marked as simulated, and an Emergency is
sent as High so a pager doesn't start repeating.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from ekos_notifier import replay
from ekos_notifier.ekos import translation
from ekos_notifier.events import Event
from ekos_notifier.i18n import _
from ekos_notifier.notify import should_send
from ekos_notifier.pipeline import Pipeline, simulate
from ekos_notifier.rules import Rules
from ekos_notifier.samples import SAMPLE_NIGHT

DERIVED = ("session", "rules", "service")  # sources of what the pipeline itself produced
BEFORE = 60.0  # seconds of events read before the session's setup or start
AFTER = 900.0  # and after its end, for grace periods and the all-clears that follow
# States known before the session started, carried in as snapshots.
CARRIED = (
    "site.location",
    "mount.park",
    "mount.state",
    "mount.pier_side",
    "capture.camera",
    "capture.target",
    "indi.capabilities",
    "ekos.state",
    "indi.state",
)


@dataclass
class Night:
    label: str  # what was replayed, for the heading
    events: list[Event]


class SimulateError(Exception):
    """Worded for the user."""


def sample() -> Night:
    return recording(SAMPLE_NIGHT, label="the sample night (M 31 on the KStars simulators)")


def recording(path: str | Path, label: str | None = None) -> Night:
    try:
        meta, records = replay.read(path)
    except (OSError, ValueError) as exc:
        raise SimulateError(str(exc)) from None
    label = label or f"{path}"
    language = meta.get("language")
    translator = translation.for_language(language)
    if translator:
        label += f" (KStars in '{translator.language}', read through its translations)"
    elif language and not language.startswith(("en", "C")):
        label += (
            f" (KStars in '{language}', whose translations aren't installed here: alerts that"
            " read Ekos's log may be missing)"
        )
    numbers = replay.meta_numbers(meta)
    return Night(label, list(replay.events(records, translator=translator, numbers=numbers)))


def sessions(db_path: str | Path, limit: int = 10) -> list[sqlite3.Row]:
    with closing(_connect(db_path)) as db:
        return list(
            db.execute(
                "SELECT * FROM sessions WHERE started_at IS NOT NULL"
                " ORDER BY started_at DESC LIMIT ?",
                (limit,),
            )
        )


def session(db_path: str | Path, which: str) -> Night:
    """``which``: "last" (the latest session that imaged) or the start of a session id."""
    with closing(_connect(db_path)) as db:
        if which == "last":
            row = db.execute(
                "SELECT * FROM sessions WHERE started_at IS NOT NULL"
                " ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        else:
            rows = list(db.execute("SELECT * FROM sessions WHERE id LIKE ?", (which + "%",)))
            if len(rows) > 1:
                raise SimulateError(f"'{which}' matches {len(rows)} sessions: give more of the id")
            row = rows[0] if rows else None
        if row is None:
            raise SimulateError(
                "no imaging session in the database yet"
                if which == "last"
                else f"no session '{which}' (see --list)"
            )
        start = (row["setup_at"] or row["started_at"]) - BEFORE
        last = db.execute("SELECT MAX(ts) FROM events").fetchone()[0] or start
        end = (row["ended_at"] or last) + AFTER
        marks = ",".join("?" * len(DERIVED))
        carried = []
        for kind in CARRIED:
            prior = db.execute(
                f"SELECT ts, kind, data, source FROM events WHERE kind = ? AND ts < ?"
                f" AND source NOT IN ({marks}) ORDER BY ts DESC, id DESC LIMIT 1",
                (kind, start, *DERIVED),
            ).fetchone()
            if prior is not None:
                data = {**json.loads(prior["data"]), "snapshot": True}
                carried.append(Event(start, prior["kind"], data, prior["source"]))
        rows = db.execute(
            f"SELECT ts, kind, data, source FROM events WHERE ts BETWEEN ? AND ?"
            f" AND source NOT IN ({marks}) ORDER BY ts, id",
            (start, end, *DERIVED),
        )
        events = carried + [
            Event(r["ts"], r["kind"], json.loads(r["data"]), r["source"]) for r in rows
        ]
    when = time.strftime("%H:%M", time.localtime(row["started_at"]))
    label = f"your session of the night of {row['night']} (started {when}, {row['id'][:8]})"
    return Night(label, events)


def run(night: Night, rules: Rules) -> list[Event]:
    """Every alert the night raises with these rules, in order."""
    out: list[Event] = []
    simulate(night.events, Pipeline(out.append, rules=rules))
    return [e for e in out if e.kind == "alert"]


def report(night: Night, alerts: Iterable[Event], *, brief: bool = False) -> list[str]:
    alerts = list(alerts)
    lines = [f"Simulating {night.label}.", ""]
    sent = held = 0
    for a in alerts:
        d = a.data
        now = should_send(d)
        sent += now
        held += not now
        how = d["priority"].capitalize() + ("" if now else " (digest)")
        lines.append(f"{time.strftime('%H:%M', time.localtime(a.ts))}  {how:<16} {d['title']}")
        if not brief:
            lines += [f"{'':23}{line}" for line in str(d.get("body", "")).splitlines()]
            lines.append("")
    if not alerts:
        lines.append("No alerts.")
    if brief or not alerts:
        lines.append("")
    lines.append(
        f"{sent} alert{'s' * (sent != 1)} would be sent at once, {held} held for the digest."
    )
    return lines


def send(
    alerts: Iterable[Event], urls: list[str], sender: Callable[[str, str, str, str], str | None]
) -> list[str]:
    """Send the alerts that would go out at once; returns the errors."""
    errors = []
    for a in alerts:
        d = a.data
        if not should_send(d):
            continue
        priority = "high" if d["priority"] == "emergency" else d["priority"]
        for url in urls:
            title = _("[Simulated] {title}").format(title=d["title"])
            error = sender(url, priority, title, str(d.get("body", "")))
            if error:
                errors.append(error)
    return errors


def _connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.exists():
        raise SimulateError(f"{path}: no database yet (has the service run?)")
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db
