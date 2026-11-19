# Changelog

All notable changes are recorded here. The format follows Keep a Changelog, and versions follow
Semantic Versioning.

## Unreleased

- **Asking for a device preset works now.** `ekos-notifier indi-dump --device "Your box"` writes
  just that device, small enough to paste into a Device support issue; the whole rig's dump was
  too long to paste, and the issue form couldn't take it as an attachment. The command now says
  what to do with the file, and the form shows the steps.

- **Fixed: `indi-dump` missed humidity sensors that abbreviate it**, like the SVBONY SV241 Pro's
  `SHT40_HUMI`: they're listed as candidates now.

## 0.3.0 — 2026-11-20

The field-test build: the version to take to real rigs before 1.0. Everything Ekos Notifier
says can now be translated, and the plan is settled. 1.0 is alerts, the hourly digest and
the session record, tested on real nights. The web dashboard, carrying data home and the
reference importer come after 1.0, and the planned heartbeat is dropped.

- **Changed: no heartbeat.** The planned dead-man's switch (`heartbeat:` in the config, never
  built) is dropped: pinging only during a session alerts every morning when the night ends, and
  at a dark site every time the internet drops. A rig that runs around the clock can ping a
  service like Healthchecks.io by itself; the README has a one-line recipe ("If the rig itself
  goes silent"). A config that still has `heartbeat:` gets a note that it's ignored.

- **Messages can be translated.** Everything Ekos Notifier says is ready for translation with
  gettext: alerts, the hourly digest and the session summary on your phone, and in the terminal
  `doctor`, `setup`, `--help`, `config check` and `config alerts`, `simulate`, `indi-dump`,
  `install-service` and config errors. A translation is one `.po` file, made with Poedit or
  `msginit`, no Python needed (see CONTRIBUTING.md, "Translating"). The new `language:`
  setting picks it: `auto` (your system's language, the default) or a code such as `de`. No
  translations ship yet, so everything stays in English. Log lines stay English.

- **Wording fixes:** "Room for about 1 more frame" (was "frames"), and the digest's "This
  interval: …" (was "Last this interval: …").

## 0.2.0 — 2026-11-19

The 0.2 "First run" milestone: `ekos-notifier setup` sets up notifications in a few questions,
`ekos-notifier doctor` checks the rig and says how to fix what's wrong, the docs have a site,
and alerts work with KStars in any language it ships. Message styles are dropped: messages are
in plain English.

- **Changed: message styles are dropped.** Messages are in plain English. The Olde English
  style is dropped and Terse is deferred, so the `style:` setting is gone; a config that still
  has it gets a note that it's ignored (delete the line).

- **Fixed: alerts work with KStars in another language.** Ekos translates its log, and some
  alerts read it. With KStars in German, for example, **Autofocus failed** could be missed,
  alerts lost the reason Ekos gave, and stopping Ekos yourself was taken for Ekos being lost.
  Before any rule sees a log line, Ekos Notifier now turns it back into English using KStars's
  own translations, which works for every language KStars ships. Numbers are read in KStars's
  own locale too: German KStars writes a 10 s exposure as `10,000`, which was read as 0. If
  this computer doesn't have that locale, those numbers are left unread and `doctor` says how
  to add it.
  PHD2's tips ("For best results…") are no longer sent as warnings when PHD2 runs in another
  language. `doctor` reports KStars's language. Recordings now note it, and `simulate` replays
  a recording made in another language through the same translations.

- **`ekos-notifier setup`** is a terminal wizard that sets Ekos Notifier up with a few
  questions. It starts by showing what it can see on the rig. It sets up your notifier
  (Pushover, ntfy, Telegram, Discord or any Apprise URL), sending it a test. It also asks about
  the battery and the hourly digest. It saves only when you say so, and changes just those lines
  of the config, keeping your other settings and the comments; the old file is kept as `.bak`.
  The installer offers it when no notifier is set up, and the README's setup guide starts from
  it.

- **`ekos-notifier doctor`** checks everything Ekos Notifier needs and says how to fix what
  isn't right. It checks the install, including a venv broken by an OS update. It checks the
  config: notifier URLs, and a file other users can read. It checks the service: whether it's
  installed, running or failed, and whether its unit is out of date or names a Python that's
  gone. It also checks the database, KStars and Ekos on D-Bus, a KStars Flatpak's permissions,
  the INDI server and what's recognised on it, PHD2, the clock's sync and the free disk space.
  It's read-only. The README's setup steps and troubleshooting start from it.

- **Fixed:** "Mount parked", the all-clear after the "Mount left unparked" Emergency, is sent
  at once. It waited for the hourly digest, which runs only during a session, and that
  Emergency comes after Ekos stops, so it was likely never sent.

- **Docs site** at https://anandvn.github.io/ekos-notifier/, published from main: a **Quick
  start** page (the README's install and setup guide, included rather than copied) and an
  **Alerts reference**: every alert with its config name, default, what sets it off and how to
  tune it, checked against the code by a test.

## 0.1.1 — 2026-11-19

Fixes from the first live rig: battery alerts that didn't fire, a way to see which INDI devices
were recognised, config settings that silently did nothing, and the installer's PATH advice.

- **The log says what INDI devices were recognised**, once they've settled after Ekos
  connects them: "INDI recognised: Power Box (battery voltage, current, outputs); QHY CCD
  (camera temperature)". Again when that changes, and "nothing recognised yet" (with a pointer
  to `indi-dump`) when there's nothing.

- **Battery low is sent at any time**, not only during a session (seen live: a LiFePO4 at
  12.0 V raised nothing with no session running), and both battery levels now count **at or
  under** the level, so exactly 12.0 V on a LiFePO4 is critical.
- **`config check` flags settings that do nothing**: unknown keys in any section, with a
  suggestion ("did you mean 'dew_margin_c'?"), and `low_volts`/`critical_volts` that replace
  the battery preset's levels. They're warnings, not errors; the service logs them at startup
  and on reload too.

- **Fixed:** when `~/.local/bin` isn't on your `PATH`, the installer and the README now say to
  add it to `~/.bashrc` (`~/.zshrc` for zsh), not `~/.profile`: terminals like Konsole don't
  read `~/.profile`, and on StellarMate a `~/.bash_profile` hides it even at login. If the line
  is already there, the installer just says to open a new terminal.

## 0.1.0 — 2026-11-19

The first release: the 0.1 "Core" milestone. Ekos Notifier follows Ekos (D-Bus), PHD2 and
INDI, records each night as a session in a local SQLite database, and sends alerts, an hourly
digest and a session summary to any Apprise notifier. Installed with `install.sh` (a venv in
your home folder, no root). Pre-alpha: not yet field-tested on real rigs, and settings may
change before 1.0. Not yet built: the setup wizard, `doctor`, the web dashboard, sync, the
heartbeat, and the Terse and Olde English styles.

- **Hardening, from a pre-release review:**
  - The notifier survives a failed pass (the database busy, a notifier library raising): it
    logs the traceback, backs off and carries on, instead of stopping the service.
  - Calls to KStars over D-Bus give up after 10 s, so a hung KStars can't pile up tasks.
  - An unexpected error in the Ekos, PHD2 or INDI source is logged with its traceback (a
    connection coming and going is still one line).
  - A broken config at startup no longer stops the service: it records the night without
    alerts, and the fixed file is picked up by itself.
  - A database from a newer version stops the service with exit code 78, which the unit no
    longer restarts on (`RestartPreventExitStatus=78`), instead of retrying every 10 s.

- **Fixed:** `ekos-notifier install-service` run while the service is up (re-installing, or
  an installer upgrade) said another copy was "probably in a terminal" and didn't apply the new
  unit. It now recognises the service and restarts it on the new unit and version; the warning
  is left for a copy that really is running elsewhere.

- **`install.sh`**: installs into a venv in your home folder with no root and no extra tools
  (`curl -fsSL …/install.sh | sh`), links `~/.local/bin/ekos-notifier`,
  writes a commented config and offers the systemd user service. Re-run to upgrade (restarting
  the service; rebuilding the venv after a Python upgrade); `--uninstall` keeps config and
  sessions. **`ekos-notifier config init`** writes the commented example config (mode 600),
  now shipped inside the package. CI runs the installer end to end.
- **Config reload**: the running service applies an edited config by itself within seconds
  (or at once with `systemctl --user reload ekos-notifier`): new notifier URLs, message
  levels and thresholds take effect without losing the night's state, and the INDI connection
  restarts if its settings changed. A config with a mistake is logged and ignored.
- **README**: a step-by-step install and setup guide (notifier URLs, editing the config,
  messages, thresholds, devices, troubleshooting). The example config's commented examples can
  now be used by deleting the `#` (under `messages: {}` that made invalid YAML), and settings
  not used yet are marked so.

- **Clouds** (High): PHD2's guide star fading by half for 2 minutes, or two frames in a row
  losing half their stars, with the numbers; **Clouds passed** when the stars are back. A star
  lost at full brightness isn't clouds (that's Guiding lost). `alerts: clouds_drop_percent`
  (default 50, 0 = off). Thresholds to be tuned on a real cloudy night.

- **Pre-flight checks** in "Starting session": battery, free disk space, dew margin and
  humidity, and the camera against its setpoint; failed checks are listed first as warnings.
  The **session summary** adds median HFR and guide RMS, temperature, humidity and camera
  temperature ranges, the lowest battery voltage and the problems raised. The **hourly digest**
  no longer says "All's well" when no light frame was saved in the hour ("No light frames since
  21:40").

- **Flats reminder** (Normal, with the session summary): filters with lights but no flats tonight,
  once per filter per night. **Darks reminder** (off by default): "cover the telescope" when a
  dark job starts; the Ekos adapter now reads the active Capture job's frame type (`capture.job`).
  `messages:` supports alerts that are off unless turned on.

- Choose which messages you get: a `messages:` section in the config sets any alert to `off`,
  `digest`, `quiet`, `normal`, `high` or `emergency`; all-clears follow their problem when it's
  off or held for the digest. `ekos-notifier config alerts` lists every alert with its default
  and your setting; `config check` flags unknown names (with a suggestion) and lowered
  Emergencies. A registry of every alert (`ekos_notifier.messages`), checked against the code by
  a test.

- **Plate solve failed** (High): an Ekos Align run fails and no solve succeeds within 3 min,
  saying whether it followed the meridian flip or a new Scheduler target, with Ekos's reason;
  "Plate solve succeeded" after.

- `ekos-notifier simulate`: replay a night and print every alert it would send, with your config:
  the sample night (now shipped in the package), a recording, or one of your sessions from the
  database (`--session last`, `--list`; read-only). `--brief` for titles, `--send` to send them
  marked [Simulated] (an Emergency as High). The mount-limit Emergency is planned for after 1.0.

- **Meridian flip failed** and **Meridian flip overdue** (Emergency), with "Meridian flip done"
  after. Ekos's Mount module logs its flips only in its `logText` property (no `newLog` signal),
  which the Ekos adapter now polls every 10 s, so Mount's log lines are recorded too. Overdue: the
  mount still on its pre-flip pier side 15 min plus one exposure after the flip time.
- **Disk low** (High): under `alerts.disk_low_gb` free where Ekos saves frames, with frames and
  time left at tonight's pace; "Disk space OK" after. `alerts.disk_low_gb` is now read.

- **Dew risk** (High): dew margin under `alerts.dew_margin_c` or humidity over 90 % for 5 min,
  naming dew heaters that are off; "Dew risk passed" in the digest. **Cooler can't hold** and
  **Cooler can't reach setpoint** (High), with "Cooler at setpoint" after: the setpoint comes from
  Ekos Capture's log, or from the camera driver reporting it reached its target (new
  `camera.cooler` events). `alerts.dew_margin_c` and `alerts.cooler_tolerance_c` are now read.
- Install plan: an `install.sh` (a venv in your home folder, no root, no pipx) ships with 1.0,
  since StellarMate's images have neither pipx nor pacman repositories. Docs updated.
- **Power boxes, whatever the brand:** a preset for libindi 2.x's standard power interface
  (Pegasus PPB/PPBA/SPB/UPB/UPB3, SVBONY, Terrans, Celestron Dew Power, Gemini PBH, smPro, …):
  input voltage, current and power as metrics, and each output's on/off, current and label
  (`power.output` events). **Battery critical** (Emergency, also outside a session), **Battery
  low** (High, once) and **Battery OK**, with `power.battery` presets (`agm_12v`, `lifepo4_12v`,
  `mains`, `custom`) and `low_volts`/`critical_volts`. **Power output off** and **No current on**
  an output (High) during a session, not when Ekos shuts down; **Power output back**. The digest
  shows "Power 12.4 V (low 12.1 V), 4.2 A". `indi-dump` no longer lists a box's outputs as
  candidates. Verified against a test driver built on libindi 2.2.5's `INDI::PowerInterface`.

- Fixes from the live sky test: "Starting session" now comes before the alerts it leads to;
  "Twilight coming" is sent once per night, not again with every short session; a meridian flip
  (the mount swinging through the pole and back) no longer counts as a new target, so "Target
  low" isn't repeated. Confirmed live: Ekos flipped at 07:07:53, predicted 07:07.
- **Twilight coming** and **Target low** (Normal, immediate) and **Meridian flip soon** (Quiet,
  for the digest), from a small pure-Python ephemeris (`ekos_notifier.ephemeris`: sidereal time,
  Sun, altitudes, crossings). Site from KStars's `location()` (or the mount's INDI
  `GEOGRAPHIC_COORD`), target from the mount's INDI coordinates, limits from Ekos (`[Mount]` flip
  and altitude settings, the Scheduler's altitude constraint). The digest shows time to the flip,
  to the target's altitude limit and to dawn. Config `alerts.twilight_warning_minutes`,
  `alerts.target_low_warning_minutes` are now read.
