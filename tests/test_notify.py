import asyncio
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from town_crier import replay
from town_crier.events import Event
from town_crier.notify import Notifier, init_cursor, sink_id, with_priority
from town_crier.service import serve
from town_crier.store import Store

FIXTURE = Path(__file__).parent / "fixtures/replay/sim-m31-scheduler-kstars-3.8.5-flatpak.ndjson.gz"
PUSHOVER = "pover://" + "u" * 30 + "@" + "t" * 30


def alert(ts, priority="normal", immediate=True, title="t"):
    return Event(
        ts,
        "alert",
        {"rule": "r", "priority": priority, "title": title, "body": "b", "immediate": immediate},
        "rules",
    )


class FakeSender:
    def __init__(self, fail=0):
        self.fail = fail
        self.sent = []

    def __call__(self, url, priority, title, body):
        if self.fail:
            self.fail -= 1
            return "network unreachable"
        self.sent.append((url, priority, title))
        return None


def params(url):
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def test_pushover_priorities():
    url, levels = with_priority(PUSHOVER + "?sound=bike", "emergency")
    assert levels and params(url) == {
        "sound": "bike",
        "priority": "emergency",
        "interval": "60",
        "expire": "3600",
    }
    assert params(with_priority(PUSHOVER, "quiet")[0]) == {"priority": "low"}


def test_ntfy_and_others():
    assert params(with_priority("ntfys://ntfy.sh/topic", "high")[0]) == {"priority": "high"}
    assert with_priority("mailto://a:b@example.com", "emergency") == (
        "mailto://a:b@example.com",
        False,
    )


def test_sink_id_hides_the_url():
    assert len(sink_id(PUSHOVER)) == 12 and "u" * 5 not in sink_id(PUSHOVER)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "tc.db"
    s = Store(path)
    s.append([alert(1.0, title="old alert")])  # from before the notifier existed
    init_cursor(s)
    s.close()
    return path


def notifier(db, send, now):
    return Notifier(db, [PUSHOVER], send=send, clock=lambda: now["t"], retry_delays=(30, 60))


def test_sends_new_alerts_not_old_ones_emergency_first(db):
    s = Store(db)
    s.append(
        [
            alert(2.0, "normal"),
            alert(3.0, "emergency"),
            alert(4.0, "normal", immediate=False),
            alert(5.0, "quiet"),
        ]
    )
    send, now = FakeSender(), {"t": 10.0}
    n = notifier(db, send, now)
    n.step()
    assert [p for _, p, _ in send.sent] == ["emergency", "normal"]
    statuses = [r[0] for r in s.db.execute("SELECT status FROM outbox ORDER BY event_id")]
    assert statuses == ["sent", "sent", "held", "held"]
    n.step()
    assert len(send.sent) == 2  # nothing sent twice
    s.close()


def test_offline_alerts_are_retried_until_they_go(db):
    s = Store(db)
    s.append([alert(2.0, "emergency")])
    send, now = FakeSender(fail=2), {"t": 10.0}
    n = notifier(db, send, now)
    n.step()  # fails, retry in 30 s
    now["t"] = 30.0
    n.step()  # not due yet
    assert send.sent == [] and send.fail == 1
    now["t"] = 41.0
    n.step()  # fails again, retry in 60 s
    now["t"] = 102.0
    n.step()
    assert [p for _, p, _ in send.sent] == ["emergency"]
    row = s.db.execute("SELECT status, attempts, last_error FROM outbox").fetchone()
    assert (row[0], row[1], row[2]) == ("sent", 3, None)
    s.close()


def test_service_sends_the_m31_alerts(tmp_path):
    _, records = replay.read(FIXTURE)
    events = list(replay.ekos_events(records))
    send = FakeSender()
    db = tmp_path / "tc.db"

    async def scenario():
        stop = asyncio.Event()
        clock = {"t": events[0].ts}

        async def source(emit):
            for e in events:
                clock["t"] = e.ts
                emit(e)
            for step in range(70):  # 11+ minutes after the end, in 10 s ticks
                clock["t"] = events[-1].ts + 10 * (step + 1)
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.3)  # let the writer and notifier catch up
            stop.set()
            await asyncio.Event().wait()

        # The whole night replays in a second, so to the notifier every alert looks old:
        # this test is about routing, not expiry.
        n = Notifier(db, [PUSHOVER], send=send, poll=0.05, clock=lambda: clock["t"], max_age=1e9)
        await serve(
            db, sources=[source], notifier=n, tick=0.005, clock=lambda: clock["t"], stop=stop
        )

    asyncio.run(scenario())
    assert [(p, t) for _, p, t in send.sent] == [
        ("normal", "Starting session"),
        ("emergency", "Mount left unparked"),
        ("normal", "Session summary"),
    ]


def test_undelivered_alerts_are_dropped_not_sent_late(db):
    s = Store(db)
    s.append([alert(100.0, "emergency", title="mount")])
    send, now = FakeSender(fail=100), {"t": 100.0}
    n = Notifier(db, [PUSHOVER], send=send, clock=lambda: now["t"], retry_delays=(15,), max_age=600)
    for t in range(100, 1000, 15):  # offline the whole time
        now["t"] = float(t)
        n.step()
    send.fail = 0  # back online
    now["t"] = 2000.0
    n.step()
    assert send.sent == []
    status, error = s.db.execute("SELECT status, last_error FROM outbox").fetchone()
    assert status == "dropped" and "within 10 min" in error
    assert [e.data["title"] for e in s.events_after(kinds=["alert"])][
        -1
    ] == "mount"  # still recorded
    s.close()


def test_alert_already_too_old_when_seen_is_dropped(db):
    s = Store(db)
    s.append([alert(100.0, "emergency")])
    send = FakeSender()
    Notifier(db, [PUSHOVER], send=send, clock=lambda: 100.0 + 3600, max_age=600).step()
    assert send.sent == []
    assert s.db.execute("SELECT status FROM outbox").fetchone()[0] == "dropped"
    s.close()
