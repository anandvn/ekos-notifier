import os

from ekos_notifier import legacy


def make_old(tmp_path):
    config = tmp_path / "config"
    data = tmp_path / "data"
    (config / "town-crier").mkdir(parents=True)
    (config / "town-crier/config.yaml").write_text("notify: []\n")
    (data / "town-crier").mkdir(parents=True)
    for suffix in ("", "-wal", "-shm"):
        (data / f"town-crier/town-crier.db{suffix}").write_text(suffix or "db")
    os.environ["XDG_CONFIG_HOME"], os.environ["XDG_DATA_HOME"] = str(config), str(data)
    return config, data


def test_moves_config_and_database_once(tmp_path):
    config, data = make_old(tmp_path)
    said = []
    legacy.migrate(say=said.append, proc=tmp_path / "no-proc")
    assert (config / "ekos-notifier/config.yaml").read_text() == "notify: []\n"
    new = data / "ekos-notifier"
    assert sorted(p.name for p in new.iterdir()) == [
        "ekos-notifier.db",
        "ekos-notifier.db-shm",
        "ekos-notifier.db-wal",
    ]
    assert (new / "ekos-notifier.db-wal").read_text() == "-wal"
    assert not (config / "town-crier").exists() and not (data / "town-crier").exists()
    assert len(said) == 2
    legacy.migrate(say=said.append, proc=tmp_path / "no-proc")
    assert len(said) == 2  # nothing left to do


def test_never_overwrites_new_folders(tmp_path):
    config, data = make_old(tmp_path)
    (config / "ekos-notifier").mkdir()
    (data / "ekos-notifier").mkdir()
    legacy.migrate(say=lambda m: None, proc=tmp_path / "no-proc")
    assert (config / "town-crier/config.yaml").exists()
    assert (data / "town-crier/town-crier.db").exists()
    assert list((data / "ekos-notifier").iterdir()) == []


def test_leaves_a_database_that_is_still_open(tmp_path):
    _, data = make_old(tmp_path)
    fd_dir = tmp_path / "proc/4242/fd"
    fd_dir.mkdir(parents=True)
    (fd_dir / "3").symlink_to(data / "town-crier/town-crier.db")
    said = []
    legacy.migrate(say=said.append, proc=tmp_path / "proc")
    assert (data / "town-crier/town-crier.db").exists() and not (data / "ekos-notifier").exists()
    assert "still has its database open" in said[-1]
