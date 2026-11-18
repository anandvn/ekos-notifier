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


def test_every_example_can_be_uncommented(tmp_path):
    # The guide says: to use an example, delete the "#". That must give valid YAML every time
    # (it didn't when examples sat under "messages: {}").
    import re

    example = re.compile(r'^(\s*)# (- "|[a-z_.]+: )')
    lines = config.example_text().splitlines()
    uncommented = [example.sub(r"\1\2", line) for line in lines]
    assert sum(a != b for a, b in zip(lines, uncommented, strict=True)) >= 10
    path = tmp_path / "config.yaml"
    path.write_text("\n".join(uncommented))
    cfg = config.load(path)
    assert len(cfg.notify) == 2 and cfg.messages.level("darks_reminder") == "normal"
    assert cfg.low_volts == 12.0 and "env.humidity" in str(cfg.indi_mappings)
    assert cfg.warnings == []  # every key in the example is a real setting


def test_warnings_for_settings_that_do_nothing(tmp_path, capsys):
    path = tmp_path / "config.yaml"
    path.write_text(
        "notfy: []\n"
        "alerts:\n  dew_margin: 3\n"
        "power:\n  preset: auto\n  battery: lifepo4_12v\n  low_volts: 12.5\n"
    )
    warnings = config.load(path).warnings
    assert "'notfy' isn't a setting (did you mean 'notify'?); it's ignored" in warnings
    assert any("'alerts.dew_margin'" in w and "did you mean 'dew_margin_c'" in w for w in warnings)
    assert any("'power.preset'" in w and "recognised automatically" in w for w in warnings)
    assert (
        "'power.low_volts: 12.5' replaces lifepo4_12v's 12.8 V; delete it to use the preset's level"
        in warnings
    )
    assert main(["config", "check", "--config", str(path)]) == 0  # warnings, not errors
    assert "note: 'power.low_volts: 12.5' replaces" in capsys.readouterr().out


def test_no_warning_for_the_presets_own_levels_and_mains_ignores_levels(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("power:\n  battery: lifepo4_12v\n  low_volts: 12.8\n  critical_volts: 12\n")
    assert config.load(path).warnings == []
    path.write_text("power:\n  battery: mains\n  low_volts: 12\n")
    assert config.load(path).warnings == [
        "'power.low_volts' is ignored with battery: mains (no voltage alerts)"
    ]
