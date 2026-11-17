# Ekos Notifier — Design Spec

Exported from the working design doc, Nov 2026. This file is the source of truth from here on;
update it alongside the code.

## Overview

Ekos Notifier is an open-source (MIT) companion service for KStars/Ekos rigs: it watches an imaging night, records it as a session in a local database, sends alerts that matter, and carries the night's data home. It runs on the imaging computer itself and depends on no specific server, notification service or network.

**Goals**

- Alerts that are worth acting on: equipment risk and lost data, not noise.
- A complete, reproducible record of every night: plan, setup quality, conditions, every frame and event.
- Works fully offline at a dark site; internet is a bonus, never a requirement.
- Gets the data home with one button (or automatically) to whatever storage the user owns.
- Docs and first-run experience good enough that a stranger is set up in 15 minutes.

**Non-goals (v1)**

- Controlling equipment. Ekos Notifier observes and reports; Ekos stays in charge.
- N.I.N.A./Windows support.
- Image processing or stacking.

## Workflow and session lifecycle

A session starts automatically when the Ekos Scheduler starts (or the first Light frame of a manual sequence), with Start/End buttons in the GUI as override. One session = one night; each Scheduler target is a target segment inside it.

### States

1. **Standby** — service started at boot by systemd. Nothing is sent except the critical-battery Emergency.
2. **Setup** — entered when Ekos connects the equipment profile. A setup log records in the background while the user focuses, polar aligns and calibrates. The GUI shows it as a live checklist.
3. **Active** — entered on Scheduler start (or first manual Light frame, or GUI Start). Snapshot, pre-flight checks, then alerting, the hourly digest and heartbeat begin.
4. **Closed** — when the imaging run ends, on GUI End, or on Ekos disconnect. Summary sent, heartbeat and digest stopped, session exported for sync. If Ekos went away while the mount was unparked, the "Ekos stopped with the mount unparked" Emergency is sent too, whatever the state.
   - *The imaging run ends* when the Scheduler stops after running (finished or aborted), or, with no Scheduler, when Capture reports its sequence complete (with the Scheduler, Capture reports that once per job, so it's ignored). Parked or not: the summary says the mount's last known state, and the Emergency covers an unparked mount. The summary arrives within seconds of the last frame. A later run the same night starts a new session.
   - Snapshot readings (taken when Ekos Notifier connects) never close a session: after a restart mid-run, the Scheduler's snapshot has read `idle` while it was running.
   - *Ekos disconnect* means Ekos stays gone for a grace period (default 10 min), so a KStars crash followed by a restart doesn't split the night; the session's end time is when Ekos went away. The Emergency has its own 2-minute timer and isn't delayed by this.
   - A session still open at local noon after its night closes as `night_ended`, at its last activity.
   - A session that closes from Setup without ever imaging is recorded as `abandoned`.
   - A session can start straight from Standby (Ekos Notifier started mid-night, or no Ekos connect seen). After a service restart, the open session is picked up from the store.

The rules are implemented in `ekos_notifier.session.SessionManager`, which is driven only by events and the clock, so replayed nights test the same decisions as live ones.

### Setup log (recorded during Setup)

| Item | Source | Default green threshold |
| --- | --- | --- |
| Polar alignment error (az, alt, total) | KStars log / `.analyze` file (not exposed over D-Bus, verified on 3.8.5) | < 1′ total |
| Focus runs: position, HFR, filter, temperature | Ekos Focus D-Bus | — |
| Guide calibration: RA/Dec rates, orthogonality, camera, binning | PHD2 `get_calibration_data`, or Ekos internal guider | orthogonality within 5° |
| Trial guiding RMS (last few minutes) | PHD2 events / Ekos Guide | < 0.8″ |

### Snapshot taken at session start

- **Target:** name, J2000 RA/Dec; reference FITS path, file hash, solved centre and rotation.
- **Sequence:** raw .esl/.esq XML stored verbatim, plus parsed summary (filters × exposures × counts, gain/offset, binning, cooler setpoint).
- **Constraints:** minimum altitude, moon separation, twilight rules.
- **Optical train:** scope, focal length incl. reducer, camera, pixel scale, field of view.
- **Conditions:** site lat/long/elevation, moon phase and separation, ambient temperature, humidity, dew point, battery voltage.
- **Software:** KStars, INDI, PHD2 and OS versions.

### Pre-flight checks

Camera at setpoint · guiding settled · battery and disk above threshold · dew margin · system clock plausible (NTP/GPS synced) · notifier and heartbeat reachable (skipped silently when offline; see Offline behavior). Failed checks become warnings in the Starting session message; they never block the session.

### Calibration frames

Darks, flats and bias never start a session. They are recorded as calibration runs linked to the most recent session.

### User notes

A GUI text box saves timestamped notes into the session ("seeing poor, breeze from NE after midnight").

## Alerts

Every alert maps to one of four priorities; Emergency and High send immediately, Normal is batched into the hourly digest unless time-critical. All thresholds are config defaults.

| Priority | Delivery | Meaning |
| --- | --- | --- |
| Emergency | Immediate, repeats until acknowledged (Pushover: retry 60 s, expire 1 h) | Equipment at risk or rig silent |
| High | Immediate, once, bypasses quiet hours | The night's data is in trouble |
| Normal | Batched into the digest; time-critical ones immediate | Milestones and manual steps |
| Quiet | Silent / in-app only | Nice to know |

Priorities map onto each notifier's native levels where it has them (Pushover −2…2, ntfy 1–5); notifiers without levels prefix the title.

### Emergency

| Event | Default trigger |
| --- | --- |
| Mount nearing a limit with nothing capturing | Tracking within 15 min of meridian/pier limit or horizon after sequence end |
| Meridian flip failed | Flip error, or past hour-angle limit without flipping |
| Battery critical | < 11.5 V (12 V LiFePO4/AGM preset) |
| Ekos stopped with the mount unparked | Ekos stops or disconnects INDI (Scheduler finish/abort, crash, or user) while the last known mount state was unparked, and Ekos hasn't reconnected within 2 min. Uses the last known `parkStatus`, since the mount's properties vanish with Ekos. Clears when the mount is seen parked again. |
| Rig or uplink silent | External heartbeat missed past grace period (sent by the heartbeat service, not the rig) |

### High

| Event | Default trigger |
| --- | --- |
| Guiding lost | Guiding stopped (Ekos Guide aborted, reacquiring, calibration or dither error, or PHD2 lost the star) and not guiding again within 5 min of time when guiding was expected; looping or recalibrating doesn't count as recovered. The clock pauses, and restarts from zero, while the mount slews or parks, Capture flips, solves or focuses, Focus or Align is busy, calibration frames are being taken, or capture had already stopped before guiding did. Capture *suspended*, or *aborted* by Ekos because guiding stopped, doesn't pause it; nor does a mount that stopped tracking (the message says so). Message names PHD2's lost-star reason and alert if any. Followed by "Guiding recovered" (Normal, immediate). |
| PHD2 warning | PHD2 raises a warning or error during an active session (e.g. "not able to make sufficient corrections in RA", "INDI camera disconnected"). Once per message per session, with Ekos's current guide RMS. PHD2's advice (the dark-library tip, which it also tags as a warning), info messages, and warnings caused by Ekos shutting down aren't sent: a warning is held 10 s and dropped if Ekos logs "Disconnecting INDI devices", INDI or Ekos goes idle, or capture completed (the Scheduler's end), since PHD2 warns about the camera and mount just before INDI reports idle (seen live). |
| Guiding poor | Follows Ekos's own guide-deviation limit (per sequence job; default `[Capture] GuideDeviation` in `kstarsrc`, 2″). When the job enforces it, Ekos suspends an exposure when deviation goes over the limit ("Guiding deviation 3.791 exceeded limit value of 0.5 arcsecs…"), resumes once under a separate, looser resume limit (2″ by default), and can trip again seconds later, so no exposure ever finishes (seen live): alert when Ekos has been suspending exposures and no frame has finished for `alerts.guiding_poor_minutes` (default 10); the all-clear comes when a frame finishes. When the job doesn't enforce it, Ekos keeps exposing: alert when Ekos's total guide RMS (√(RA² + DEC²), arcsec) has stayed over that limit for the same time while guiding. Dithering doesn't restart the window; guiding stopping does. Followed by "Guiding back within limits" (Normal). `guiding_poor_minutes: 0` turns it off. The Ekos path matches Ekos's English log messages only. |
| Capture aborted | A running capture aborted and not restarted within 60 s (Ekos also reports "aborted" for an idle Capture when a focus exposure fails; that isn't counted), when Ekos gave a reason (its log just before, e.g. "Autoguiding stopped. Aborting...", "Autofocus failed. Aborting exposure", or guiding, focus or a device failing in the seconds before). An abort with no cause is taken as the user's own Stop and only recorded: a Stop logs only "Camera capture stopped" (seen live). |
| Scheduler aborted | The Scheduler goes `aborted` after running (Stop leaves it `idle`); quotes its last log line. |
| Capture stalled | Capture busy (exposing, downloading, changing filter, starting a job, dithering) with no progress for 2 × (exposure + download), at least 2 min. Exposure from Ekos's "Capturing 60.000-second…" log, download time from its "Download Time" log. Waiting states (guider drift, suspended, temperature, focus, flip) and Ekos going away don't count. Followed by "Capture running again" (Normal) at the next frame. |
| Autofocus failed | 2 consecutive failed runs. On KStars 3.8.5 a failed run ends `aborted`, not `failed`, with "Autofocus failed, moving back to initial focus position…" in its log (seen live); a run counts as failed if it ends `failed`, or `aborted` with that line. Ekos's own retries ("Restarting autofocus process…") are part of the run, so a failed ending is counted 10 s later unless a restart follows; a run whose retry completes is a success and resets the count. An `aborted` with no failure logged is the user's Abort. |
| Plate solve failed | After a flip or target change |
| Dew risk | Ambient − dew point < 2 °C, or RH > 90 %; flags dew heaters off or low while humid |
| Battery low | < 12.0 V, once, with runtime estimate |
| Power port fault | Overcurrent or port fault reported by power box |
| Cooler can't hold | > 2 °C off setpoint for 10 min |
| Clouds | Star lost + HFR jump + star count drop together; one alert, one all-clear |
| Disk low | < 10 GB free |

### Normal — manual steps

| Event | Default trigger | Timing |
| --- | --- | --- |
| Target low | 30 min before altitude/horizon limit | Immediate |
| Twilight coming | 45 min before astronomical dawn | Immediate |
| Sequence complete | Sequence ends; starts the parking escalation if mount still tracking | Immediate |
| Flats reminder | Lights done for the night | Digest |
| Darks reminder (optional, off by default) | Dark sequence started | Digest |

### Normal — milestones

"Starting session" and the session summary are sent at once rather than waiting for the digest.

Starting session (with pre-flight results) · first sub saved (thumbnail) · meridian flip complete · target switched (Scheduler) · session summary at close (totals per filter, integration, median HFR, guide RMS, aborts, min battery, temperature/humidity range, sessions awaiting sync).

### Quiet

Meridian flip in 15 min.

### Smart-alert rules

- **Intent-aware:** guiding stops caused by Ekos itself (flip, autofocus, dither) are never failures.
- **Debounce:** a condition must persist past its window before alerting; a lost star that recovers in seconds is silent.
- **Collapse and clear:** a repeating problem sends one alert, then one "recovered after N min".
- **Correlate:** concurrent star-lost, HFR jump and star-count drop become one Clouds alert.
- **Thumbnails:** a 100–200 KB stretched JPEG attaches to first-sub and summary messages where the notifier supports images.

## Hourly digest and message styles

The digest is one status message per interval (default 60 min) while a session is Active, folding in any held Normal events. Off in the GUI = Normal events send individually.

### Digest

- **Title carries the verdict:** "23:00 — All's well" or "23:00 — Attention: dew margin 1.4 °C".
- **Priority:** Quiet when all's well; Normal when it says Attention.
- **Timing:** on the clock hour, or every N minutes from session start (configurable). A digest due less than 10 minutes after the session started is skipped.
- **Verdict:** Attention when a High or Emergency alert was raised in the interval and not cleared in it, or a problem raised earlier is still open: one with an all-clear alert until it's sent (guiding lost, guiding poor, capture stalled, mount left unparked), and capture aborted, Scheduler aborted and autofocus failed until capture, the Scheduler or focus is working again (seen live: without this, "All's well" with nothing captured since an abort). The problems are named in the title and carry over as "Still open", including across a service restart (rebuilt from the session's alerts, then closed by Ekos's snapshot if no longer true).
- **Implemented so far** (`ekos_notifier.digest`, plain style): target (from the Scheduler's job), filter and job progress (from Capture's "has N/M frames captured"), lights and integration this interval and tonight, calibration frames, median guide RMS, failed dithers, median HFR when Ekos measures it, and "Since last": every alert (sent or held) and milestones such as a completed meridian flip. Temperatures, battery, dew, flip and dawn times wait for the INDI adapter and the site location; Terse and Olde English for the template sets.
- **Body:** target · filter · frames done/planned (+ this interval) · median HFR · guide RMS · camera temp · battery and trend · ambient, RH, dew margin · time to flip and dawn · events since last digest · thumbnail.

```
IC 1396 · Ha · 42/60 (+11 this hr)
HFR 2.1 (med) · RMS 0.62" · 1 dither fail
Cam −10.0 °C · Batt 12.7 V (−0.2/hr)
Amb 14.2 °C · RH 78% · Dew Δ 4.1 °C
Flip in 1h40 · Dawn in 6h05
Since last: flip complete 22:31
```

### Message styles

Every message is rendered from a template set; the style is a single config setting. Numbers and units are identical across styles, only the wording changes.

| Event | Plain (default) | Terse | Olde English |
| --- | --- | --- | --- |
| Session start | Starting session — IC 1396 | ▶ IC 1396 | Hear ye, hear ye! The watch beginneth upon IC 1396 |
| Digest, OK | 23:00 — All's well | 23:00 OK | Eleven of the clock, and all is well |
| Digest, warning | 23:00 — Attention: dew margin 1.4 °C | 23:00 ⚠ dew 1.4 °C | Eleven of the clock — the dew draweth nigh (1.4 °C) |
| Guiding aborted | Guiding aborted, not recovered after 5 min | Guide abort 5m | Hark! The guide star hath fled these five minutes |
| Battery low | Battery low: 11.9 V, ~2 h left | Batt 11.9V ~2h | The cells grow faint: 11.9 volts, scarce two hours remain |
| Mount near limit | Mount still tracking, 12 min to pier limit | MOUNT 12m to limit | Alarum! The mount strayeth toward the pier, twelve minutes hence |
| Summary | Session complete: 142 frames, 11 h 50 m | Done 142f 11h50m | The night's labour is done: 142 frames, eleven hours and fifty minutes of light |

- **Safety rule:** Emergency messages in any style carry the plain-English line as their last line, so a stylised alert is never ambiguous at 3 a.m.
- **Community styles:** templates live in `styles/<name>/` (Jinja); new styles (pirate, Shakespearean, other languages) are a pull request away. Plain English doubles as the base for translations.

## Offline behavior

No internet never stops or degrades recording; it only silences outbound messages.

- **Offline at session start:** notifier and heartbeat are skipped without warning. GUI shows "Offline — recording only".
- **Link drops mid-session:** recording continues. An alert that can't be delivered is retried for a short window (default 10 min, `notify_expire_minutes`), then dropped rather than sent late: alerts arriving after the fact can't be acted on, and a backlog of them on reaching home is noise. This includes the session summary. Dropped alerts stay in the local record (GUI, export); the outbox marks them `dropped` with the reason.
- **Heartbeat caveat:** an external heartbeat service can't tell a dead rig from a dead uplink and will alert either way. Mitigations: grace period configurable (default 30 min); heartbeat is a per-session toggle that defaults on only if connectivity looked solid at session start; message wording says "rig or uplink".
- **Local truth:** everything is in the local database first; outbound delivery is a best-effort consumer of it.

## Data model and export format

SQLite (WAL mode) on the rig is the source of truth; a closed session is exported once as an immutable folder whose format is documented and versioned so anyone can write an importer.

### Local tables

| Table | Holds |
| --- | --- |
| sessions | id (UUID), state, start/end, site, software versions, snapshot JSON, summary JSON, synced_at |
| targets | session_id, name, RA/Dec, sequence XML, reference FITS info, start/end |
| setup_items | session_id, kind (polar_align, focus, calibration, trial_guide), values JSON, ts |
| frames | event_id, session_id, target_id, ts, type, filter, exposure, HFR, star count, guide RMS, camera temp, file path |
| samples | ts, metric, value (ambient, RH, dew point, voltage, current, guide error) |
| events | id, session_id, ts, source, kind, priority (alerts only), data JSON |
| outbox | event_id, sink, status, attempts, sent_at |
| notes | session_id, ts, text |
| calibration_runs | linked session_id, type, filter, exposure, count |

`events` is append-only and holds every event as the adapters reported it (`capture.frame`,
`mount.park`, `guide.rms`, `phd2.step`, …); `frames` and `samples` are filled from events in the same
transaction. The database lives at `~/.local/share/ekos-notifier/ekos-notifier.db` (honouring
`XDG_DATA_HOME`), in WAL mode so readers never block the writer, and its schema is versioned with
`PRAGMA user_version` migrations. Implemented so far: `events`, `frames`, `samples`; the other
tables arrive with the session manager, notifier and GUI.

### Export folder (written at session close)

```
sessions/2026-11-07_IC1396_a3f9/
  session.json      # snapshot, setup log, targets, notes, summary; schema_version
  events.ndjson     # every event, one per line
  frames.csv        # per sub: time, filter, exp, HFR, stars, RMS, temps
  samples.csv       # environmental and power time series
  sequence.esq      # raw plan(s)
  logs/             # Ekos .analyze + PHD2 guide log for the night
```

- **Schema:** `session.json` validated by a published JSON Schema; `schema_version` follows semver.
- **Privacy:** export option to round site coordinates (e.g. to 0.1°) or omit them.
- **Retention:** local copies pruned N days (default 30) after `synced_at`.

## Syncing data home

Sync is a file copy with rclone: every unsynced session folder (and optionally the FITS files) goes to one configured destination, one night or ten alike.

1. **Trigger:** the GUI Flush button, or automatically when the destination becomes reachable (periodic probe; optional NetworkManager dispatcher hook for instant start on connect).
2. **Copy:** `rclone copy` of `sessions/` to the destination. Interrupted copies resume next time; repeats are harmless.
3. **Mark:** a session gets `synced_at` only after rclone exits successfully.
4. **FITS (optional):** second copy of the Ekos capture directory to its own destination path; a checkbox per flush.

### Destinations (any rclone backend)

| Destination | Typical user |
| --- | --- |
| SFTP to a NAS or Linux box | Homelab owners |
| SMB share | Windows / Synology / QNAP households |
| Local path (USB drive) | No home network needed: plug in, flush, carry |
| S3 / Backblaze B2 | Cloud archive |
| Google Drive, Dropbox, OneDrive | Casual users |

- **Security guidance in docs:** a dedicated destination user and SSH key; for SFTP, restrict the key to one directory (`rrsync` or chroot).
- **Downstream:** importers read the export folders, keyed by session UUID so re-imports never duplicate. A reference importer (SQLite/Postgres) ships in `contrib/`; the rig never talks to anyone's database.

## Web GUI

A phone-first web page served by the service on the rig's local network, read-only toward Ekos, with a red night-mode theme on by default after sunset.

| View | Contents |
| --- | --- |
| Now | State, target, filter, frames done/planned, HFR, guide RMS, camera temp, battery, dew margin, time to flip/dawn, Start/End session buttons |
| Setup | Live checklist: polar alignment, focus, calibration, trial guiding, each green/amber against thresholds |
| Charts | HFR per frame by filter; guide RMS and RA/Dec error; temperature, RH, dew point; battery voltage |
| Latest frame | Stretched JPEG preview |
| Events | Filterable log; shows what was sent where and when |
| Notes | Add timestamped notes to the session |
| Sessions | Past sessions with summaries and sync status |
| Sync | Unsynced count, destination reachable?, last flush, Flush button with progress, FITS checkbox |
| Settings | Notifier test, thresholds, digest interval, message style, heartbeat, sync destination, units |

- **Stack:** Starlette, server-rendered HTML, htmx for live updates, uPlot for charts; all assets vendored, no CDN.
- **Never slows capture:** the GUI only reads SQLite (WAL), and all work runs at low priority.
- **Access:** local network only; password set during first run; configurable port that avoids StellarMate's own services.
- **Remote viewing:** out of scope; docs point to Tailscale for users who want it.

## Integrations and plug-in points

Every external dependency sits behind a small adapter interface with a sensible default, so no user is tied to any service or brand.

| Plug-in point | v1 implementations | Example config |
| --- | --- | --- |
| Event sources | Ekos D-Bus (Capture, Guide, Focus, Align, Mount, Scheduler); PHD2 event server (TCP 4400); INDI client for device properties | on by default, auto-detected |
| Guiding | PHD2 or Ekos internal guider | auto-detected from Ekos profile |
| Power and weather | INDI properties with presets: Pegasus PPB/UPB, PrimaLuce Eagle, generic weather; custom property mapping | `power: preset: pegasus_ppb` |
| Notifications | Apprise URLs (Pushover, ntfy, Telegram, Discord, Slack, Gotify, email, Home Assistant, 100+) | `notify: ["pover://user@token"]` |
| Heartbeat | Any ping URL: Healthchecks.io (hosted or self-hosted), Uptime Kuma push, Cronitor | `heartbeat: url: https://hc-ping.com/…` |
| Sync | rclone remote or local path | `sync: dest: "nas:astro/sessions"` |
| Message styles | Plain, Terse, Olde English template sets | `style: olde` |

- **Battery presets:** LiFePO4 12 V, AGM 12 V, AC mains (voltage alerts off), custom thresholds.
- **Units:** °C/°F, guide error in arcseconds or pixels, 12/24 h time.
- **Adding an adapter:** documented interface + example in `contrib/`; adapters register through Python entry points.

## Device support and graceful degradation

No device is assumed: every rule declares the metrics it needs, and a rule whose inputs no device provides is switched off and shown as "not available on this rig", never an error. Guiding, capture, focus and mount alerts need only Ekos and PHD2, so every user gets them.

### Canonical metrics

Adapters translate device properties into canonical names; rules never see device names.

| Metric | Used by | Fallback |
| --- | --- | --- |
| `power.voltage`, `power.current` | Battery low/critical, runtime estimate | None: battery alerts off |
| `power.port_fault` | Port fault | None |
| `env.temperature`, `env.humidity` | Dew risk, digest | Any INDI weather device or USB temp/RH sensor |
| `env.dewpoint` | Dew risk | Derived from temperature + humidity (Magnus formula) |
| `power.dew_heater` | Dew heater check | Check skipped |

When several devices report the same metric, a configurable priority order picks the source.

### Device detection (three tiers)

1. **Preset:** matched by INDI driver name; mapping verified.
2. **Heuristic:** numeric properties whose elements look like VOLTAGE, TEMPERATURE, HUMIDITY, DEW_POINT; the wizard asks the user to confirm each match.
3. **Manual:** property mapping in config.

### Implemented so far (`ekos_notifier.indi`)

- **Client:** read-only. It sends `getProperties` only (no `new*`, BLOBs never enabled), to the INDI server of the active Ekos profile (`userdb.sqlite` `profile` `host`/`port`; empty means local mode on localhost:7624; `indi.host`/`indi.port` in config override it). It retries quietly while the server is down (it only runs while Ekos is connected).
- **Metrics:** presets match devices by INDI interface bits (`DRIVER_INTERFACE`: 2 camera, 8 focuser, 128 weather, …, checked against the simulators) or driver executable, and map `PROPERTY.ELEMENT` to canonical metrics. Built-in and verified on the simulators: weather stations (`WEATHER_PARAMETERS` temperature, humidity, dew point), camera temperature (`CCD_TEMPERATURE`) and focuser temperature. Anything else is mapped in config, `metric: "Device / PROPERTY.ELEMENT"`. The dew point is derived (Magnus) for a device with temperature and humidity but none of its own.
- **Events:** `metric` (`metric`, `value`, `device`, `via`) when a value moves past a small deadband or at least once a minute; `indi.device` on connect/disconnect; `indi.capabilities` (metric → devices) whenever the set changes, which it does as Ekos connects and disconnects equipment. Samples are stored with their source device (schema 4), and each frame gets the imaging camera's temperature (Ekos Capture's `camera`, or the name in its log lines).
- **Digest:** camera temperature, ambient, RH and dew margin.
- **`ekos-notifier indi-dump`:** every device's properties to a file, site location removed, with what's recognised and the readings that could be mapped.
- **Not yet:** power-box presets (need `indi-dump` output from real devices), heuristic detection with confirmation (needs the wizard), stale-metric notices, and the alerts that use the metrics (dew risk, battery, cooler).

