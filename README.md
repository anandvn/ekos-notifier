# Ekos Notifier

**Your imaging night, watched and reported.**

Ekos Notifier is a companion service for KStars/Ekos astrophotography rigs. It runs on your imaging
computer, watches the night, and:

- **Alerts you when it matters**: guiding lost, sequence aborted, dew coming, battery low, mount
  heading for the pier. Not every lost star.
- **Sends an hourly digest** ("23:00 — All's well"), in plain English, terse, or Olde English.
- **Records every night as a session**: the plan, setup quality, conditions, every frame and event.
- **Works offline at a dark site.** Internet is a bonus, never a requirement.
- **Brings the data home** with one button, to a NAS, a USB drive or the cloud.

It works with the notification service you already use (Pushover, ntfy, Telegram, Discord, email
and 100+ more via Apprise) and degrades gracefully: no power box, no weather station, no internet —
no problem.

> **Status: pre-alpha.** The design is done; code is starting. See [the design](docs/design.md)
> and [the roadmap](docs/design.md#roadmap). Not released yet.
>
> What works today: following Ekos (over D-Bus), PHD2 and INDI, recording each night as a
> session in a local SQLite database, the alerts in [the docs](docs/index.md) and the hourly
> digest, sent to any [Apprise](https://github.com/caronc/apprise/wiki) notifier, such as
> Pushover. No GUI or sync yet.

## Install

On the computer running KStars, as yourself (not root):

```bash
curl -fsSL https://raw.githubusercontent.com/anandvn/ekos-notifier/main/install.sh | sh -s -- --from main
```

Until the first release is on PyPI, `--from main` installs the latest code from GitHub; after
that, plain `| sh` installs the latest release. The script puts Ekos Notifier in a venv in your
home folder (`~/.local/share/ekos-notifier/venv`), links `~/.local/bin/ekos-notifier`, writes a
commented config to fill in (`~/.config/ekos-notifier/config.yaml`) and offers to start it with
your session. Run it again to upgrade; `--uninstall` removes it (keeping your config and
sessions); `--help` lists the rest.

No root and no extra tools: only Python 3.10+ with its own `venv` module (StellarMate has it,
and has neither pipx nor pacman repositories; on Debian, Ubuntu and Raspberry Pi OS it's
`sudo apt install python3-venv`). Already use pipx or uv? `pipx install ekos-notifier` or `uv
tool install ekos-notifier`, then `ekos-notifier install-service`, works too. To work on the
code, see [CONTRIBUTING.md](CONTRIBUTING.md).

## Contributing

Device presets, message styles and docs are the easiest ways in. See
[CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).
