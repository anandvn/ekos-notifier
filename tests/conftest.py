import pytest

from ekos_notifier import i18n
from ekos_notifier.ekos import translation


@pytest.fixture(autouse=True)
def _no_user_config(tmp_path_factory, monkeypatch):
    """Never read the developer's real config (it may hold live notifier keys) or the running
    KStars's settings (they change as the rig is used), and never touch their real data folders."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("xdg-config")))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path_factory.mktemp("xdg-data")))
    monkeypatch.setattr("ekos_notifier.service.guide_deviation_limit", lambda: None)
    monkeypatch.setattr("ekos_notifier.service.observing_limits", lambda: None)
    monkeypatch.setattr("ekos_notifier.ekos.translation.for_running_kstars", lambda: None)
    # Messages in English, whatever this computer's language (language: auto follows it)
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LANG", "C.UTF-8")
    i18n.use("en")
    monkeypatch.setattr(
        "ekos_notifier.ekos.translation.numbers_for_running_kstars",
        lambda: ("", translation.ENGLISH),
    )