### Presets as data

- Presets are YAML files in `src/ekos_notifier/indi/presets/`, contributed by pull request with no Python required.
- `ekos-notifier indi-dump` writes a device's full property list for attaching to a "support my device" issue.
- Each preset is labelled verified, community or untested; the wizard shows the label.

### Isolation and stale data

- Each adapter runs as its own supervised task; a crash or disconnect restarts it with backoff and never stops the service.
- A metric not updated within N minutes is stale; rules never fire on stale inputs. One "sensor lost" Normal notice is sent instead. Exception: "Ekos stopped with the mount unparked" deliberately fires on the last known park state, because losing that state is the event.

### Software versions

Ekos and PHD2 adapters detect the running version and report their actual capabilities. The supported KStars and PHD2 range is documented, and `doctor` warns outside it.

### Test matrix (CI)

Simulation replays run against: no power box · weather station only · temp/RH sensor only · each preset · device disconnecting mid-night · internal guider instead of PHD2 · Flatpak StellarMate (Arch). Community `indi-dump` files become fixtures, so a change that breaks someone's device fails the build.

## Configuration, first run and documentation

Top priority: a new user goes from install to a test alert on their phone in under 15 minutes, without reading source code.

### Configuration

- One YAML file (`~/.config/ekos-notifier/config.yaml`), fully commented, every key with a default.
- The GUI Settings view edits the same file; `ekos-notifier config check` validates it and explains errors in plain words.