- No session summary when you stop Ekos yourself (Ekos logs "Disconnecting INDI devices…"
  first): the session closes quietly as `ekos_stopped`. A crash (KStars vanishing, an error, Ekos
  idle with no shutdown logged) closes as `ekos_lost` with a summary. The "mount unparked"
  Emergency is sent either way. After a service restart, a session keeps the time and kind of
  the Ekos stop before it and its summary counts the session's frames (it said "No light
  frames", and the restart time as the end).
- `ekos-notifier install-service`: a systemd user service (`ExecStart=<this Python> -m
  ekos_notifier run`, restart on failure, wanted by `default.target`), enabled and started;
  `--no-start`, `--uninstall`, `--config`. One Ekos Notifier per database: a second copy refuses
  to start instead of sending every alert twice. `python -m ekos_notifier` runs the CLI.
- **INDI adapter** (`ekos_notifier.indi`): a read-only client of the INDI server the Ekos
  profile uses. Presets (YAML, by interface or driver) map device properties to canonical
  metrics: weather stations, camera and focuser temperatures built in; anything else via
  `indi.metrics` in the config. Dew point derived when a device has temperature and humidity.
  `metric`, `indi.device` and `indi.capabilities` events; samples keep their source device
  (schema 4) and frames get the imaging camera's temperature. The digest shows camera
  temperature, ambient, RH and dew margin, and the Capture target for manual sessions.
  `ekos-notifier indi-dump` writes every device's properties (site location removed) and lists
  candidate readings.
