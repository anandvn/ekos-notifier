# Changelog

All notable changes are recorded here. The format follows Keep a Changelog, and versions follow
Semantic Versioning.

## Unreleased

- Ekos adapter (`town_crier.ekos`): finds the session bus KStars is on, listens to Ekos signals
  by the `org.kde.kstars` name (modules that appear later are covered), reports KStars appearing
  and vanishing, reads current states on connect, and reconnects with backoff. Signals become
  `Event`s (`capture.frame`, `mount.park`, `scheduler.state`, …); state codes are named, with
  those checked against a recording marked. `town_crier.replay` runs recordings through the same
  decoder. New dependency: `dbus-fast`.
- Tests driven by the M 31 replay fixture.
- Event store (`town_crier.store`): SQLite in WAL mode with versioned migrations. Every event is
  appended to `events`; `frames` (with the guide RMS at capture time) and `samples` are filled
  in the same transaction. `EventWriter` batches events from asyncio code onto the store's own
  thread and retries failed writes rather than dropping them.
- Session manager (`town_crier.session`): Standby → Setup → Active → Closed from Ekos events;
  tags every event with its session, attaches setup to the session it leads into and late
  calibration frames to the last imaging session. Closes on Scheduler finish + park, GUI End,
  Ekos gone for 10 minutes (a crash and restart doesn't split the night), or the night ending.
  Resumes an open session after a service restart. New `sessions` table (schema 2).
- `town-crier run`: the service in the foreground. Ekos adapter → session manager → store, with a
  clock tick for time-based session rules; SIGINT/SIGTERM stop it cleanly and everything queued
  is written. `--db`, `-v`.
- Fixed: stopping the writer mid-batch or before its database was open dropped queued events.
- Project skeleton: design spec, briefing for contributors, CLI stub, CI, docs site scaffold.
- Planned `town-crier config check` subcommand added to the CLI stub.
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