### First-run wizard (GUI, opens automatically on first start)

1. **Welcome + password** for the GUI.
2. **Detect:** finds KStars/Ekos, PHD2, INDI devices; shows what it found in plain words ("PHD2 found — guiding alerts on").
3. **Notifications:** pick a service from a list, paste the key, press Send test. Skippable (record-only mode).
4. **Power:** pick power box preset and battery type, or "mains power".
5. **Heartbeat (optional):** explain the dead-man's switch in two sentences, paste a ping URL, test.
6. **Sync (optional):** pick destination type, guided rclone setup, Test write.
7. **Style + digest:** message style (with live preview, including Olde English), digest interval.
8. **Done:** a checklist of what's on, and "Simulate a night" to see sample alerts.

### Built-in safety nets

- `ekos-notifier doctor`: checks D-Bus access, PHD2 port, INDI connection, clock sync, disk, notifier, sync destination; prints fixes.
- **Simulation mode:** replays a recorded night so users can tune alerts in daylight; also drives the test suite.

### Documentation plan (MkDocs site on GitHub Pages)

| Page | Purpose |
| --- | --- |
| README | One screenshot, what it does, 3-command install, link to quick start |
| Quick start | Install → wizard → test alert, with screenshots |
| How a night works | The session lifecycle in pictures |
| Alerts reference | Every alert, trigger, default threshold, how to tune |
| Notifier guides | Pushover, ntfy, Telegram, Discord, email — one short page each |
| Sync guides | NAS over SFTP, USB drive, SMB, cloud |
| Power box presets | Supported devices and custom mapping |
| Export format | Folder layout + JSON Schema, importer guide |
| Troubleshooting / FAQ | `doctor` output explained, common StellarMate/Astroberry issues |
| Contributing | Adapters, message styles, presets, dev setup with simulation |

