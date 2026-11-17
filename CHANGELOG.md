# Changelog

All notable changes are recorded here. The format follows Keep a Changelog, and versions follow
Semantic Versioning.

## Unreleased

- **Guiding lost** alert (High, `ekos_notifier.guiding`): guiding stopped and not back within
  `alerts.guiding_recovery_minutes` (default 5) of time when guiding was expected, then
  **Guiding recovered**. Intent-aware: slews, meridian flips, plate solving, autofocus, flats with
  tracking off, calibration frames and the end of the run pause the clock; a guider stuck
  recalibrating doesn't count as recovered. Uses Ekos's Guide state (internal guider or PHD2)
  and PHD2's own lost-star reports; replays of the M 31 and IC 410 nights send no false alarm.
- **Renamed to Ekos Notifier** (was Town Crier): PyPI `ekos-notifier`, command `ekos-notifier`,
  package `ekos_notifier`, repository `anandvn/ekos-notifier`. The hourly "town crier" digest is
  now the hourly digest (config `digest:`, was `crier:`); the Olde English style stays. On first
  run, `~/.config/town-crier` and `~/.local/share/town-crier` (database and WAL files) move to
  the new names, unless the new folders exist or an old process still has the database open.
  Recordings in the old `town-crier-replay/0` format still load.
- Ekos adapter (`ekos_notifier.ekos`): finds the session bus KStars is on, listens to Ekos signals
  by the `org.kde.kstars` name (modules that appear later are covered), reports KStars appearing
  and vanishing, reads current states on connect, and reconnects with backoff. Signals become
  `Event`s (`capture.frame`, `mount.park`, `scheduler.state`, …); state codes are named, with
  those checked against a recording marked. `ekos_notifier.replay` runs recordings through the same
  decoder. New dependency: `dbus-fast`.
- Tests driven by the M 31 replay fixture.
- PHD2 adapter (`ekos_notifier.phd2`): reads PHD2's event server on localhost:4400 (read-only) and
  records guide steps (pixels, pulses, SNR, mass, HFD), guider state, lost stars, settle
  results, dithers, calibration and PHD2's alerts. The connect burst is marked as a snapshot;
  reconnects with backoff and waits quietly while PHD2 isn't running. Reads which guider Ekos
  uses from the running KStars (Flatpak or native): the selected profile's guider in
  `userdb.sqlite` and the Guide module's setting in `kstarsrc`. Connects to the PHD2 host and
  port Ekos uses, and doesn't contact PHD2 at all when both say internal or lin_guider. `ekos-notifier run` starts it
  alongside the Ekos adapter, and `replay.events()` replays both from a recording.
- Event store (`ekos_notifier.store`): SQLite in WAL mode with versioned migrations. Every event is
  appended to `events`; `frames` (with the guide RMS at capture time) and `samples` are filled
  in the same transaction. `EventWriter` batches events from asyncio code onto the store's own
  thread and retries failed writes rather than dropping them.
- Session manager (`ekos_notifier.session`): Standby → Setup → Active → Closed from Ekos events;
  tags every event with its session, attaches setup to the session it leads into and late
  calibration frames to the last imaging session. Closes when the Scheduler stops (finished or
  aborted, parked or not) or a manual sequence completes, on GUI End, on Ekos gone for 10 minutes (a crash and restart doesn't split the night), or the night ending.
  Resumes an open session after a service restart. New `sessions` table (schema 2).
- `ekos-notifier run`: the service in the foreground. Ekos adapter → session manager → store, with a
  clock tick for time-based session rules; SIGINT/SIGTERM stop it cleanly and everything queued
  is written. `--db`, `-v`.
- Fixed: stopping the writer mid-batch or before its database was open dropped queued events.
- Notifications. Rules (`ekos_notifier.rules`) turn events into alerts stored as events: "Starting
  session" and the session summary (Normal, sent at once), and the "Ekos stopped with the mount
  unparked" Emergency (2 minutes after Ekos stops, with an all-clear when the mount parks). The
  notifier (`ekos_notifier.notify`) sends them through Apprise from an outbox. An alert that can't
  be delivered is retried for 10 minutes (`notify_expire_minutes`) and then dropped, not sent
  late; it stays in the local record. Pushover Emergencies repeat every 60 s until acknowledged (up to an hour), ntfy
  gets matching priorities, other services get the priority in the title. Schema 3: `outbox`,
  `consumers`. New dependencies: `apprise`, `PyYAML`.
- Config file (`~/.config/ekos-notifier/config.yaml`, `notify:` so far); `ekos-notifier run --config`.
- `ekos-notifier config check` validates the config and every notifier URL (keys hidden).
- `ekos-notifier test-notify [--emergency]` sends a test message to every notifier.
- Replay ticks the clock in recorded time (`pipeline.simulate`), so timers fire when they would
  have on the night. The service ticks every 10 s.
- Fixed: the writer waited its gathering window before every batch even with a backlog queued.
- Project skeleton: design spec, briefing for contributors, CLI stub, CI, docs site scaffold.
- Planned `ekos-notifier config check` subcommand added to the CLI stub.
- Stub commands exit with code 3 (not 2, which is argparse's usage-error code).
- Design: the GUI uses Starlette (not FastAPI) and the Ekos adapter uses `dbus-fast` (not the
  unmaintained `dbus-next`).
- Design: platform matrix (StellarMate, Astroberry OS, Raspberry Pi OS, Ubuntu, AstroArch); the
  service runs where KStars runs, finds the session bus, and reads INDI host/port from the Ekos
  profile. Flatpak findings from the dev VM recorded in `docs/open-questions.md`.
- Test fixtures: Ekos/INDI D-Bus introspection from KStars 3.8.5 (Flatpak) with Ekos running.
- Developer tools: `tools/record_night.py` (records D-Bus, PHD2 and INDI streams as a replay
  file), `tools/sim_night.py` (drives a simulated night through Ekos), `tools/scrub_replay.py`
  (removes site coordinates and hostnames before a recording is committed).
- `tools/scrub_replay.py`: joins several recordings, trims to a time window (`--since`/`--until`),
  drops non-KStars D-Bus traffic and INDI debug messages (`--keep-noise` keeps them), and scrubs
  site coordinates written as degrees and minutes. `--since`/`--until` take ISO 8601 with an
  offset. The `GEOGRAPHIC_COORD` rewrite now works (it
  never matched JSON-escaped quotes, so elevation was left unscrubbed).
- Design: new Emergency alert "Ekos stopped with the mount unparked", because the Ekos Scheduler
  stops Ekos and disconnects INDI when a job finishes or aborts, even with the mount tracking.
