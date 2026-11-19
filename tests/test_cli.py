from importlib.metadata import version

import pytest

from ekos_notifier import __version__
from ekos_notifier.cli import main


def test_version_matches_package_metadata():
    assert __version__ == version("ekos-notifier")


def test_no_command_prints_help(capsys):
    assert main([]) == 0
    assert "ekos-notifier" in capsys.readouterr().out


def test_doctor_runs_the_checks(monkeypatch, tmp_path):
    from ekos_notifier import doctor

    seen = {}

    async def fake_run(config_path, db):
        seen.update(config=config_path, db=db)
        return 1

    monkeypatch.setattr(doctor, "run", fake_run)
    db = tmp_path / "x.db"
    assert main(["doctor", "--config", "c.yaml", "--db", str(db)]) == 1
    assert seen == {"config": "c.yaml", "db": db}


def test_config_check_without_a_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert main(["config", "check"]) == 0
    assert "records only" in capsys.readouterr().out


def test_config_check_flags_a_bad_url(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text('notify:\n  - "pover://short@x"\n  - "nonsense"\n')
    assert main(["config", "check", "--config", str(cfg)]) == 1
    assert "NOT a valid Apprise URL" in capsys.readouterr().err


def test_config_check_rejects_bad_yaml(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("notify: [unclosed\n")
    assert main(["config", "check", "--config", str(cfg)]) == 1
    assert "not valid YAML" in capsys.readouterr().err


def test_test_notify_needs_notifiers(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("notify: []\n")
    assert main(["test-notify", "--config", str(cfg)]) == 1


def test_config_without_subcommand_is_usage_error():
    with pytest.raises(SystemExit) as exc:
        main(["config"])
    assert exc.value.code == 2


def test_config_rejects_a_bad_expiry(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("notify_expire_minutes: soon\n")
    assert main(["config", "check", "--config", str(cfg)]) == 1
    assert "notify_expire_minutes" in capsys.readouterr().err


def test_config_check_shows_battery_levels(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("power:\n  preset: auto\n  battery: lifepo4_12v\n")
    assert main(["config", "check", "--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "battery: lifepo4_12v, low at or under 12.8 V, critical at or under 12 V" in out
    assert "'power.preset' isn't used (power boxes are recognised automatically)" in out
    cfg.write_text("power:\n  battery: mains\n")
    assert main(["config", "check", "--config", str(cfg)]) == 0
    assert "battery: mains, voltage alerts off" in capsys.readouterr().out


def test_config_check_shows_dew_and_cooler(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("alerts:\n  dew_margin_c: 3\n  cooler_tolerance_c: 0\n")
    assert main(["config", "check", "--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "dew risk: margin under 3 °C" in out and "cooler: off" in out