## Architecture, packaging and platforms

One Python process on the imaging computer: adapters feed events in, SQLite stores everything first, and every output is a consumer reading from the store.

The PHD2 adapter (`ekos_notifier.phd2`) reads PHD2's event server and sends nothing; PHD2 serves several clients, so Ekos's connection is unaffected. PHD2's connect burst (`Version` … `AppState`) is recorded as a snapshot, and the guider state (`stopped`, `looping`, `calibrating`, `guiding`, `lost_lock`, `paused`) is tracked from the events that follow. Guide steps are recorded at PHD2's rate (about one a second), about 240 bytes each in the database: some 5 MB for a 6-hour night. It reads which guider Ekos uses from the running KStars (Flatpak or native): the selected equipment profile's guider (`userdb.sqlite` `profile`, selected by `kstarsrc` `[Ekos] profile`) and the Guide module's setting (`kstarsrc` `[Guide] GuiderType`, `PHD2Host`, `PHD2Port`), which Ekos sets from the profile when it starts. If either says PHD2, it connects to that host and port. Only when both say another guider (internal or lin_guider) does it leave PHD2 alone, re-reading the settings every 30 s so a switch mid-night is picked up; the choice is recorded as an `ekos.guider` event. When it can't tell (KStars not running yet), or PHD2 isn't running, it retries quietly.

