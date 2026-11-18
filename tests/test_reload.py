import asyncio
import os
import signal

from ekos_notifier.config import Config, load
from ekos_notifier.events import Event
from ekos_notifier.notify import Notifier
from ekos_notifier.reload import ConfigWatcher
from ekos_notifier.rules import Rules
from ekos_notifier.service import build_rules, serve

URL = "pover://" + "u" * 30 + "@" + "t" * 30


def ev(ts, kind, sid="s1", **data):
    return Event(ts, kind, data, "ekos", sid)


def config(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return load(path)


# -- the rules take new settings and keep the night -------------------------------------------


def test_reconfigure_keeps_state_and_takes_settings(tmp_path):
    rules = build_rules(Config(), live=False)
    out = rules.handle(ev(0, "session.started", night="2026-11-17", reason="scheduler"))
    out += rules.handle(ev(10, "metric", metric="power.voltage", value=11.9, device="Box"))
    out += rules.tick(100)
    assert [a.data["rule"] for a in out if a.data.get("rule") == "battery_low"] == ["battery_low"]

    new = config(
        tmp_path,
        "power:\n  battery: lifepo4_12v\n"
        "messages:\n  battery_ok: high\n"
        "alerts:\n  dew_margin_c: 3\n  disk_low_gb: 25\n  clouds_drop_percent: 70\n",
    )
    rules.reconfigure(build_rules(new, live=False), 200)
    power = next(r for r in rules.session_rules if type(r).__name__ == "PowerRule")
    assert (power.low, power.critical) == (12.8, 12.0)
    dew = next(r for r in rules.session_rules if type(r).__name__ == "DewRule")
    clouds = next(r for r in rules.session_rules if type(r).__name__ == "CloudsRule")
    assert dew.margin == 3.0 and abs(clouds.below - 0.3) < 1e-9
    # Still the same night: battery low isn't raised again, and its all-clear (now High) comes
    # once the battery is back over the new low level.
    out = rules.handle(ev(300, "metric", metric="power.voltage", value=11.9, device="Box"))
    out += rules.tick(400)
    assert not [a for a in out if a.data.get("rule") == "battery_low"]
    out = rules.handle(ev(500, "metric", metric="power.voltage", value=13.2, device="Box"))
    [ok] = [a for a in out if a.data.get("rule") == "battery_ok"]
    assert ok.data["priority"] == "high"


def test_a_new_digest_interval_reschedules_it():
    rules = Rules()
    rules.handle(ev(0, "session.started", night="2026-11-17", reason="scheduler"))
    before = rules.digest._next
    new = build_rules(Config(digest_interval_minutes=15, digest_align_to_clock=False), live=False)
    rules.reconfigure(new, 60)
    assert rules.digest._next == 60 + 15 * 60 != before


# -- the watcher ------------------------------------------------------------------------------


def watch(path, steps):
    """Run a watcher with a fast poll; ``steps`` is a list of (delay, action)."""
    applied, failed = [], []

    async def scenario():
        watcher = ConfigWatcher(path, applied.append, failed.append, poll=0.02)
        task = asyncio.create_task(watcher.run())
        for delay, action in steps:
            await asyncio.sleep(delay)
            action(watcher)
        await asyncio.sleep(0.15)
        task.cancel()

    asyncio.run(scenario())
    return applied, failed


def write(path, text):
    def action(_):
        path.write_text(text)
        os.utime(path, ns=(os.stat(path).st_mtime_ns + 10**9,) * 2)  # coarse clocks

    return action


def test_an_edit_is_applied_once_it_settles(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("notify: []\n")
    applied, failed = watch(
        path,
        [(0.05, write(path, "notify:\n")), (0.01, write(path, f'notify:\n  - "{URL}"\n'))],
    )
    assert len(applied) == 1 and applied[0].notify == [URL] and failed == []


def test_a_broken_edit_is_reported_and_ignored(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("notify: []\n")
    applied, failed = watch(path, [(0.05, write(path, "notify:\n\t- oops\n"))])
    assert applied == [] and "not valid YAML" in failed[0]


def test_sighup_reloads_at_once_and_nothing_changed_means_no_reload(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("notify: []\n")
    assert watch(path, [(0.05, lambda w: None)]) == ([], [])
    applied, _ = watch(path, [(0.0, lambda w: w.trigger())])
    assert len(applied) == 1


# -- the running service ----------------------------------------------------------------------


def test_the_service_sends_to_a_notifier_added_while_it_runs(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("notify: []\n")
    sent = []
    clock = {"t": 1000.0}

    def send(url, priority, title, body):
        sent.append((url, title))

    async def scenario():
        stop = asyncio.Event()
        emitted = asyncio.Event()

        async def source(emit):
            emit(ev(clock["t"], "ekos.state", sid=None, state="success"))
            emit(ev(clock["t"], "scheduler.state", sid=None, state="running"))  # alert, no URL
            await emitted.wait()
            clock["t"] += 60
            emit(ev(clock["t"], "scheduler.state", sid=None, state="idle"))  # the summary
            await asyncio.Event().wait()

        notifier = Notifier(tmp_path / "db", [], send=send, poll=0.02, clock=lambda: clock["t"])
        task = asyncio.create_task(
            serve(
                tmp_path / "db",
                config=load(path),
                sources=[source],
                notifier=notifier,
                tick=0.01,
                clock=lambda: clock["t"],
                stop=stop,
                watch_config=True,
            )
        )
        await asyncio.sleep(0.3)
        path.write_text(f'notify:\n  - "{URL}"\n')
        os.utime(path, ns=(os.stat(path).st_mtime_ns + 10**9,) * 2)
        await asyncio.sleep(0.1)
        os.kill(os.getpid(), signal.SIGHUP)  # don't wait for the 2 s poll
        await asyncio.sleep(0.3)
        emitted.set()
        await asyncio.sleep(0.4)
        stop.set()
        await task

    asyncio.run(scenario())
    assert sent == [(URL, "Session summary")]  # "Starting session" came before the URL
