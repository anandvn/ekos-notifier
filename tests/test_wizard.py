import stat

import pytest

from ekos_notifier import config, wizard
from ekos_notifier.cli import main
from ekos_notifier.wizard import Answers, Prompter, Seen

KEY = "a" * 30
TOKEN = "b" * 30


def example():
    return config.example_text().replace("# Copy to ~/.config/ekos-notifier/config.yaml. ", "# ")


class Script:
    """Answers questions in order, and records everything said."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.out = []

    def read(self, prompt):
        self.out.append(prompt)
        if not self.answers:
            raise EOFError
        answer = self.answers.pop(0)
        if answer is KeyboardInterrupt:
            raise KeyboardInterrupt
        return answer

    def say(self, line):
        self.out.append(line)

    @property
    def text(self):
        return "\n".join(self.out)


async def nothing_seen(cfg):
    return Seen(["  ok    KStars found"], indi=True, voltage=12.6)


def run(path, *answers, send=lambda url: None, installed=True, installs=None):
    s = Script(*answers)
    rc = wizard.run(
        str(path),
        Prompter(s.read, s.say),
        send=send,
        find=nothing_seen,
        service=lambda: installed,
        install_service=(lambda: installs.append(1) or 0) if installs is not None else None,
    )
    return rc, s


# -- editing the text -----------------------------------------------------------------------


def test_set_value_changes_uncomments_or_adds():
    text = "power:\n  battery: agm_12v   # the kind\n  # low_volts: 12.0   # Battery low\n"
    text = wizard.set_value(text, "power", "battery", "mains")
    assert "  battery: mains   # the kind\n" in text
    text = wizard.set_value(text, "power", "low_volts", "12.4")
    assert "  low_volts: 12.4   # Battery low\n" in text
    text = wizard.set_value(text, "power", "critical_volts", "11.8")
    assert text.startswith("power:\n  critical_volts: 11.8\n")
    text = wizard.set_value(text, "digest", "enabled", "false")
    assert text.endswith("\ndigest:\n  enabled: false\n")
    assert config.load  # the result is loaded in the tests below


def test_set_value_stays_inside_its_section():
    text = "digest:\n  enabled: true\n\n# Power\npower:\n  enabled: x\n"
    out = wizard.set_value(text, "digest", "enabled", "false")
    assert out == "digest:\n  enabled: false\n\n# Power\npower:\n  enabled: x\n"


def test_comment_out():
    text = "power:\n  battery: custom\n  low_volts: 12.2  # mine\n"
    assert wizard.comment_out(text, "power", "low_volts").endswith("  # low_volts: 12.2  # mine\n")


def test_set_notify_replaces_items_and_keeps_the_examples():
    text = example()
    once = wizard.set_notify(text, ["pover://x@y", 'ntfys://ntfy.sh/a"b'])
    assert '\nnotify:\n  - "pover://x@y"\n  - "ntfys://ntfy.sh/a\\"b"\n  # - "pover://USER' in once
    twice = wizard.set_notify(once, ["tgram://t/c"])
    assert "pover://x@y" not in twice and '  - "tgram://t/c"\n  # - "pover' in twice
    assert wizard.set_notify('notify: "x://y"\n', []) == "notify:\n"
    assert wizard.set_notify("digest:\n  enabled: true\n", ["a://b"]).startswith(
        'notify:\n  - "a://b"\n\ndigest:'
    )


@pytest.mark.parametrize(
    "answers",
    [
        Answers(["pover://u@t"], battery="lifepo4_12v", digest_interval=30),
        Answers([], battery="mains", digest_enabled=False),
        Answers(["a://b", "c://d"], battery="custom", low_volts=24.2, critical_volts=23.0),
        Answers([], battery="agm_12v", low_volts=12.2),  # the preset with my own low level
    ],
)
def test_the_example_with_answers_applied_loads_as_asked(answers, tmp_path):
    cfg = wizard.check_text(wizard.apply(example(), answers), tmp_path / "config.yaml")
    assert wizard.matches(cfg, answers)
    assert cfg.warnings == [] or answers.low_volts == 12.2


def test_by_hand_is_valid_yaml_that_says_the_same(tmp_path):
    a = Answers(["pover://u@t"], battery="custom", low_volts=24.0, critical_volts=23.0)
    assert wizard.matches(wizard.check_text(wizard.by_hand(a), tmp_path / "c.yaml"), a)


# -- the whole wizard -----------------------------------------------------------------------


def test_first_run_with_pushover(tmp_path):
    path = tmp_path / "ekos-notifier" / "config.yaml"
    sent = []
    rc, s = run(
        path,
        "",  # set up a notifier: yes
        "",  # Pushover
        "short",  # not 30 characters: asked again
        KEY,
        TOKEN,
        "",  # send a test: yes
        "",  # it arrived
        "",  # no other notifier
        "2",  # LiFePO4
        "",  # digest on
        "",  # every 60 minutes
        "",  # save
        "",  # and install the service
        installed=False,
        installs=(installs := []),
        send=lambda url: sent.append(url),
    )
    assert rc == 0, s.text
    url = f"pover://{KEY}@{TOKEN}"
    assert sent == [url]
    assert "that's 30 letters and digits" in s.text
    assert "Your power box reads 12.6 V now." in s.text
    assert "(tested)" in s.text and KEY not in s.text  # the summary hides the key
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    cfg = config.load(path)
    assert cfg.notify == [url] and cfg.battery == "lifepo4_12v"
    assert "# Where to send alerts" in path.read_text()  # the example's comments are there
    assert installs == [1]  # the service was offered and installed


def test_a_failed_test_can_be_dropped(tmp_path):
    path = tmp_path / "config.yaml"
    rc, s = run(
        path,
        "",
        "5",  # another service
        "nope",  # not an Apprise URL: asked again
        "json://localhost/hook",
        "",  # send a test
        "n",  # don't try again
        "",  # don't keep it (default no)
        "",  # no other
        "3",  # mains
        "n",  # no digest
        "",  # save
        send=lambda url: "connection refused",
    )
    assert rc == 0, s.text
    assert "It didn't send: connection refused" in s.text
    assert "not a URL Apprise understands" in s.text
    cfg = config.load(path)
    assert cfg.notify == [] and cfg.battery == "mains" and not cfg.digest_enabled


def test_keeping_everything_changes_nothing_but_my_own_levels(tmp_path):
    path = tmp_path / "config.yaml"
    original = (
        "# mine\nnotify:\n   - 'pover://u@t'   # odd indent, kept\n"
        "power:\n  battery: lifepo4_12v\n  low_volts: 13.0\n"
    )
    path.write_text(original)
    rc, s = run(path, "", "", "", "", "", "")  # keep, LiFePO4, keep my level? no, digest, 60, save
    assert rc == 0, s.text
    assert "Your config sets its own levels (low 13 V)" in s.text
    assert path.read_text() == original.replace("  low_volts: 13.0", "  # low_volts: 13.0")
    assert path.with_name("config.yaml.bak").read_text() == original


def test_keeping_my_own_levels(tmp_path):
    path = tmp_path / "config.yaml"
    original = "power:\n  battery: lifepo4_12v\n  low_volts: 13.0\n"
    path.write_text(original)
    rc, _ = run(path, "n", "", "y", "", "", "")  # no notifier, LiFePO4, keep mine, digest
    assert rc == 0 and path.read_text() == original


def test_leaving_saves_nothing(tmp_path):
    path = tmp_path / "config.yaml"
    rc, s = run(path, "", "1", KeyboardInterrupt)
    assert rc == 1 and "Nothing saved." in s.text and not path.exists()
    rc, s = run(path, "n", "", "", "", "n")  # no notifier … then don't save
    assert rc == 1 and not path.exists()


def test_a_config_with_a_mistake_is_left_alone(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("notify: [unclosed\n")
    rc, s = run(path)
    assert rc == 1 and "Fix it first" in s.text


def test_a_layout_it_cant_edit_is_left_alone_with_the_lines_to_add(tmp_path):
    path = tmp_path / "config.yaml"
    original = "power: {battery: agm_12v}\n"
    path.write_text(original)
    rc, s = run(path, "n", "3", "", "", "")  # no notifier, mains, digest on, 60, save
    assert rc == 1 and "can't edit safely" in s.text
    assert "  battery: mains" in s.text
    assert path.read_text() == original


def test_setup_needs_a_terminal(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert main(["setup"]) == 2
    assert "run it in a terminal" in capsys.readouterr().err


def test_set_value_refuses_a_section_on_one_line():
    with pytest.raises(wizard.CantEdit):
        wizard.set_value("power: {battery: agm_12v}\n", "power", "battery", "mains")
