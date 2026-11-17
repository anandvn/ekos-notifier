import os
import sqlite3

from ekos_notifier.ekos.settings import UNKNOWN, guider_settings, parse_kconfig

HOME = "/home/astro"
FLATPAK_ENV = {
    "HOME": HOME,
    "XDG_CONFIG_HOME": f"{HOME}/.var/app/org.kde.kstars/config",
    "XDG_CONFIG_DIRS": "/app/etc/xdg:/etc/xdg",
    "XDG_DATA_HOME": f"{HOME}/.var/app/org.kde.kstars/data",
}
FLATPAK_RC = f"{HOME}/.var/app/org.kde.kstars/config/kstarsrc"
FLATPAK_DB = f"{HOME}/.var/app/org.kde.kstars/data/kstars/userdb.sqlite"
# As on the dev VM: two internal-guider profiles and one PHD2 profile.
PROFILES = [
    ("Simulators", 0, None, None),
    ("p1 svx102t", 0, None, None),
    ("Ekos Notifier Sim", 1, "localhost", 4400),
    ("Remote guider", 1, "guidepi.local", 4401),
    ("Lin", 2, None, None),
]


def fake_kstars(tmp_path, pid=1352, env=FLATPAK_ENV, rc="", db=FLATPAK_DB, profiles=PROFILES):
    """A /proc entry for a kstars process whose root holds its kstarsrc and userdb."""
    proc = tmp_path / "proc"
    entry = proc / str(pid)
    entry.mkdir(parents=True)
    (entry / "comm").write_text("kstars\n")
    (entry / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in env.items()))
    root = entry / "root"
    rc_home = env.get("XDG_CONFIG_HOME") or env["HOME"] + "/.config"
    if rc is not None:
        f = root / rc_home.lstrip("/") / "kstarsrc"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(rc)
    if profiles is not None:
        f = root / db.lstrip("/")
        f.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(f)
        con.execute(
            "CREATE TABLE profile (id INTEGER PRIMARY KEY, name TEXT, guidertype INTEGER,"
            " guiderhost TEXT, guiderport INTEGER)"
        )
        con.executemany(
            "INSERT INTO profile (name, guidertype, guiderhost, guiderport) VALUES (?, ?, ?, ?)",
            profiles,
        )
        con.commit()
        con.close()
    root.mkdir(exist_ok=True)
    return proc


def settings(proc):
    return guider_settings(proc, uid=os.getuid())


def test_parse_kconfig():
    groups = parse_kconfig(
        "# comment\n[Guide]\nGuiderType[$i]=1\nName[de]=x\nName=y\nGuiderType=2\n[Other]\nk=v\n"
    )
    assert groups["Guide"] == {"GuiderType": "2", "Name": "y"}  # later lines win
    assert groups["Other"] == {"k": "v"}


def test_ekos_started_with_an_internal_guider_profile(tmp_path):
    # As seen live: starting "Simulators" removed both `profile=` and `GuiderType=` (defaults).
    s = settings(fake_kstars(tmp_path, rc="[Ekos]\nEkosWindowWidth=1920\n[Guide]\n"))
    assert (s.guider, s.profile, s.uses_phd2) == ("internal", "Simulators", False)


def test_ekos_started_with_a_phd2_profile(tmp_path):
    rc = (
        "[Ekos]\nprofile=Remote guider\n"
        "[Guide]\nGuiderType=1\nPHD2Host=guidepi.local\nPHD2Port=4401\n"
    )
    s = settings(fake_kstars(tmp_path, rc=rc))
    assert (s.guider, s.phd2_host, s.phd2_port) == ("phd2", "guidepi.local", 4401)


def test_phd2_wins_when_profile_and_guide_setting_differ(tmp_path):
    # PHD2 profile picked but Ekos not started yet: the Guide setting still says internal.
    s = settings(fake_kstars(tmp_path / "a", rc="[Ekos]\nprofile=Remote guider\n"))
    assert (s.guider, s.phd2_host, s.phd2_port) == ("phd2", "guidepi.local", 4401)
    # Internal profile, but PHD2 chosen in Guide → Options.
    s = settings(fake_kstars(tmp_path / "b", rc="[Guide]\nGuiderType=1\n"))
    assert (s.guider, s.phd2_host, s.phd2_port) == ("phd2", "localhost", 4400)


def test_lin_guider(tmp_path):
    rc = "[Ekos]\nprofile=Lin\n[Guide]\nGuiderType=2\n"
    assert settings(fake_kstars(tmp_path, rc=rc)).guider == "lin_guider"


def test_system_default_under_the_user_file(tmp_path):
    proc = fake_kstars(tmp_path, rc="[General]\n")
    system = proc / "1352/root/app/etc/xdg/kstarsrc"
    system.parent.mkdir(parents=True)
    system.write_text("[Ekos]\nprofile=Ekos Notifier Sim\n")
    assert settings(proc).guider == "phd2"


def test_native_kstars_paths(tmp_path):
    env = {"HOME": HOME}
    proc = fake_kstars(
        tmp_path,
        env=env,
        rc="[Ekos]\nprofile=p1 svx102t\n",
        db=f"{HOME}/.local/share/kstars/userdb.sqlite",
    )
    assert (settings(proc).guider, settings(proc).profile) == ("internal", "p1 svx102t")


def test_unknown_unless_certain(tmp_path):
    assert settings(tmp_path / "empty-proc") == UNKNOWN  # no KStars running
    assert settings(fake_kstars(tmp_path / "a", rc=None)) == UNKNOWN  # no kstarsrc
    assert settings(fake_kstars(tmp_path / "b", profiles=None)) == UNKNOWN  # no userdb
    gone = "[Ekos]\nprofile=Deleted profile\n"
    assert settings(fake_kstars(tmp_path / "c", rc=gone)) == UNKNOWN
    odd = "[Guide]\nGuiderType=7\n"  # a guider we don't know
    assert settings(fake_kstars(tmp_path / "d", rc=odd)) == UNKNOWN
    mixed = "[Ekos]\nprofile=Lin\n"  # profile lin_guider, Guide setting internal
    assert settings(fake_kstars(tmp_path / "e", rc=mixed)) == UNKNOWN
    proc = fake_kstars(tmp_path / "f", rc="")
    fake_kstars(tmp_path / "f", pid=2, rc="[Ekos]\nprofile=Ekos Notifier Sim\n")
    assert settings(proc) == UNKNOWN  # two KStars that disagree