- INDI fixes from the live test: a reading stays current until INDI says the device or property
  is gone (INDI sends values only when they change, so a settled cooler goes quiet and frames had
  lost their camera temperature); `indi-dump` also removes user names from home paths and no
  longer lists simulator settings or "…_CURRENT_ENTRY" counters as candidates.
- **Hourly digest** (`ekos_notifier.digest`): on the hour during a session, "HH:MM — All's
  well" (Quiet, sent silently) or "HH:MM — Attention: …" (Normal) when a problem was raised and
  not cleared, or is still open. Target, filter, job progress, lights and integration this
  interval and tonight, calibration, median guide RMS, failed dithers, median HFR, and every
  alert and milestone since the last digest. Config `digest:` (`enabled`, `interval_minutes`,
  `align_to_clock`) is now read (the pre-rename `crier:` section still is). Immediate alerts are
  sent at their own priority, so Quiet ones go out silently instead of being held. A capture
  abort, Scheduler abort or autofocus failure stays "Still open" until things work again, and
  open problems and the session's start survive a service restart.
- More High alerts: **Guiding poor** (follows Ekos's own guide-deviation limit: when a job
  enforces it, the time Ekos holds exposures; otherwise Ekos's guide RMS over that limit; config
  `alerts.guiding_poor_minutes`, default 10), **Capture aborted** (with Ekos's reason; aborts with
  no cause are taken as the user's Stop), **Scheduler aborted**, **Capture stalled** (no progress
  for 2 × (exposure + download), at least 2 min) with "Capture running again", and **Autofocus
  failed** (two runs in a row, Ekos's retry loop counted once). Shaped by live tests: Ekos's
  limit makes it suspend and resume over and over, so the clock runs until a frame finishes; a
  failed autofocus ends `aborted` with the failure only in the log; Capture reported `aborted`
  while idle isn't an abort; PHD2 warnings caused by stopping Ekos are dropped.
- **Guiding lost** alert (High, `ekos_notifier.guiding`): guiding stopped and not back within
  `alerts.guiding_recovery_minutes` (default 5) of time when guiding was expected, then
  **Guiding recovered**. Intent-aware: slews, meridian flips, plate solving, autofocus, flats with
  tracking off, calibration frames and the end of the run pause the clock; a guider stuck
  recalibrating doesn't count as recovered. Uses Ekos's Guide state (internal guider or PHD2)
  and PHD2's own lost-star reports; replays of the M 31 and IC 410 nights send no false alarm.
- **PHD2 warning** alert (High): PHD2's warnings and errors during a session, once per message
  per session, with the current guide RMS; its dark-library advice, info messages and the
  warnings caused by the Scheduler's shutdown aren't sent. Rules now also pick up an active
  session after a service restart (before, guiding alerts went silent until the next session),
  without resending PHD2 warnings already sent in it.
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
