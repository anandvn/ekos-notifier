import pytest

from ekos_notifier import replay, simulate
from ekos_notifier.cli import main
from ekos_notifier.config import Config
from ekos_notifier.events import Event
from ekos_notifier.pipeline import Pipeline
from ekos_notifier.pipeline import simulate as replay_night
from ekos_notifier.rules import Rules
from ekos_notifier.samples import SAMPLE_NIGHT
from ekos_notifier.service import build_rules
from ekos_notifier.store import Store

SAMPLE_RULES = ["session_started", "session_summary", "ekos_stopped_mount_unparked"]


def rules():
    return build_rules(Config(), live=False)


@pytest.fixture(scope="module")
def recorded_db(tmp_path_factory):
    """A database the service would have written while the sample night happened."""
    path = tmp_path_factory.mktemp("db") / "ekos-notifier.db"
    store = Store(path)
    _, records = replay.read(SAMPLE_NIGHT)
    out: list[Event] = []
    replay_night(replay.events(records), Pipeline(out.append, rules=Rules()))
    store.append(out)
    store.close()
    return path, [e for e in out if e.kind == "alert"]


def test_sample_night_is_the_default():
    night = simulate.sample()
    alerts = simulate.run(night, rules())
    assert [a.data["rule"] for a in alerts] == SAMPLE_RULES


def test_a_session_from_the_database_replays_to_the_same_alerts(recorded_db):
    path, original = recorded_db
    night = simulate.session(path, "last")
    assert "night of" in night.label
    assert not any(e.source in simulate.DERIVED for e in night.events)
    alerts = simulate.run(night, rules())
    assert [a.data["rule"] for a in alerts] == [a.data["rule"] for a in original]
    # Within one 10 s tick: the replay's clock starts from the window's first event.
    assert all(abs(a.ts - o.ts) <= 10 for a, o in zip(alerts, original, strict=True))


def test_session_by_id_prefix_and_unknown_ids(recorded_db):
    path, _ = recorded_db
    [row] = simulate.sessions(path)
    assert simulate.session(path, row["id"][:6]).events
    with pytest.raises(simulate.SimulateError, match="no session 'nope'"):
        simulate.session(path, "nope")


def test_missing_database_is_explained(tmp_path):
    with pytest.raises(simulate.SimulateError, match="no database yet"):
        simulate.session(tmp_path / "none.db", "last")


def test_report_marks_what_waits_for_the_digest():
    night = simulate.Night("a test night", [])
    alerts = [
        Event(0, "alert", {"rule": "a", "priority": "high", "title": "Now", "body": "x"}),
        Event(60, "alert", {"rule": "b", "priority": "normal", "title": "Later", "body": "y"}),
    ]
    lines = simulate.report(night, alerts, brief=True)
    assert lines[0] == "Simulating a test night."
    assert any("High" in line and "Now" in line for line in lines)
    assert any("Normal (digest)" in line and "Later" in line for line in lines)
    assert lines[-1] == "1 alert would be sent at once, 1 held for the digest."


def test_send_marks_simulated_and_never_pages():
    sent = []
    alerts = [
        Event(0, "alert", {"rule": "a", "priority": "emergency", "title": "Mount", "body": "b"}),
        Event(1, "alert", {"rule": "d", "priority": "quiet", "title": "Digest", "body": "c"}),
    ]
    errors = simulate.send(alerts, ["url"], lambda *a: sent.append(a))
    assert errors == []
    assert sent == [("url", "high", "[Simulated] Mount", "b")]


def test_cli_simulate_sample_and_list(tmp_path, capsys, recorded_db):
    assert main(["simulate", "--brief", "--config", str(tmp_path / "none.yaml")]) == 1
    (tmp_path / "c.yaml").write_text("{}")
    assert main(["simulate", "--brief", "--config", str(tmp_path / "c.yaml")]) == 0
    out = capsys.readouterr().out
    assert "Mount left unparked" in out and "3 alerts would be sent at once" in out
    path, _ = recorded_db
    assert (
        main(["simulate", "--list", "--db", str(path), "--config", str(tmp_path / "c.yaml")]) == 0
    )
    assert "scheduler_finished" in capsys.readouterr().out
