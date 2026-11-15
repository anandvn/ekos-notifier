from town_crier import __version__
from town_crier.cli import main


def test_version_string():
    assert __version__


def test_no_command_prints_help(capsys):
    assert main([]) == 0
    assert "town-crier" in capsys.readouterr().out


def test_stub_command_reports_not_implemented(capsys):
    assert main(["doctor"]) == 2
    assert "not implemented" in capsys.readouterr().err
