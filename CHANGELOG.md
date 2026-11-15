# Changelog

All notable changes are recorded here. The format follows Keep a Changelog, and versions follow
Semantic Versioning.

## Unreleased

- Project skeleton: design spec, briefing for contributors, CLI stub, CI, docs site scaffold.
- Planned `town-crier config check` subcommand added to the CLI stub.
- Stub commands exit with code 3 (not 2, which is argparse's usage-error code).
- Design: the GUI uses Starlette (not FastAPI) and the Ekos adapter uses `dbus-fast` (not the
  unmaintained `dbus-next`).
