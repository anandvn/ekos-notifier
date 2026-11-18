"""The 0.1.0 review's fixes: the service survives what it can, and stops cleanly when it can't."""

import asyncio
import logging

import pytest

from ekos_notifier import systemd
from ekos_notifier.cli import main
from ekos_notifier.ekos.adapter import EkosAdapter
from ekos_notifier.events import Event
from ekos_notifier.notify import Notifier, init_cursor
from ekos_notifier.store import Store

URL = "pover://" + "u" * 30 + "@" + "t" * 30


# -- the notifier survives a failed pass ------------------------------------------------------


def test_a_failing_pass_is_logged_and_the_notifier_carries_on(tmp_path, caplog):
    db = tmp_path / "tc.db"
    store = Store(db)
    init_cursor(store)
    store.append(
        [Event(10.0, "alert", {"rule": "r", "priority": "high", "title": "t", "body": "b"})]
    )
    store.close()
    sent, calls = [], []

    def send(url, priority, title, body):
        calls.append(title)
        if len(calls) == 1:
            raise RuntimeError("a notifier library blew up")
        sent.append(title)

    async def scenario():
        n = Notifier(db, [URL], send=send, poll=0.01, clock=lambda: 20.0)
        task = asyncio.create_task(n.run())
        await asyncio.sleep(0.3)
        assert not task.done()  # still running
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert sent == ["t"]  # the alert stayed queued and went on the next pass
    [record] = [r for r in caplog.records if "a pass failed" in r.getMessage()]
    assert record.exc_info is not None  # with the traceback


# -- D-Bus calls can't hang forever -----------------------------------------------------------


def test_a_call_kstars_never_answers_gives_up():
    class HungBus:
        async def call(self, msg):
            await asyncio.Event().wait()

    adapter = EkosAdapter(lambda e: None)
    adapter.call_timeout = 0.05
    reply = asyncio.run(
        adapter._call(
            HungBus(), "org.kde.kstars", "/KStars/Ekos", "org.kde.kstars.Ekos", "getStatus", "", []
        )
    )
    assert reply is None


# -- bugs in a source are logged with their traceback -----------------------------------------


@pytest.mark.parametrize(
    ("error", "traceback"), [(KeyError("frame"), True), (ConnectionError("bus gone"), False)]
)
def test_unexpected_source_errors_get_a_traceback(caplog, error, traceback):
    adapter = EkosAdapter(lambda e: None, backoff=(0.01,))

    async def run_once():
        raise error

    adapter.run_once = run_once

    async def scenario():
        task = asyncio.create_task(adapter.run())
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    record = next(r for r in caplog.records if "Ekos adapter" in r.getMessage())
    assert bool(record.exc_info) is traceback


# -- a broken config doesn't stop the night ---------------------------------------------------


def test_a_broken_config_runs_recording_only_and_is_watched(tmp_path, monkeypatch, caplog):
    import ekos_notifier.service as service

    path = tmp_path / "config.yaml"
    path.write_text("notify:\n\t- oops\n")
    seen = {}

    async def fake_serve(db, *, config, watch_config):
        seen.update(config=config, watch=watch_config)

    monkeypatch.setattr(service, "serve", fake_serve)
    assert main(["run", "--config", str(path), "--db", str(tmp_path / "db")]) == 0
    assert seen["config"].notify == [] and seen["config"].path == path and seen["watch"]
    assert "recording only" in caplog.text and "not valid YAML" in caplog.text


def test_a_config_file_that_isnt_there_is_still_an_error(tmp_path, capsys):
    assert main(["run", "--config", str(tmp_path / "nope.yaml")]) == 1
    assert "no such file" in capsys.readouterr().err


# -- a newer database stops the service for good ----------------------------------------------


def test_the_unit_does_not_restart_on_a_fatal_error():
    assert "RestartPreventExitStatus=78" in systemd.unit_text(python="/usr/bin/python3")
