from importlib.metadata import version

import pytest

from town_crier import __version__
from town_crier.cli import EXIT_NOT_IMPLEMENTED, main


def test_version_matches_package_metadata():
    assert __version__ == version("ekos-towncrier")


def test_no_command_prints_help(capsys):
    assert main([]) == 0
    assert "town-crier" in capsys.readouterr().out


def test_stub_command_reports_not_implemented(capsys):  # doctor is still a stub
    assert main(["doctor"]) == EXIT_NOT_IMPLEMENTED
    assert "not implemented" in capsys.readouterr().err


def test_config_check_is_planned(capsys):
    assert main(["config", "check"]) == EXIT_NOT_IMPLEMENTED
    assert "config check" in capsys.readouterr().err


def test_config_without_subcommand_is_usage_error():
    with pytest.raises(SystemExit) as exc:
        main(["config"])
    assert exc.value.code == 2
