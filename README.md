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
> What works today: `ekos-notifier run` follows Ekos over D-Bus and records the night (sessions,
> frames, guiding, every Ekos state change) in a local SQLite database, and sends the first
> alerts (session start and summary, and the "mount left unparked" Emergency) to any
> [Apprise](https://github.com/caronc/apprise/wiki) notifier, such as Pushover.
> To try it from a checkout, on the computer running KStars:
>
> ```bash
> python -m venv .venv && . .venv/bin/activate && pip install -e .
> ekos-notifier run            # Ctrl-C to stop; the database is ~/.local/share/ekos-notifier/ekos-notifier.db
> ```

## Planned install

```bash
pipx install ekos-notifier
ekos-notifier install-service
# the first-run wizard opens in your browser
```

## Contributing

Device presets, message styles and docs are the easiest ways in. See
[CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).
