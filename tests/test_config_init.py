import stat

from ekos_notifier import config
from ekos_notifier.cli import main


def test_init_writes_the_example_privately_and_never_overwrites(tmp_path, capsys):
    path = tmp_path / "sub" / "config.yaml"
    assert main(["config", "init", "--config", str(path)]) == 0
    assert "Wrote" in capsys.readouterr().out
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert config.load(path).notify == []  # the example is a valid config
    assert "Copy to" not in path.read_text()
    path.write_text("notify: []\n")
    assert main(["config", "init", "--config", str(path)]) == 0
    assert "already exists" in capsys.readouterr().out
    assert path.read_text() == "notify: []\n"


def test_example_is_the_repos():
    assert config.example_text().startswith("# Ekos Notifier configuration")
