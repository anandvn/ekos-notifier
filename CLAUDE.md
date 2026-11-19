# Ekos Notifier — project briefing for Claude Code

Read this first. It records the decisions already made so you don't re-litigate them.
The full spec is `docs/design.md`; what still needs verifying is `docs/open-questions.md`.

## What this is

Ekos Notifier is an open-source (MIT) companion service for KStars/Ekos astrophotography rigs.
It runs **on the imaging computer**, watches an imaging night, records it as a *session* in a
local SQLite database, sends alerts worth acting on, sends an hourly status digest, and
carries each night's data home with one button. It **observes only**: it never controls equipment.

Owner: Anand (anandvn on GitHub). He images with KStars/Ekos + PHD2 on StellarMate OS on a
mini PC, QHY Minicam 8, AM5/GM811G mounts, a power box with temperature/humidity sensors,
mostly at dark sites with intermittent internet.

## Decisions already made (don't change without asking)

- **License:** MIT. **Public on GitHub.** Docs and first-run experience are the top priority.
- **Generic:** Every external dependency sits behind an adapter:
  - Notifications: **Apprise** URLs (Pushover is Anand's choice, but nothing hard-codes it).
  - Sync home: **rclone** (SFTP/SMB/USB path/S3/Drive…). No custom HTTP receiver.
  - Power/weather: **INDI properties → canonical metrics** (`power.voltage`, `env.humidity`, …)
    via YAML presets + heuristic detection + manual mapping. No device is ever assumed.
- **Capability gating:** each alert rule declares the metrics it needs; if none are available the
  rule is off and shown as "not available on this rig" — never an error.
- **Local-first:** every event is written to SQLite (WAL) first. Notifier, GUI and sync
  are consumers of the store. No internet = record silently; nothing is lost.
- **Session model:** one session per night; Scheduler targets are segments inside it.
  Starts automatically on Ekos Scheduler start (or first manual Light frame); GUI Start/End override.
  Setup (focus, polar align, calibration, trial guiding) is logged *before* the session and attached.
- **Alert priorities:** Emergency / High / Normal / Quiet. Normal is batched into the hourly
  digest except time-critical ones (target low, twilight, sequence complete).
- **Messages:** plain English. The Olde English style was dropped (2026-11-19) and Terse is
  deferred. Our own messages are translatable (gettext, a `.po` file per language; done in 0.3);
  no English line on Emergencies, which are reviewed instead. KStars in another language is
  handled by reading its log back into English.
- **No heartbeat (dropped 2026-11-20):** a session-only ping alerts every morning and at every
  dark-site link drop; a 24/7 rig pings by cron with no help from us (README recipe).
- **Road to 1.0 (decided 2026-11-20):** 0.3 is the field-test build;
  0.3.x fixes what real rigs find; 1.0 is alerts, digest and session record, field-tested. The
  GUI, export, sync and the reference importer come **after 1.0**.
- **GUI (after 1.0):** Starlette (not FastAPI: pydantic-core is compiled) + server-rendered HTML + htmx +
  uPlot, all assets vendored (no CDN), red night mode, LAN only, password set in first-run wizard. Read-only toward Ekos.
- **Export (after 1.0):** each closed session → immutable folder (`session.json`, `events.ndjson`,
  `frames.csv`, `samples.csv`, sequence file, logs). Versioned JSON Schema.
- **Out of scope for v1:** N.I.N.A./Windows, equipment control, image processing, remote GUI access.

## Tech conventions

- Python **3.10+**, asyncio, nothing that must compile at install time (optional compiled
  speed-ups with a pure-Python fallback, like `dbus-fast`'s, are fine).
  Package source in `src/ekos_notifier/`.
- **Dependencies:** only three at runtime (`dbus-fast`, `apprise`, `PyYAML`). Add one only when
  it replaces something we can't reasonably build ourselves; prefer a little code of our own
  (decided 2026-11-20: no Babel; translations choose their decimal separator).
- Ekos via D-Bus (`dbus-fast`), PHD2 via its JSON event server (TCP 4400), INDI via a light XML
  client (TCP 7624).
- Tests: `pytest`. The **simulation replay** (recorded nights) drives both tests and the user-facing
  "simulate a night" feature. Lint/format with `ruff`.
- CLI entry point: `ekos-notifier` (subcommands planned: `run`, `doctor`, `config check`,
  `indi-dump`, `install-service`, `simulate`).
- Config: `~/.config/ekos-notifier/config.yaml`; `config.example.yaml` in the repo is the commented
  reference. Never commit a real `config.yaml` (it holds notifier keys).
- Keep docs in step with code: a user-visible change updates `docs/` in the same PR.

## Platform gotcha: Flatpak StellarMate

StellarMate's Arch-based release runs KStars/Ekos (and possibly PHD2/INDI) as **Flatpaks**.
Ekos Notifier must run on the host, find KStars data under `~/.var/app/org.kde.kstars/…` as well
as `~/.local/share/kstars/`, and reach D-Bus/ports across the sandbox. It installs into a venv in
the user's home, set up by `install.sh` (decided 2026-11-18, shipped since 0.1.0):
StellarMate images have no pipx and no pacman repositories, so don't plan on pipx or the AUR.
This is unverified — see `docs/open-questions.md`.

## Dev environment

A StellarMate VM (Arch + Flatpak KStars) is available for development with the INDI simulators
(telescope, CCD, focuser, filter wheel, weather) and PHD2's simulator camera. First job on the VM:
work through `docs/open-questions.md` and record findings there and in `docs/design.md`.

## Package name

Renamed from Town Crier / `ekos-towncrier` on 2026-11-17: GitHub repo and PyPI
distribution **`ekos-notifier`** (free on PyPI as of Nov 2026), CLI command `ekos-notifier`,
Python package `ekos_notifier`. The hourly digest is plain "hourly digest" (config `digest:`).
Old recordings (`town-crier-replay/0`) and the old `~/.config/town-crier`, `~/.local/share/town-crier` folders are still understood.
