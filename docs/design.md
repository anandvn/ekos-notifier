# Ekos Notifier — Design Spec

Exported from the working design doc, Nov 2026. This file is the source of truth from here on;
update it alongside the code.

## Overview

Ekos Notifier is an open-source (MIT) companion service for KStars/Ekos rigs: it watches an imaging night, records it as a session in a local database, sends alerts that matter, and carries the night's data home. It runs on the imaging computer itself and depends on no specific server, notification service or network.

**Status:** this spec covers the whole product; what's built is marked **Implemented**. 1.0 is alerts, the hourly digest and the session record, field-tested on real rigs. The web GUI (and with it the GUI first-run wizard, user notes and Start/End buttons), the export folders, sync and the reference importer, and the mount-limit Emergency come **after 1.0**; see the [Roadmap](#roadmap).

**Goals**

- Alerts that are worth acting on: equipment risk and lost data, not noise.
- A complete, reproducible record of every night: plan, setup quality, conditions, every frame and event.
- Works fully offline at a dark site; internet is a bonus, never a requirement.
- Gets the data home with one button (or automatically) to whatever storage the user owns (after 1.0).
- Docs and first-run experience good enough that a stranger is set up in 15 minutes.

**Non-goals (v1)**

- Controlling equipment. Ekos Notifier observes and reports; Ekos stays in charge.
- N.I.N.A./Windows support.
- Image processing or stacking.

## Workflow and session lifecycle

