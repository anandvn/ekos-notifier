from importlib.metadata import version

import pytest

from ekos_notifier import __version__
from ekos_notifier.cli import EXIT_NOT_IMPLEMENTED, main


def test_version_matches_package_metadata():
    assert __version__ == version("ekos-notifier")


def test_no_command_prints_help(capsys):
    assert main([]) == 0
    assert "ekos-notifier" in capsys.readouterr().out


def test_stub_command_reports_not_implemented(capsys):  # doctor is still a stub
    assert main(["doctor"]) == EXIT_NOT_IMPLEMENTED
    assert "not implemented" in capsys.readouterr().err


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
