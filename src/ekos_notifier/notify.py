"""Send alerts from the store through Apprise (docs/design.md, "Alerts" and "Offline behavior").

The notifier is a consumer of the store: it reads ``alert`` events it hasn't seen, adds an
``outbox`` row per alert and notifier URL, and sends pending rows. A failed send is retried with
backoff, but only while the alert is still worth acting on: one not delivered within ``max_age``
seconds of being raised is marked ``dropped``, never sent late (a night's worth of stale alerts
arriving when you get home helps nobody). The alert itself stays in the store for the GUI and
the session export. Nothing the notifier does can block recording.

Which alerts are sent now: Emergency and High always; Normal when marked ``immediate`` (the rest
are held for the hourly digest, not built yet); Quiet never (in-app only).

Priorities map onto notifiers that have levels: Pushover (Emergency repeats every 60 s until
acknowledged, for up to an hour) and ntfy. Other notifiers get the priority in the title.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ekos_notifier.store import Store

log = logging.getLogger(__name__)

CONSUMER = "notify"
MAX_AGE = 600.0  # seconds: undelivered alerts older than this are dropped
RETRY_DELAYS = (15, 30, 60, 120)  # then every 2 minutes, until MAX_AGE

PUSHOVER = {
    "emergency": {"priority": "emergency", "interval": "60", "expire": "3600"},
    "high": {"priority": "high"},
    "normal": {"priority": "normal"},
    "quiet": {"priority": "low"},
}
NTFY = {"emergency": "max", "high": "high", "normal": "default", "quiet": "low"}
TITLE_PREFIX = {"emergency": "EMERGENCY: ", "high": "Important: "}

Sender = Callable[[str, str, str, str], "str | None"]  # (url, priority, title, body) -> error


def sink_id(url: str) -> str:
    """A stable, non-secret name for a notifier URL (URLs hold keys; they're never stored)."""
    return hashlib.sha256(url.encode()).hexdigest()[:12]


def with_priority(url: str, priority: str) -> tuple[str, bool]:
    """The URL with priority parameters for notifiers that have levels, and whether it does."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    if scheme in ("pover", "pushover"):
        params.update(PUSHOVER.get(priority, {}))
    elif scheme in ("ntfy", "ntfys"):
        params["priority"] = NTFY.get(priority, "default")
    else:
        return url, False
    return urlunsplit(parts._replace(query=urlencode(params))), True


def send_apprise(url: str, priority: str, title: str, body: str) -> str | None:
    """Send one message; ``None`` on success, else the reason it failed."""
    import apprise  # imported here: only the notifier needs it

    target, has_levels = with_priority(url, priority)
    if not has_levels:
        title = TITLE_PREFIX.get(priority, "") + title
    ap = apprise.Apprise()
    if not ap.add(target):
        return "not a valid Apprise URL"
    with apprise.LogCapture(level=logging.WARNING) as captured:
        ok = ap.notify(title=title, body=body)
        details = captured.getvalue().strip()
    if ok:
        return None
    return (details.splitlines()[-1] if details else "send failed")[:500]


def init_cursor(store: Store) -> None:
    """On a database the notifier hasn't used, start from now rather than sending old alerts.
    Called before the service writes anything, so no new alert can slip in first."""
    with store.db:
        store.db.execute(
            "INSERT OR IGNORE INTO consumers SELECT ?, COALESCE(MAX(id), 0) FROM events",
            (CONSUMER,),
        )


def should_send(data: dict) -> bool:
    priority = data.get("priority")
    return priority in ("emergency", "high") or (priority == "normal" and data.get("immediate"))


class Notifier:
    def __init__(
        self,
        store_path: str | Path,
        urls: list[str],
        *,
        send: Sender = send_apprise,
        poll: float = 2.0,
        clock: Callable[[], float] = time.time,
        retry_delays: tuple[float, ...] = RETRY_DELAYS,
        max_age: float = MAX_AGE,
    ) -> None:
        self.store_path = store_path
        self.urls = {sink_id(u): u for u in urls}
        self.send = send
        self.poll = poll
        self.clock = clock
        self.retry_delays = retry_delays
        self.max_age = max_age
        self.sent = 0
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="notify")
        self._store: Store | None = None

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            while True:
                await loop.run_in_executor(self._executor, self.step)
                await asyncio.sleep(self.poll)
        finally:
            if self._store is not None:
                await loop.run_in_executor(self._executor, self._store.close)
                self._store = None
            self._executor.shutdown(wait=False)

    def step(self) -> None:
        """Queue new alerts, then try everything due. Runs on the notifier's own thread."""
        if self._store is None:
            self._store = Store(self.store_path)
        self._queue_new(self._store)
        self._send_due(self._store)

    def _queue_new(self, store: Store) -> None:
        db = store.db
        init_cursor(store)
        last = db.execute(
            "SELECT last_event_id FROM consumers WHERE name = ?", (CONSUMER,)
        ).fetchone()[0]
        while True:
            batch = store.events_after_with_ids(last, kinds=["alert"], limit=200)
            if not batch:
                break
            with db:
                for event_id, event in batch:
                    status = "pending" if should_send(event.data) else "held"
                    for sink in self.urls:
                        db.execute(
                            "INSERT OR IGNORE INTO outbox (event_id, sink, status, next_try)"
                            " VALUES (?, ?, ?, ?)",
                            (event_id, sink, status, self.clock()),
                        )
                last = batch[-1][0]
                db.execute(
                    "UPDATE consumers SET last_event_id = ? WHERE name = ?", (last, CONSUMER)
                )

    def _send_due(self, store: Store) -> None:
        db = store.db
        rows = db.execute(
            "SELECT o.id, o.sink, o.attempts, e.ts, e.data FROM outbox o JOIN events e"
            " ON e.id = o.event_id WHERE o.status = 'pending' AND o.next_try <= ?"
            " ORDER BY CASE e.priority WHEN 'emergency' THEN 0 WHEN 'high' THEN 1 ELSE 2 END,"
            " o.id",
            (self.clock(),),
        ).fetchall()
        for row in rows:
            age = self.clock() - row["ts"]
            if age > self.max_age:
                # Too late to act on: drop it rather than deliver it late.
                with db:
                    db.execute(
                        "UPDATE outbox SET status = 'dropped', last_error = ? WHERE id = ?",
                        (f"not delivered within {self.max_age / 60:g} min", row["id"]),
                    )
                log.info("dropped undelivered alert (%d min old)", age // 60)
                continue
            url = self.urls.get(row["sink"])
            if url is None:  # notifier removed from the config since this was queued
                with db:
                    db.execute(
                        "UPDATE outbox SET status = 'failed', last_error = ? WHERE id = ?",
                        ("notifier no longer configured", row["id"]),
                    )
                continue
            data = json.loads(row["data"])
            error = self.send(url, data["priority"], data["title"], data["body"])
            now = self.clock()
            with db:
                if error is None:
                    self.sent += 1
                    db.execute(
                        "UPDATE outbox SET status = 'sent', sent_at = ?,"
                        " attempts = attempts + 1, last_error = NULL WHERE id = ?",
                        (now, row["id"]),
                    )
                    log.info("sent %s alert: %s", data["priority"], data["title"])
                else:
                    delay = self.retry_delays[min(row["attempts"], len(self.retry_delays) - 1)]
                    db.execute(
                        "UPDATE outbox SET attempts = attempts + 1, next_try = ?,"
                        " last_error = ? WHERE id = ?",
                        (now + delay, error, row["id"]),
                    )
                    log.warning(
                        "couldn't send %r (%s); retrying in %ds unless it's older than %g min",
                        data["title"],
                        error,
                        delay,
                        self.max_age / 60,
                    )
