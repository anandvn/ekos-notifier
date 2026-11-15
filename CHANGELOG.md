# Changelog

All notable changes are recorded here. The format follows Keep a Changelog, and versions follow
Semantic Versioning.

## Unreleased

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