A session starts automatically when the Ekos Scheduler starts (or the first Light frame of a manual sequence; a frame saved to a temporary folder is a preview and doesn't count, see below), with Start/End buttons in the GUI as override. One session = one night; each Scheduler target is a target segment inside it.

### States

1. **Standby** — service started at boot by systemd. Nothing is sent except the critical-battery Emergency.
2. **Setup** — entered when Ekos connects the equipment profile. A setup log records in the background while the user focuses, polar aligns and calibrates. The GUI shows it as a live checklist.
3. **Active** — entered on Scheduler start (or first manual Light frame, or GUI Start). Snapshot, pre-flight checks, then alerting and the hourly digest begin.
4. **Closed** — when the imaging run ends, on GUI End, or on Ekos disconnect. Summary sent, digest stopped, session exported for sync (after 1.0). If Ekos went away while the mount was unparked, the "Ekos stopped with the mount unparked" Emergency is sent too, whatever the state.
   - *The imaging run ends* when the Scheduler stops after running (finished or aborted), or, with no Scheduler, when Capture reports its sequence complete (with the Scheduler, Capture reports that once per job, so it's ignored). Parked or not: the summary says the mount's last known state, and the Emergency covers an unparked mount. The summary arrives within seconds of the last frame. A later run the same night starts a new session.
   - Snapshot readings (taken when Ekos Notifier connects) never close a session: after a restart mid-run, the Scheduler's snapshot has read `idle` while it was running.
   - *Ekos disconnect* means Ekos stays gone for a grace period (default 10 min), so a KStars crash followed by a restart doesn't split the night; the session's end time is when Ekos went away. The Emergency has its own 2-minute timer and isn't delayed by this.
   - *Stopped or lost:* an orderly stop (Ekos logs "Disconnecting INDI devices…", or the Scheduler logs it is stopping Ekos, within a minute before INDI/Ekos go idle; seen live for the user's Stop) closes as `ekos_stopped` and **sends no summary**: the user is at the rig. Anything else (KStars vanishing, an error, idle with no shutdown logged) is a crash: `ekos_lost`, summary sent. The "mount unparked" Emergency is sent either way. After a service restart, the stop's time and kind come from the session's recorded events, and the summary counts the session's frames from the store.
   - A session still open at local noon after its night closes as `night_ended`, at its last activity.
   - A session that closes from Setup without ever imaging is recorded as `abandoned`.
   - A session can start straight from Standby (Ekos Notifier started mid-night, or no Ekos connect seen). After a service restart, the open session is picked up from the store.

The rules are implemented in `ekos_notifier.session.SessionManager`, which is driven only by events and the clock, so replayed nights test the same decisions as live ones.

**Previews:** a frame Capture reports with its file in a temporary folder (`/tmp/`, `/var/tmp/`) is a preview, recorded as `capture.preview` instead of `capture.frame`, so no rule counts it: it starts no session, isn't a light in the summary, digest or flats reminder, and doesn't move where Disk low checks. Seen live (StellarMate OS 2.4.0, 2026-11-20, issues #52 and #53): focusing and polar aligning from the StellarMate app run a one-frame Capture "sequence" whose light frame goes to `/tmp/mcp_cache_<camera>.fits`; it started a session, the sequence completing closed it a second later (summary, Flats reminder), and Disk low checked `/tmp`, an 8 GB scratch area. The switch is made in the pipeline (`pipeline.classify`), not the D-Bus decoder, so nights stored before it also replay right with `simulate --session`.

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

Camera at setpoint · guiding settled · battery and disk above threshold · dew margin · system clock plausible (NTP/GPS synced) · notifier reachable (skipped silently when offline; see Offline behavior). Failed checks become warnings in the Starting session message; they never block the session.

- **Implemented so far:** battery against the low and critical levels (on `mains`, just the voltage), free disk space where frames are saved against `alerts.disk_low_gb`, dew margin and humidity against the dew-risk limits, and the imaging camera against its setpoint. Each check comes from the rule that alerts on it, from what the rig has reported so far, and is left out when the rig doesn't report it. Failed checks lead, as "Check: battery 11.84 V, under 12 V low; …", then the readings that passed: "Pre-flight: battery 13.1 V, dew margin 6.2 °C, RH 64%, 212 GB free." A camera still cooling is a reading, not a warning: Ekos waits for the setpoint, and "Cooler can't reach setpoint" covers a cooler that never gets there.
- **Not yet:** guiding settled (at a Scheduler start guiding hasn't begun yet, so it needs a later check), the system clock, and the notifier.

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

**Choosing what's sent:** every alert below has a name and a default, and the user can set each one under `messages:` in the config: `off` (never sent or listed in the digest, still recorded), `digest` (listed in the next hourly digest only; a problem still counts in its "Attention" verdict), `quiet` (sent at once, silently), or `normal`, `high`, `emergency`. An all-clear the user hasn't set follows its problem when that's `off` or `digest`. The hourly digest is set the same way (`digest: off | quiet | normal…`; its "Attention" form is never below Normal). Settings apply as alerts are raised (`ekos_notifier.messages`), so the digest, notifier, GUI and record agree, and an alert keeps its `default_priority`. `ekos-notifier config alerts` lists every name, default and setting; an unknown name is a config error with the closest match suggested; `config check` notes a lowered Emergency. A test checks the registry against every alert the code raises. The GUI's Settings view will edit the same section.

### Emergency

| Event | Default trigger |
| --- | --- |
| Mount nearing a limit with nothing capturing | **After 1.0.** Tracking within 15 min of the binding limit after the sequence ends. The limit is the soonest of Ekos's own (`hourAngleLimit`, `altitudeLimits`, when enabled), the mount's from a per-driver INDI preset (ZWO AM5 `MERIDIAN_LIMIT`/`POST_MERIDIAN_TRACK`, Losmandy Gemini `SAFETY_LIMITS`; element names need `indi-dump`s from the mounts), a config fallback (`mount.limit_past_meridian_degrees`), and the horizon. High when the mount enforces the limit itself (it stops tracking), Emergency when only Ekos or the config knows it. With no limit known, only the horizon is watched and `config check` says so. |
| Meridian flip failed | Ekos logs a failed flip ("Meridian flip failed, pier side not changed", "No successful Meridian Flip done, delay too long", "Post meridian flip calibration error. Aborting..."; "…Restarting..." is a retry and doesn't count, seen in a recorded night). These come from Ekos's Mount module, which has no `newLog` signal: its `logText` property is polled every 10 s. **Meridian flip overdue:** Ekos is set to flip, and the mount is still tracking the target 15 min plus one exposure past the flip hour angle on the pier side it settled on before the flip point (taken from the mount, since which side is "before" depends on hemisphere and driver); quotes Ekos's last "Meridian flip inactive (…)" reason. Once until "Meridian flip done" (Normal, immediate): a completed flip in Ekos's log, or the pier side changing. Checked against the live simulator flip (07:07, pier side changed 07:08): no false alert. |
| Battery critical | `power.voltage` at or under the battery preset's critical level (AGM 12 V: 11.5 V; LiFePO4 12 V: 12.0 V) for 1 min (a slew's dip doesn't count). Sent at any time, session or not. Once until the voltage is back 0.3 V over the low level, which sends "Battery OK" (Normal, immediate). |
| Ekos stopped with the mount unparked | Ekos stops or disconnects INDI (Scheduler finish/abort, crash, or user) while the last known mount state was unparked, and Ekos hasn't reconnected within 2 min. Uses the last known `parkStatus`, since the mount's properties vanish with Ekos. Clears when the mount is seen parked again: "Mount parked" (Normal, sent at once, since there's usually no session left to carry it in a digest). |

### High

| Event | Default trigger |
| --- | --- |
| Guiding lost | Guiding stopped (Ekos Guide aborted, reacquiring, calibration or dither error, or PHD2 lost the star) and not guiding again within 5 min of time when guiding was expected; looping or recalibrating doesn't count as recovered. The clock pauses, and restarts from zero, while the mount slews or parks, Capture flips, solves or focuses, Focus or Align is busy, calibration frames are being taken, or capture had already stopped before guiding did. Capture *suspended*, or *aborted* by Ekos because guiding stopped, doesn't pause it; nor does a mount that stopped tracking (the message says so). Message names PHD2's lost-star reason and alert if any. Followed by "Guiding recovered" (Normal, immediate). |
| PHD2 warning | PHD2 raises a warning or error during an active session (e.g. "not able to make sufficient corrections in RA", "INDI camera disconnected"). Once per message per session, with Ekos's current guide RMS. PHD2's advice (the dark-library tip, which it also tags as a warning), info messages, and warnings caused by Ekos shutting down aren't sent: a warning is held 10 s and dropped if Ekos logs "Disconnecting INDI devices", INDI or Ekos goes idle, or capture completed (the Scheduler's end), since PHD2 warns about the camera and mount just before INDI reports idle (seen live). |
| Guiding poor | Follows Ekos's own guide-deviation limit (per sequence job; default `[Capture] GuideDeviation` in `kstarsrc`, 2″). When the job enforces it, Ekos suspends an exposure when deviation goes over the limit ("Guiding deviation 3.791 exceeded limit value of 0.5 arcsecs…"), resumes once under a separate, looser resume limit (2″ by default), and can trip again seconds later, so no exposure ever finishes (seen live): alert when Ekos has been suspending exposures and no frame has finished for `alerts.guiding_poor_minutes` (default 10); the all-clear comes when a frame finishes. When the job doesn't enforce it, Ekos keeps exposing: alert when Ekos's total guide RMS (√(RA² + DEC²), arcsec) has stayed over that limit for the same time while guiding. Dithering doesn't restart the window; guiding stopping does. Followed by "Guiding back within limits" (Normal). `guiding_poor_minutes: 0` turns it off. With KStars in another language, Ekos's lines are read back into English first (see "KStars in another language"). |
| Capture aborted | A running capture aborted and not restarted within 60 s (Ekos also reports "aborted" for an idle Capture when a focus exposure fails; that isn't counted), when Ekos gave a reason (its log just before, e.g. "Autoguiding stopped. Aborting...", "Autofocus failed. Aborting exposure", or guiding, focus or a device failing in the seconds before). An abort with no cause is taken as the user's own Stop and only recorded: a Stop logs only "Camera capture stopped" (seen live). |
| Scheduler aborted | The Scheduler goes `aborted` after running (Stop leaves it `idle`); quotes its last log line. |
| Capture stalled | Capture busy (exposing, downloading, changing filter, starting a job, dithering) with no progress for 2 × (exposure + download), at least 2 min. Exposure from Ekos's "Capturing 60.000-second…" log, download time from its "Download Time" log. Waiting states (guider drift, suspended, temperature, focus, flip) and Ekos going away don't count. Followed by "Capture running again" (Normal) at the next frame. |
| Autofocus failed | 2 consecutive failed runs. On KStars 3.8.5 a failed run ends `aborted`, not `failed`, with "Autofocus failed, moving back to initial focus position…" in its log (seen live); a run counts as failed if it ends `failed`, or `aborted` with that line. Ekos's own retries ("Restarting autofocus process…") are part of the run, so a failed ending is counted 10 s later unless a restart follows; a run whose retry completes is a success and resets the count. An `aborted` with no failure logged is the user's Abort. |
| Plate solve failed | During a session, an Align run fails (ends `failed`, or `aborted` with a solver failure logged during it; a plain `aborted` is the user's Abort) and no solve succeeds within 3 min (Ekos retries with looser settings, the Scheduler retries the alignment stage). Says what came just before (the meridian flip, or the Scheduler's new target, within 15 min), how many runs failed, and Ekos's last reason ("Solver timed out after 60 seconds."). Once per streak; "Plate solve succeeded" (Normal, immediate) after. The failure strings come from KStars 3.8.5's binary; a failed solve hasn't been recorded live yet. |
| Dew risk | Ambient − dew point under `alerts.dew_margin_c` (2 °C), or RH > 90 %, for 5 min during a session; with several sensors, the smallest margin and highest RH count. Names dew heaters (standard power interface outputs) that are off or at 0 %. Once; "Dew risk passed" (Normal, digest) after 10 min with the margin 1 °C over the limit and RH under 85 %, which re-arms it. `dew_margin_c: 0` turns it off. |
| Battery low | At or under the low level (AGM 12 V: 12.0 V; LiFePO4 12 V: 12.8 V, since LiFePO4 holds ~13 V until nearly empty) for 1 min, at any time, session or not (seen live: a LiFePO4 at 12.0 V raised nothing outside a session; a battery draining during setup or a cloud wait matters too). Once, re-armed like Battery critical; not sent as well when Battery critical already has been. Says the current drawn. Runtime estimate: not yet. |
| Power port fault | libindi's standard power interface reports no faults, so: **Power output off**, a DC output that was on and drawing current (≥ 0.1 A) switches off during a session; **No current on** an output, one that stays on but drops under 0.02 A for 30 s (boxes with per-output current). Held 10 s and dropped if Ekos shuts down meanwhile, and not raised for 60 s after a shutdown starts (a box set to power off on disconnect switches its outputs off as Ekos disconnects). Once per output per session; "Power output back" (Normal, immediate) when it's on and drawing again. Dew outputs are left to Dew risk. |
| Cooler can't hold | The imaging camera reached its setpoint, then stays more than `alerts.cooler_tolerance_c` (2 °C) above it for 10 min. **Cooler can't reach setpoint:** still that far above it 20 min after it was set. INDI never sends the setpoint to other clients, so it comes from Ekos Capture's log ("Setting temperature to -10.00 °C...", when a job enforces a temperature) for Capture's camera; without one, from the temperature when the driver reports reaching its target (`CCD_TEMPERATURE` Busy → Ok, seen on the CCD simulator); Busy again then means a new target, Idle that regulation is off. Once per camera per session; "Cooler at setpoint" (Normal, immediate) when it's back. `cooler_tolerance_c: 0` turns it off. |
| Clouds | Dimming, not a lost star: a star lost at full brightness is the mount or guiding (seen live: a runaway walked the star off the frame at full SNR), which Guiding lost covers. Two signs during a session: PHD2's guide-star SNR (median of the last minute) falls by `alerts.clouds_drop_percent` (50 %) from its baseline (the last 15 min of clear guiding) for 2 min, a star loss or guiding stopping after it faded keeping the sign up until a new star guides well; or two light frames in a row whose star count (Ekos's `captureComplete`, when it measures stars) falls that far from the median of the previous frames with the same filter and exposure on the same target. The message gives every sign it has: guide SNR then and now, PHD2's star losses, the frame's stars and HFR against typical. Once; "Clouds passed" (Normal, immediate) when the guide star is back over 80 % of its baseline for 5 min and the latest measured frame's stars are back too. The thresholds are first guesses: the simulators don't cloud over. `clouds_drop_percent: 0` turns it off. |
| Disk low | Under `alerts.disk_low_gb` (10 GB) free where Ekos saves frames (the last frame's folder, else home), checked each minute and after each frame during a session. Says how many more frames fit at the recent frames' size and how long that is at the session's pace. Once until 1 GB over the limit again ("Disk space OK", Normal, immediate). `disk_low_gb: 0` turns it off. |
| Weather alert | **Implemented** (`ekos_notifier.weather`, issue #54). INDI's standard weather interface gives every weather driver lights for its critical readings (`WEATHER_STATUS`: Ok, Busy for near its limit, Alert past it, against limits set in the driver), the signal Ekos's Scheduler can park the mount on; the decoder sends them as `weather.status`. Alert when a reading goes to Alert, at any time, session or not (like the battery): it names each reading past its limit with its value, says when the source is regional, and that Ekos parks if it's set to. "Weather OK" (Normal, immediate) once nothing has been in Alert for 10 min, so a reading flickering at its limit sends one alert. Per reading, a weather station at the rig wins over a secondary source: OpenWeatherMap's light counts only for a reading no direct device watches. Seen in a real dump (#54): a Pegasus PPBM's weather status watches only temperature, so OpenWeatherMap's rain, snow, wind and forecast still count beside it. Readings match by INDI name (`WEATHER_RAIN_HOUR`); drivers that name one differently both count, rather than risk missing rain. The pre-flight check reports the status. Drivers update their lights before their readings in each cycle (seen on the simulator: the first alert said last cycle's 0 mm of rain), so the alert waits 5 s for the same cycle's values, and the decoder sends a new status when the readings behind a light out of Ok change; a blip shorter than that sends nothing. Verified live on the INDI weather simulator in rain (fixture `sim-weather-rain-libindi-2.2.5.ndjson.gz`: Weather alert "Precip (mm) 5", Weather OK 10 min after); built from OpenWeatherMap's and a PPBM's real property definitions; not yet seen on a real rig. |

### Normal — manual steps

| Event | Default trigger | Timing |
| --- | --- | --- |
| Target low | 30 min (`alerts.target_low_warning_minutes`) before the target the mount is tracking sinks below Ekos's altitude limit: the mount limit if enabled (`[Mount] MinimumAltLimit`), else the Scheduler's constraint (`[Scheduler] SchedulerAltitudeValue`), else 10°. Once per target | Immediate |
| Twilight coming | 45 min (`alerts.twilight_warning_minutes`) before astronomical dawn (Sun rising through −18°), once per session | Immediate |
| Sequence complete | Not an alert of its own: the run ending closes the session, which sends the summary (saying whether the mount is parked), and "Mount left unparked" covers a mount left tracking once Ekos stops | — |
| Flats reminder | A session that imaged ends: the filters with lights tonight but no flats tonight (flats from the sequence or an earlier session of the night count; each filter once per night; not when you stopped Ekos). The sample night has one: Luminance lights, flats for the other six filters only | With the summary |
| Darks reminder (off by default: `messages: darks_reminder: normal`) | A dark job starts, known before its first frame: as Capture starts, the Ekos adapter reads the active job's frame type (`getActiveJobID`, `getJobFrameType`, read-only; checked live on 3.8.5) as `capture.job`. "Cover the telescope". Once per night, session or not | Immediate |

### Normal — milestones

"Starting session" and the session summary are sent at once rather than waiting for the digest.

Starting session (with pre-flight results) · first sub saved (thumbnail) · meridian flip complete · target switched (Scheduler) · session summary at close (totals per filter, integration, median HFR, guide RMS, aborts, min battery, temperature/humidity range, sessions awaiting sync).

The summary so far: lights per filter and integration, calibration frames, median HFR (when Ekos measures it) and median guide RMS, the ambient temperature, humidity and imaging camera temperature ranges, the lowest battery voltage, the problems raised (each High or Emergency alert by title, with a count when repeated, which covers aborts), and the mount's last state. After a service restart the HFR and guide RMS come from the stored frames; the ranges and problems start again from the restart. Sessions awaiting sync wait for sync.

### Quiet

Meridian flip in 15 min: only when Ekos is set to flip (`[Mount] ExecuteMeridianFlip`), at its flip hour angle (`MeridianFlipOffsetDegrees`); held for the digest's "Since last".

**Implemented** (`ekos_notifier.ephemeris`, `ekos_notifier.sky`): pure-Python sidereal time and Sun position (Meeus, low precision; checked against his worked examples, good to about a minute). The site is KStars's `location()` over D-Bus (JSON: latitude, longitude, elevation, time zone), falling back to the mount's INDI `GEOGRAPHIC_COORD`; the target is the mount's INDI `EQUATORIAL_EOD_COORD` (sent on a move of more than ~3′). Ekos's Mount D-Bus properties (`equatorialCoords`, `hourAngle`) would do too, but INDI's are already in the recordings. A target is where the mount settles and tracks: positions passed through while slewing (a meridian flip goes through the pole) don't count. Live test: the flip notice said 07:07 and Ekos flipped at 07:07:53. The digest adds "Flip in 1 h 40 min · M 31 below 10° in 3 h 54 min · Dawn in 6 h 05 min".

### Smart-alert rules

- **Intent-aware:** guiding stops caused by Ekos itself (flip, autofocus, dither) are never failures.
- **Debounce:** a condition must persist past its window before alerting; a lost star that recovers in seconds is silent.
- **Collapse and clear:** a repeating problem sends one alert, then one "recovered after N min".
- **Correlate:** concurrent star-lost, HFR jump and star-count drop become one Clouds alert.
- **Thumbnails:** a 100–200 KB stretched JPEG attaches to first-sub and summary messages where the notifier supports images.

## Hourly digest and message wording

The digest is one status message per interval (default 60 min) while a session is Active, folding in any held Normal events. Off in the GUI = Normal events send individually.

### Digest

- **Title carries the verdict:** "23:00 — All's well" or "23:00 — Attention: dew margin 1.4 °C". With no problem and no light frame in the interval, "All's well" would mislead (seen live: a session left open after capture stopped said it every hour), so it says "23:00 — No light frames since 21:40" (or "yet"), still Quiet: the Scheduler waiting for a target is no problem.
- **Priority:** Quiet when all's well; Normal when it says Attention.
- **Timing:** on the clock hour, or every N minutes from session start (configurable). A digest due less than 10 minutes after the session started is skipped.
- **Verdict:** Attention when a High or Emergency alert was raised in the interval and not cleared in it, or a problem raised earlier is still open: one with an all-clear alert until it's sent (guiding lost, guiding poor, capture stalled, mount left unparked), and capture aborted, Scheduler aborted and autofocus failed until capture, the Scheduler or focus is working again (seen live: without this, "All's well" with nothing captured since an abort). The problems are named in the title and carry over as "Still open", including across a service restart (rebuilt from the session's alerts, then closed by Ekos's snapshot if no longer true).
- **Implemented** (`ekos_notifier.digest`): target (from the Scheduler's job), filter and job progress (from Capture's "has N/M frames captured"), lights and integration this interval and tonight, calibration frames, median guide RMS, failed dithers, median HFR when Ekos measures it, camera temperature, ambient, RH, dew margin and power (from INDI), time to the flip, the target setting and dawn (from the site), and "Since last": every alert (sent or held) and milestones such as a completed meridian flip. Not yet: the battery trend and thumbnails.
- **Body:** target · filter · frames done/planned (+ this interval) · median HFR · guide RMS · camera temp · battery and trend · ambient, RH, dew margin · time to flip and dawn · events since last digest · thumbnail.

```
IC 1396 · Ha · 42/60 (+11 this hr)
HFR 2.1 (med) · RMS 0.62" · 1 dither fail
Cam −10.0 °C · Batt 12.7 V (−0.2/hr)
Amb 14.2 °C · RH 78% · Dew Δ 4.1 °C
Flip in 1h40 · Dawn in 6h05
Since last: flip complete 22:31
```

### Message wording

Messages are in plain English. The Olde English style was dropped (too gimmicky to maintain), and a Terse style is deferred. Varying the wording is planned as translation instead.

**Translation** (0.3) uses gettext, the system KStars uses and translators know, so the GUI's strings (after 1.0) are translatable from the start.

**Implemented** (`ekos_notifier.i18n`) for everything Ekos Notifier says: on the phone, alert titles and bodies, the hourly digest, the session summary and its statistics, the pre-flight checks, the test message and `simulate --send`'s "[Simulated]"; in the terminal, `--help`, `doctor` (its checks, fixes, status labels and headings), `setup` (questions, choices, summary), `config check` and `config alerts`, config errors and warnings, `simulate`'s report, `indi-dump`, `install-service` and the old-folders notice. The CLI picks the language from the config before building `--help` (the system's when there's no config). Log lines stay English (they're for debugging), as do commands in fixes (they're placeholders, so a translator can't break them) and the `[Y/n]` answers. 676 messages.

- **Marked strings:** `_("Ekos solved the frame at {time}.").format(time=…)`, `ngettext(…)` where a count varies, and `N_(…)` for table entries translated where they're used (the start and end reasons, the fallback causes). Values are named placeholders, so a translation can reorder them; numbers are formatted before they go in (`f"{volts:.2f}"`), so a translator can't break a format. Sentences built from fragments ("…the frame{when} ({runs} failed…)") were split into whole sentences, one per case. Ekos's state names in messages ("still in progress", "mount was unparked") come from one table (`i18n.WORDS`). Quoted Ekos lines, PHD2's messages, target, filter and device names are data and go in as they are.
- **Template and catalogs:** `tools/update_pot.py` collects the marked strings (with `ast`, no gettext tools needed) into `src/ekos_notifier/locale/ekos-notifier.pot`, with file references but no line numbers, so it changes only when a message does. A translation is `src/ekos_notifier/locale/<language>.po` (`de.po`, `pt_BR.po`), made with `msginit`, Poedit or Weblate. `.po` files ship as text and are read at startup by a small parser (contexts, plurals, continued lines; fuzzy and empty entries are left out, so English is used for them) and handed to Python's `gettext`: no compiled `.mo`, no build step. Checked against GNU `msginit`/`msgfmt --check`.
- **Setting:** `language:` (`auto`, the default: the service's own locale, `LANGUAGE`/`LC_ALL`/`LC_MESSAGES`/`LANG`, as gettext reads it; or a code such as `de`, `pt_BR`, falling back from region to language and then English). Applied whenever the rules are built: at startup, on a config reload, and in `simulate`. A language with no catalog gets a config note. Messages are translated when they're made, so the session record keeps exactly what was sent, in the language it went out in; rules, settings and the digest go by each alert's `rule` key, never its wording.
- **Tests:** a made-up language (every message in ⟪…⟫) replays the sample night and the German fixture, and fails on any title or line it can't translate, and runs `config alerts`, `config check`, `simulate`, `doctor` and the setup wizard in it, failing on any line that isn't translated (lines that are only data, the digest's target and filter and PHD2's own messages, are allowed); the template must be current; every translation's placeholders must match the English (`update_pot.py --check` says which); and a check that no function assigns `_` (`_, x = …`), which would break `_()` there at runtime.
- **No English line on Emergencies.** The old styles rule ("an Emergency ends with a plain-English line, whatever the style") guarded against a joke style hiding an urgent message; it doesn't carry over to a language the user chose. A second, English sentence is noise to someone who reads German at 3 a.m., and lock screens and watches show the title and first lines, so a trailing line is often never seen. Support doesn't need it either: every alert keeps its `rule` key (`battery_critical`) whatever its wording. The risk it covered, a wrong or stale translation of an urgent message, is handled in review instead: the four Emergencies (Mount left unparked, Battery critical, Meridian flip failed, Meridian flip overdue) are checked by a second speaker before a translation is merged, and an untranslated or fuzzy entry already falls back to English.
- **Numbers in messages (decided, to build with the first comma-language translation):** each translation chooses its decimal separator. Numbers we *read* are already right whatever the locale (PHD2 and INDI always write `.`; KStars's log is read in its own locale, see "KStars in another language"); this is about numbers we *write*, which come out of Python as `12.4` and look foreign to a German reader expecting `12,4`. A translation gets one entry, the decimal separator (a gettext context, `msgctxt "decimal separator"`, so it can't clash with another message), set to `,` or left as `.`; one helper formats every number in a message with it, replacing the `f"{x:.1f}"` values passed to `.format()`. Messages never group thousands, so the separator is the only thing to localize. Not the `locale` module: `locale.format_string` needs the locale generated on the host (often missing for a language that isn't the system's), maps a region rather than our language setting (`de_CH` uses `.`), and changes process-wide state in a service that runs all night. Not Babel: correct out of the box, but a dependency of about 30 MB whose locale data changes output between releases, for one character. Until a translation sets it, numbers stay `12.4`, which is still clear.
- **Dates and times** need no localizing: 24-hour times (`22:15`) and ISO dates (`2026-11-19`), as messages and `simulate --list` already use. Where a sentence holds a date or time, it's a placeholder in a translatable string, so a translation can place it. The GUI (after 1.0) follows the same rule.

## Offline behavior

No internet never stops or degrades recording; it only silences outbound messages.

- **Offline at session start:** the notifier is skipped without warning. GUI shows "Offline — recording only".
- **Link drops mid-session:** recording continues. An alert that can't be delivered is retried for a short window (default 10 min, `notify_expire_minutes`), then dropped rather than sent late: alerts arriving after the fact can't be acted on, and a backlog of them on reaching home is noise. This includes the session summary. Dropped alerts stay in the local record (GUI, export); the outbox marks them `dropped` with the reason.
- **No heartbeat (dropped 2026-11-20).** A dead-man's switch (the rig pings an outside service, which alerts when the pings stop) was planned, to cover what the rig can't report itself: a crash, a power cut, a dead uplink. Dropped before it was built: pinged only during sessions, every normal end of a night looks like silence, so the service alerts each morning unless the check is paused through a service-specific API (Healthchecks' needs an API key in the config); at a dark site with patchy internet it fires whenever the link drops, which is most of what it would say; and the commonest way a rig dies there, the battery, is already an alert before it does (Battery low, then Battery critical). A rig that runs 24/7 (an observatory) is better served by pinging all the time, which needs nothing from Ekos Notifier: the README has a cron recipe.
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
`PRAGMA user_version` migrations. Implemented: `events`, `frames`, `samples`, `sessions`, and the
notifier's `outbox` and `consumers` (its read position in `events`). The others (`targets`,
`setup_items`, `notes`, `calibration_runs`) and the summary and sync columns arrive with the
features that fill them.

### Export folder (written at session close; after 1.0)

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

**After 1.0.** Every night is already recorded in the local database, so an exporter added later can export and sync past sessions too.

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

**After 1.0.** The terminal covers what the GUI was first needed for: `setup` (first run), `doctor` (what's wrong), `simulate` (preview), and the digest and summary on the phone.

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
| Settings | Notifier test, thresholds, digest interval, sync destination, units |

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
| Power and weather | INDI properties with presets: libindi's standard power interface (Pegasus, SVBONY, Terrans… whatever the brand), weather stations, camera and focuser temperatures; custom property mapping for anything else | `indi: metrics: power.voltage: "Box / PROPERTY.ELEMENT"` |
| Notifications | Apprise URLs (Pushover, ntfy, Telegram, Discord, Slack, Gotify, email, Home Assistant, 100+) | `notify: ["pover://user@token"]` |
| Sync | rclone remote or local path | `sync: dest: "nas:astro/sessions"` |

- **Battery presets:** LiFePO4 12 V, AGM 12 V, AC mains (voltage alerts off), custom thresholds.
- **Units:** °C/°F, guide error in arcseconds or pixels, 12/24 h time.
- **Adding an adapter:** documented interface + example in `contrib/`; adapters register through Python entry points.

## Device support and graceful degradation

No device is assumed: every rule declares the metrics it needs, and a rule whose inputs no device provides is switched off and shown as "not available on this rig", never an error. Guiding, capture, focus and mount alerts need only Ekos and PHD2, so every user gets them.

### Canonical metrics

Adapters translate device properties into canonical names; rules never see device names.

| Metric | Used by | Fallback |
| --- | --- | --- |
| `power.voltage`, `power.current`, `power.watts` | Battery low/critical, digest | None: battery alerts off |
| `power.outputs` (`power.output` events) | Power output off / no current | None |
| `env.temperature`, `env.humidity` | Dew risk, digest | Any INDI weather device or USB temp/RH sensor |
| `env.dewpoint` | Dew risk | Derived from temperature + humidity (Magnus formula) |
| `power.dew_heater` | Dew heater check | Check skipped |

When several devices report the same metric, the sensors at the rig win. **Implemented** (`ekos_notifier.sources`): a *secondary* source doesn't measure at the telescope: OpenWeatherMap's driver reports the region's weather from the internet, and the meta-weather driver re-reports other weather drivers (`SECONDARY_DRIVERS`). Its readings count only when no direct sensor reports that metric. The pipeline marks them (`secondary: true`, from each device's driver in its `indi.device` events, so nights stored before this replay right too), and every rule keeps each device's readings and picks with `prefer_direct`: Dew risk and its pre-flight check (with several direct sensors, the smallest margin and highest humidity), the cooler's ambient, the digest and the session summary's ranges. Seen live (2026-11-20): with a Pegasus PPBM at 68–88 % humidity, OpenWeatherMap's 94 % raised Dew risk and a pre-flight warning on its own, and the digest showed OpenWeatherMap's numbers because it sorted first. No setting yet: the rule is automatic until someone needs to choose.

**Placeholders aren't readings.** Drivers publish 0 before their first measurement: the PPBM for an instant at connect, OpenWeatherMap for as long as its fetch fails (13 minutes that night). The INDI decoder doesn't count 0 % humidity (never real), nor a dew point beside it, nor a temperature or dew point of exactly 0.0 that comes with 0 % humidity or before the device has reported another value; a real 0 °C after that counts.

### Device detection (three tiers)

1. **Preset:** matched by INDI driver name; mapping verified.
2. **Heuristic:** numeric properties whose elements look like VOLTAGE, TEMPERATURE, HUMIDITY, DEW_POINT; the wizard asks the user to confirm each match.
3. **Manual:** property mapping in config.

### Implemented so far (`ekos_notifier.indi`)

- **Client:** read-only. It sends `getProperties` only (no `new*`, BLOBs never enabled), to the INDI server of the active Ekos profile (`userdb.sqlite` `profile` `host`/`port`; empty means local mode on localhost:7624; `indi.host`/`indi.port` in config override it). It retries quietly while the server is down (it only runs while Ekos is connected).
- **Metrics:** presets match devices by INDI interface bits (`DRIVER_INTERFACE`: 2 camera, 8 focuser, 128 weather, …, checked against the simulators) or driver executable, and map `PROPERTY.ELEMENT` to canonical metrics. Built-in and verified on the simulators: weather stations (`WEATHER_PARAMETERS` temperature, humidity, dew point), camera temperature (`CCD_TEMPERATURE`) and focuser temperature. Built-in from real devices: the SVBONY SV241 / SV241 Pro's temperature and humidity sensor (`WEATHER_SV_SENSORS`, matched by driver since the box reports no weather interface; its power comes through the standard power interface; from an SV241 Pro's `indi-dump`, issue #47, kept as a fixture). Anything else is mapped in config, `metric: "Device / PROPERTY.ELEMENT"`. The dew point is derived (Magnus) for a device with temperature and humidity but none of its own.
- **Events:** `metric` (`metric`, `value`, `device`, `via`) when a value moves past a small deadband or at least once a minute; `indi.device` on connect/disconnect; `indi.capabilities` (metric → devices) whenever the set changes, which it does as Ekos connects and disconnects equipment. Samples are stored with their source device (schema 4), and each frame gets the imaging camera's temperature (Ekos Capture's `camera`, or the name in its log lines).
- **Power boxes:** a built-in preset for libindi 2.x's standard power interface (`DRIVER_INTERFACE` bit 18), whatever the brand: `POWER_SENSORS` voltage, current and power as metrics, and each output from `POWER_CHANNELS` (on/off), `POWER_CURRENTS` and `POWER_LABELS` (the box's own names: "Camera", "Mount"), and the `DEW_*` equivalents with `DEW_DUTY_CYCLES`, as `power.output` events. Verified with a test driver built on libindi 2.2.5's `INDI::PowerInterface` (`tools/indi_power_simulator`; its recorded stream is a test fixture); 15 installed drivers use the interface. Older drivers, with their own property names, are mapped in config. Battery presets: `agm_12v` (default), `lifepo4_12v`, `mains` (voltage alerts off), `custom`, with `power.low_volts`/`power.critical_volts` overriding.
- **Camera cooler:** `camera.cooler` events (`busy`, `ok`, `alert`, `idle`) when a camera's `CCD_TEMPERATURE` changes state, for the cooler alerts. Recorded from the CCD and weather simulators (cooling to −10 °C; humidity 75 → 92 → 60 %) as test fixtures.
- **Digest:** camera temperature, ambient, RH, dew margin, and power ("Power 12.4 V (low 12.1 V), 4.2 A").
- **`ekos-notifier indi-dump`:** every device's properties (or the named ones, `--device`) to a file, with the site location, the user name and credentials removed (any text property or element named like a key, token, password, login, user or email: OpenWeatherMap's API key reached a public issue before this, #54), with what's recognised and the readings that could be mapped. One device is a few KB, small enough to paste into an issue; a whole rig is 100 KB or more (115 KB in the first real one, issue #47), over GitHub's 65,536-character limit, so the command says to use `--device` when the file is too long to paste. Found in a dry run (2026-11-20): the form's dump field rendered as code, which takes no attachments, and a whole-rig dump couldn't be pasted; the form now has a plain field that takes a paste or an attached file. The candidate filter also missed the SV241's `SHT40_HUMI` (it looked for `HUMID`).
- **Not yet:** presets for power boxes whose drivers predate it, heuristic detection with confirmation (needs the wizard), stale-metric notices, and a battery runtime estimate.

### Presets as data

- Presets are YAML files in `src/ekos_notifier/indi/presets/`, contributed by pull request with no Python required.
- `ekos-notifier indi-dump --device "…"` writes a device's full property list to paste into a "Device support" issue (the form asks for the device, its driver and the file, pasted between code fences or attached).
- Each preset is labelled verified, community or untested; the wizard shows the label.

### Isolation and stale data

- Each adapter runs as its own supervised task; a crash or disconnect restarts it with backoff and never stops the service.
- A metric not updated within N minutes is stale; rules never fire on stale inputs. One "sensor lost" Normal notice is sent instead. Exception: "Ekos stopped with the mount unparked" deliberately fires on the last known park state, because losing that state is the event.

### KStars in another language

Most rules run on Ekos's status codes, which are the same in every language. Some also read Ekos's log lines: Autofocus failed (its retries), Plate solve failed, Meridian flip failed and done, Guiding poor when the job enforces the limit, the reason a capture aborted, and recognising your own Stop (which keeps outputs switching off and PHD2's complaints at shutdown from raising alerts). KStars translates those lines (KDE i18n; the Flatpak ships 151 languages). In German, for instance, "Autofocus failed." arrives as "Die automatische Fokussierung ist fehlgeschlagen.", and before 0.2 those alerts silently never fired.

**Implemented** (`ekos_notifier.ekos.translation`): the Ekos adapter finds the running KStars's language (its app-specific override, `klanguageoverridesrc`, else `LANGUAGE`/`LC_ALL`/`LC_MESSAGES`/`LANG` from its environment) and loads its own catalog (`kstars.mo`, looked up inside the process's root, so a Flatpak's `/app/share/locale` is found). The catalog's keys are KStars's English source strings, so each translation becomes a pattern (`%1`, `%2` … match anything, in any order) and a line matching one in full is passed on as its English source with the same values, keeping the original as `original`. The most specific match wins; translations with under four fixed characters are ignored as too vague; lines that match nothing (untranslated, or not from a translatable string) pass unchanged. No per-language work: any language KStars ships works. Loading the German catalog takes 0.16 s (8,433 phrases); a line takes about 0.4 ms. `doctor` reports the language, and warns when the catalog can't be read. Tested with a made-up language over the existing English scenarios (each alert is missed without the translation, and identical with it) and, where the KStars Flatpak is installed, against its real German catalog.

**Numbers** follow KStars's locale, not its language: German KStars writes "Capturing 10,000-second" (ten seconds; the line itself isn't translated) and "Fokus-Position 50.000". KStars writes numbers in its locale (Qt: `LC_ALL`, else `LC_NUMERIC`, else `LANG`; the language override changes words, not numbers), and nothing is guessed from how a number looks: `10,000` is ten in German and ten thousand in English. The adapter looks up that locale's decimal point and grouping mark in this computer's C library (`locale -k LC_NUMERIC`) and puts them on each Ekos log line it passes on (`numbers`, absent for English; stored with the event, so sessions from the database replay right too). Rules read each number the way KStars's source writes it: exposures (Capture's lines and the job plan: `%L`, three decimals), deviation limits and cooler setpoints with `localized` (in the line's format, strictly: in German `1.5` isn't a number); guiding deviations and download times with `plain` (KStars writes them the same in every locale: `0.820`). When this computer doesn't have KStars's locale (Qt carries its own locale data, so KStars can use one the system never generated), the localized numbers are left unread: rules fall back as when the line never came, the service logs a warning, and `doctor` says which locale to add (`/etc/locale.gen`, `sudo locale-gen`).

**PHD2** translates its own messages (wxWidgets, `phd2.mo`; its language is its own setting, and it keeps '.' in numbers whatever the locale). Its warnings are recognised by type, not wording, so they arrive in its language as they are. Only its advice ("For best results…", sent as a warning and not something to act on mid-night) is matched by wording: the translations of that advice in every PHD2 catalog installed here are recognised too, so its language needn't be found.

**Replays:** `tools/record_night.py` notes KStars's language and number format in the recording's meta record (`language`, `numbers`), and replay (`simulate`) reads Ekos's log through that language's catalog from the KStars installed on the replaying computer, saying so in its heading, or that the alerts reading Ekos's log may be missing when it has none. Sessions from the database are already stored in English.

**Verified live** (2026-11-19, KStars 3.8.5 Flatpak and PHD2 2.6.14 in German, simulators; fixture `sim-german-failures-kstars-3.8.5-flatpak.ndjson.gz`): the service of the time read the user's Disconnect as Ekos lost (a session summary and flats reminder followed), quoted no reason for the failed solve, and sent PHD2's dark-library tip as a High warning; through the translations the night raises what an English one would (Starting session, Plate solve failed with "Solver Failed.", Capture aborted with "Autoguiding stopped."). The autofocus failures came during setup, before the session, so they raise nothing in either language.

### Software versions

Ekos and PHD2 adapters detect the running version and report their actual capabilities. The supported KStars and PHD2 range is documented, and `doctor` warns outside it.

### Test matrix (CI)

Simulation replays run against: no power box · weather station only · temp/RH sensor only · each preset · device disconnecting mid-night · internal guider instead of PHD2 · Flatpak StellarMate (Arch). Community `indi-dump` files become fixtures, so a change that breaks someone's device fails the build.

## Configuration, first run and documentation

Top priority: a new user goes from install to a test alert on their phone in under 15 minutes, without reading source code.

### Configuration

- One YAML file (`~/.config/ekos-notifier/config.yaml`), fully commented, every key with a default.
- `ekos-notifier config check` validates it and explains errors in plain words; `ekos-notifier setup` edits the common settings line by line. The GUI Settings view (after 1.0) will edit the same file.

### First-run wizard

**A terminal wizard is implemented** (`ekos-notifier setup`, offered by `install.sh` when no notifier is set): it shows what doctor's D-Bus, INDI and PHD2 checks see (and the power box's voltage); sets up notifiers (Pushover, with its 30-character keys checked; ntfy, with a random topic suggested; Telegram; Discord; any Apprise URL), sending each a test and asking whether it arrived; the battery (a preset, mains, or custom levels, and whether to keep levels the file already overrides); and the digest (on, interval). It shows what will change and saves only on yes. The config is edited line by line, touching only the settings whose answers changed, so comments and everything else stay; the result is loaded back and compared with the answers before it's written (atomically, mode 600, the old file kept as `config.yaml.bak`), and a file it can't edit safely (e.g. `power: {…}` on one line) is left alone with the lines to add shown instead. It ends by offering the service if it isn't installed. A GUI wizard comes with the GUI, after 1.0; its plan:

1. **Welcome + password** for the GUI.
2. **Detect:** finds KStars/Ekos, PHD2, INDI devices; shows what it found in plain words ("PHD2 found — guiding alerts on").
3. **Notifications:** pick a service from a list, paste the key, press Send test. Skippable (record-only mode).
4. **Power:** pick power box preset and battery type, or "mains power".
5. **Sync (optional):** pick destination type, guided rclone setup, Test write.
6. **Digest:** on or off, and its interval.
7. **Done:** a checklist of what's on, and "Simulate a night" to see sample alerts.

### Built-in safety nets

- `ekos-notifier doctor`: checks D-Bus access, PHD2 port, INDI connection, clock sync, disk, notifier, sync destination; prints fixes. **Implemented** (read-only, a few seconds): the install (version, Python, a venv whose Python an OS update removed, the command on `PATH`); the config (errors, notes, notifier URLs with keys hidden, a config other users can read); the service (installed, running or failed, a unit out of date or naming a Python that's gone, another copy in a terminal); the database (sessions, a schema from a newer version); which session bus KStars is on and Ekos's status; KStars's language and number format (and whether this computer has them); a KStars Flatpak's `shared=network`, `sockets=session-bus` and home access (from `/proc/<pid>/root/.flatpak-info`); the INDI server, its devices and what's recognised (built-in presets plus the config's mappings); PHD2's version when Ekos guides with it; NTP sync (`timedatectl`); free space where Ekos saved the last frame. Each result is ok, note, warn or FAIL with a fix; the exit status is 1 on a FAIL. Not yet: the sync destination (no sync yet), the supported KStars/PHD2 version range, and Scheduler startup/shutdown steps naming devices missing from the profile.
- **Simulation mode:** replays a recorded night so users can tune alerts in daylight; also drives the test suite. **Implemented** as `ekos-notifier simulate`: the sample night shipped in the package (the M 31 simulator recording), a recording file, or one of the user's own sessions from the database (`--session last` or an id from `--list`, read-only; the states known before the session, such as the site, park state, pier side and INDI readings, are carried in as snapshots). The rules are built as the service builds them (config, and Ekos's limits from KStars's settings); the disk isn't checked. It prints each alert with its time, priority and body, marking those held for the digest; `--send` sends the ones that would go out at once, titled "[Simulated]", with an Emergency sent as High. Replaying a session from the database gives the same alerts as the night itself (tested, within one 10 s tick). A damaged recording replays as far as it goes: a file cut off mid-stream (a crash or power cut while recording) stops there, a line that isn't a record is skipped, and the heading says which; a file that isn't a recording at all is an error.

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
| Contributing | Adapters, presets, dev setup with simulation |

## Architecture, packaging and platforms

One Python process on the imaging computer: adapters feed events in, SQLite stores everything first, and every output is a consumer reading from the store.

The PHD2 adapter (`ekos_notifier.phd2`) reads PHD2's event server and sends nothing; PHD2 serves several clients, so Ekos's connection is unaffected. PHD2's connect burst (`Version` … `AppState`) is recorded as a snapshot, and the guider state (`stopped`, `looping`, `calibrating`, `guiding`, `lost_lock`, `paused`) is tracked from the events that follow. Guide steps are recorded at PHD2's rate (about one a second), about 240 bytes each in the database: some 5 MB for a 6-hour night. It reads which guider Ekos uses from the running KStars (Flatpak or native): the selected equipment profile's guider (`userdb.sqlite` `profile`, selected by `kstarsrc` `[Ekos] profile`) and the Guide module's setting (`kstarsrc` `[Guide] GuiderType`, `PHD2Host`, `PHD2Port`), which Ekos sets from the profile when it starts. If either says PHD2, it connects to that host and port. Only when both say another guider (internal or lin_guider) does it leave PHD2 alone, re-reading the settings every 30 s so a switch mid-night is picked up; the choice is recorded as an `ekos.guider` event. When it can't tell (KStars not running yet), or PHD2 isn't running, it retries quietly.

```
 Sources                  Ekos Notifier service                Outputs
 ┌──────────────┐        ┌──────────────────────┐         ┌────────────────────┐
 │ Ekos (D-Bus) │──────▶ │ Session manager      │    ┌──▶ │ Notifier (Apprise) │
 └──────────────┘        │          │           │    │    └────────────────────┘
 ┌──────────────┐        │          ▼           │    │    ┌────────────────────┐
 │ PHD2 events  │──────▶ │ Rules engine         │    ├──▶ │ Web GUI (later)    │
 └──────────────┘        │          │           │    │    └────────────────────┘
 ┌──────────────┐        │          ▼           │    │    ┌────────────────────┐
 │ INDI devices │──────▶ │ SQLite (WAL) ────────┼────┴──▶ │ Export + sync      │
 └──────────────┘        └──────────────────────┘         │ (later)            │
                                                          └────────────────────┘
```

Because outputs only read from the store, a dead notifier, network or sync target can never lose data or block capture.

- **Language and runtime:** Python 3.10+, asyncio; `dbus-fast` for Ekos, raw sockets for PHD2, a light INDI XML client. Nothing that must compile at install time (dbus-fast's optional speed-ups fall back to pure Python).
- **Dependencies:** three at runtime, `dbus-fast` (Ekos over D-Bus), `apprise` (100+ notification services) and `PyYAML` (the config), each replacing something we couldn't reasonably build ourselves. A new one has to meet the same bar. Each is another download for `install.sh` on a rig with patchy internet, another release that can change behaviour underneath us, and more to keep working with new Pythons; a small amount of our own code (the `.po` reader, the INDI client) is preferred to a library that does a little more. Babel was turned down for number formats on these grounds (see "Message wording").
- **Platforms:** StellarMate OS (including the Arch/Flatpak release), Astroberry, any Linux with KStars; x86-64 mini PCs and Raspberry Pi (ARM64).
- **Runs where KStars runs.** Ekos is observed over the local session D-Bus, so Ekos Notifier must run on the computer running KStars/Ekos. That computer doesn't have to host the INDI server. The INDI host and port come from the active Ekos profile. A Linux laptop running KStars against a remote INDI Pi works if Ekos Notifier is on the laptop. KStars on Windows or macOS is out of scope.
- **Session bus discovery:** `DBUS_SESSION_BUS_ADDRESS`, then `/run/user/<uid>/bus`, then the address in the running `kstars` process's environment. `doctor` reports which one worked.
- **Start with the session:** `install-service` writes a systemd *user* unit (`~/.config/systemd/user/ekos-notifier.service`, `ExecStart=<this Python> -m ekos_notifier run`, `Restart=on-failure`, `WantedBy=default.target`), enables and starts it. It doesn't enable lingering itself: without it the service starts at login, like StellarMate's own `org.kde.kstars.service` (wanted by `graphical-session.target`); the command says how to enable lingering for a start at boot. The adapters wait for KStars, INDI and PHD2 by themselves, so start order doesn't matter.
- **One instance per database:** `serve()` holds an `flock` on `<database>.lock` (PID inside); a second copy exits with "already running", and `install-service` enables without starting while a manual `run` holds it.

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
- **Install:** an `install.sh` (`curl -fsSL …/install.sh | sh`, since 0.1), because StellarMate's images have no pipx and no pacman repositories, and asking users to build pipx from the AUR is too much. It needs only Python's own `venv` and `ensurepip` (part of the base `python` package on Arch, `python3-venv` on Debian/Ubuntu/Raspberry Pi OS; checked on the StellarMate VM, Python 3.14.7). No root:
  - creates `~/.local/share/ekos-notifier/venv` and `pip install`s `ekos-notifier` into it (a venv isn't affected by the system Python's `EXTERNALLY-MANAGED` marker);
  - links `~/.local/bin/ekos-notifier`, and says how to add `~/.local/bin` to `PATH` if it's missing;
  - offers `ekos-notifier install-service` (the unit runs the venv's Python, since it uses the Python that wrote it) and then the wizard;
  - re-run, it upgrades in place, and rebuilds the venv if the system Python moved to a new version (an OS update from 3.14 to 3.15 breaks a venv's link; `doctor` detects that and says to re-run the installer); `--uninstall` removes the service, the venv and the link, keeping the config and the database.
  - pipx or uv users can install the package themselves and run `ekos-notifier setup` (which offers the service).
  - **Implemented** (`install.sh`, checked end to end in CI by `tools/check_install.sh` under dash): `--from` (a version from PyPI, `main` for GitHub's latest, a checkout, any pip spec; the latest PyPI release by default), `--service`/`--no-service` (asked otherwise, on the terminal even under `curl | sh`; yes without one), `--uninstall`; writes the commented config (`ekos-notifier config init`, mode 600, never overwriting); an upgrade rewrites the unit and restarts the service. `doctor` reports a venv or a service whose Python is gone; with a terminal and no notifier set, it offers `ekos-notifier setup`. Upgrading is running it again.
- **Footprint target:** < 100 MB RAM, < 2 % CPU on a Pi 4 during a session.
- **Quality:** simulation-replay test suite in CI (GitHub Actions), semver releases, CHANGELOG, MIT LICENSE, CONTRIBUTING and issue templates from day one.

## Roadmap

Get alerts right on real rigs before adding more: the GUI, export and sync come after 1.0, once a season of real nights has shaped what they should show.

| Milestone | Scope | |
| --- | --- | --- |
| 0.1 Core | Event sources (Ekos D-Bus, PHD2, INDI), canonical metrics with capability gating, `indi-dump`, SQLite store, session lifecycle, alert rules, Apprise notifier, hourly digest, simulation mode, `install.sh` and PyPI | Released (0.1.0, 0.1.1) |
| 0.2 First run | Terminal setup wizard, `doctor`, config validation, Quick start and Alerts reference docs, KStars in any language (Terse deferred, Olde English dropped) | Released (0.2.0) |
| 0.3 Field test | Translatable messages; the build taken to real rigs (the heartbeat was dropped, see "Offline behavior") | In progress |
| 0.3.x | Fixes from real nights, power box presets from real `indi-dump`s, the open questions a real rig answers | |
| 1.0 Public | Field-tested on StellarMate (and Astroberry, given a tester); docs complete for what's built; the test matrix in CI | |
| After 1.0 | Web GUI (views, GUI first-run wizard, notes, Start/End), export folders and their JSON Schema, rclone sync and Flush, reference importer, the mount-limit Emergency, a Terse style, the `units`/`privacy` settings | |

Open questions are tracked in [open-questions.md](open-questions.md).