```
 Sources                  Ekos Notifier service                Outputs
 ┌──────────────┐        ┌──────────────────────┐         ┌────────────────────┐
 │ Ekos (D-Bus) │──────▶ │ Session manager      │    ┌──▶ │ Notifier (Apprise) │
 └──────────────┘        │          │           │    │    └────────────────────┘
 ┌──────────────┐        │          ▼           │    │    ┌────────────────────┐
 │ PHD2 events  │──────▶ │ Rules engine         │    ├──▶ │ Heartbeat ping     │
 └──────────────┘        │          │           │    │    └────────────────────┘
 ┌──────────────┐        │          ▼           │    │    ┌────────────────────┐
 │ INDI devices │──────▶ │ SQLite (WAL) ────────┼────┼──▶ │ Web GUI            │
 └──────────────┘        └──────────────────────┘    │    └────────────────────┘
                                                     │    ┌────────────────────┐
                                                     └──▶ │ Export + sync      │
                                                          └────────────────────┘
```

Because outputs only read from the store, a dead notifier, network or sync target can never lose data or block capture.

- **Language and runtime:** Python 3.10+, asyncio; `dbus-fast` for Ekos, raw sockets for PHD2, a light INDI XML client. Nothing that must compile at install time (dbus-fast's optional speed-ups fall back to pure Python).
- **Platforms:** StellarMate OS (including the Arch/Flatpak release), Astroberry, any Linux with KStars; x86-64 mini PCs and Raspberry Pi (ARM64).
- **Runs where KStars runs.** Ekos is observed over the local session D-Bus, so Ekos Notifier must run on the computer running KStars/Ekos. That computer doesn't have to host the INDI server. The INDI host and port come from the active Ekos profile. A Linux laptop running KStars against a remote INDI Pi works if Ekos Notifier is on the laptop. KStars on Windows or macOS is out of scope.
- **Session bus discovery:** `DBUS_SESSION_BUS_ADDRESS`, then `/run/user/<uid>/bus`, then the address in the running `kstars` process's environment. `doctor` reports which one worked.
- **Start at boot:** `install-service` enables lingering for the user so the service runs without a desktop login.

| Platform | Base / Python | KStars | Notes |
| --- | --- | --- | --- |
| StellarMate OS 2.3+ | Arch, rolling (3.14 on dev VM) | Flatpak (3.8.5 on dev VM) | Data in `~/.var/app/org.kde.kstars/data/kstars`; PHD2/INDI native on dev VM |
| StellarMate OS ≤ 2.2 | Debian-based | native (3.8.3 in 2.2.0) | Native paths |
| Astroberry OS v3 | Raspberry Pi OS / Debian 13, 64-bit (3.13) | native (3.8.2 in v3.1) | Session-bus discovery to verify |
| Astroberry Server (≤ 2.x) | Raspberry Pi OS Buster, 32-bit (3.7) | 3.5–3.6 | **Unsupported:** Python too old, project retired |
| Raspberry Pi OS Bookworm/Trixie | Debian 12/13 (3.11/3.13) | PPA/source/distro | Supported |
| Ubuntu 22.04 / 24.04 | 3.10 / 3.12 | Mutlaq PPA | Supported |
| AstroArch | Arch ARM, rolling | native, latest | Supported |
| Debian 11 and older | 3.9 or older | — | Unsupported (Python < 3.10) |
| KStars snap | any | snap | Unverified |
- **Install:** `pipx install ekos-notifier` then `ekos-notifier install-service` (systemd user unit) and the wizard opens in the browser.
- **Footprint target:** < 100 MB RAM, < 2 % CPU on a Pi 4 during a session.
- **Quality:** simulation-replay test suite in CI (GitHub Actions), semver releases, CHANGELOG, MIT LICENSE, CONTRIBUTING and issue templates from day one.

## Roadmap

Four milestones to a public 1.0; the wizard and docs land before the GUI polish because first-run experience is the top priority.

| Milestone | Scope |
| --- | --- |
| 0.1 Core | Event sources (Ekos D-Bus, PHD2, INDI), canonical metrics with capability gating, `indi-dump`, SQLite store, session lifecycle, alert rules, Apprise notifier, hourly digest, simulation mode |
| 0.2 First run | Wizard, `doctor`, config validation, Quick start + Alerts reference docs, Olde English and Terse styles |
| 0.3 GUI + sync | Web GUI views, export folders, rclone sync and Flush, heartbeat |
| 1.0 Public | Full docs site, power box presets, pipx packaging, reference importer, multi-setup test matrix passing in CI, field-tested on StellarMate and Astroberry |

Open questions are tracked in [open-questions.md](open-questions.md).
