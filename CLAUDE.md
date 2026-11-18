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
  - Dead-man's switch: any **ping URL** (Healthchecks.io, Uptime Kuma, Cronitor). Optional.
  - Sync home: **rclone** (SFTP/SMB/USB path/S3/Drive…). No custom HTTP receiver.
  - Power/weather: **INDI properties → canonical metrics** (`power.voltage`, `env.humidity`, …)
    via YAML presets + heuristic detection + manual mapping. No device is ever assumed.
- **Capability gating:** each alert rule declares the metrics it needs; if none are available the
  rule is off and shown as "not available on this rig" — never an error.
- **Local-first:** every event is written to SQLite (WAL) first. Notifier, GUI, heartbeat and sync
  are consumers of the store. No internet = record silently; nothing is lost.
- **Session model:** one session per night; Scheduler targets are segments inside it.
  Starts automatically on Ekos Scheduler start (or first manual Light frame); GUI Start/End override.
  Setup (focus, polar align, calibration, trial guiding) is logged *before* the session and attached.
- **Alert priorities:** Emergency / High / Normal / Quiet. Normal is batched into the hourly
  digest except time-critical ones (target low, twilight, sequence complete).
- **Message styles:** Plain (default), Terse, **Olde English**. Jinja templates in `styles/<name>/`.
  Emergency messages always end with a plain-English line, whatever the style.
- **GUI:** Starlette (not FastAPI: pydantic-core is compiled) + server-rendered HTML + htmx +
  uPlot, all assets vendored (no CDN), red night mode, LAN only, password set in first-run wizard. Read-only toward Ekos.
- **Export:** each closed session → immutable folder (`session.json`, `events.ndjson`,
  `frames.csv`, `samples.csv`, sequence file, logs). Versioned JSON Schema.
- **Out of scope for v1:** N.I.N.A./Windows, equipment control, image processing, remote GUI access.

## Tech conventions

- Python **3.10+**, asyncio, nothing that must compile at install time (optional compiled
  speed-ups with a pure-Python fallback, like `dbus-fast`'s, are fine).
  Package source in `src/ekos_notifier/`.
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
Ekos Notifier must run on the host (pipx), find KStars data under `~/.var/app/org.kde.kstars/…`
as well as `~/.local/share/kstars/`, and reach D-Bus/ports across the sandbox.
This is unverified — see `docs/open-questions.md`.

## Dev environment

A StellarMate VM (Arch + Flatpak KStars) is available for development with the INDI simulators
(telescope, CCD, focuser, filter wheel, weather) and PHD2's simulator camera. First job on the VM:
work through `docs/open-questions.md` and record findings there and in `docs/design.md`.

## Package name

Renamed from Town Crier / `ekos-towncrier` on 2026-11-17 (Anand's decision): GitHub repo and PyPI
distribution **`ekos-notifier`** (free on PyPI as of Nov 2026), CLI command `ekos-notifier`,
Python package `ekos_notifier`. The hourly digest is plain "hourly digest" (config `digest:`);
the Olde English message style stays as an option. Old recordings (`town-crier-replay/0`) and
the old `~/.config/town-crier`, `~/.local/share/town-crier` folders are still understood.
