import pytest


@pytest.fixture(autouse=True)
def _no_user_config(tmp_path_factory, monkeypatch):
    """Never read the developer's real config (it may hold live notifier keys), and never touch
    their real data folders."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("xdg-config")))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path_factory.mktemp("xdg-data")))
