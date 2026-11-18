# Alerts reference

Every message Ekos Notifier can send: what sets it off, what it's called in the config, how
it's sent by default, and how to tune it.

## Priorities

| Priority | How it's sent |
| --- | --- |
| **Emergency** | At once. On Pushover it repeats every minute until you acknowledge it (for up to an hour). Equipment at risk. |
| **High** | At once, once. The night's data is in trouble. |
| **Normal** | At once for the ones marked so below; the others wait for the hourly digest. |
| **Quiet** | Silently, or only in the hourly digest. |

Notifiers with their own priority levels (Pushover, ntfy) get the matching level; others get the
priority at the start of the title.

## Changing them

Each alert has a name (the second column below). Set any of them under `messages:` in
`~/.config/ekos-notifier/config.yaml`:

```yaml
messages:
  phd2_warning: digest     # off | digest | quiet | normal | high | emergency
```

- `off`: never sent, and left out of the digest (still recorded in the session).
- `digest`: no message of its own; a line in the next hourly digest. A problem still counts in
  the digest's "Attention".
- `quiet`: sent at once, silently.
- `normal`, `high`, `emergency`: sent at once at that priority.

An all-clear you haven't set follows its problem when that's `off` or `digest`.
`ekos-notifier config alerts` lists every name with its default and your setting, and
`config check` catches a misspelt name. The running service picks up changes by itself.

Most alerts fire only **during a session** (from the Scheduler starting, or the first light
frame, until the run ends). The exceptions are marked *any time*.

## Emergency

| Alert | Name | Default | When | Tune |
| --- | --- | --- | --- | --- |
| Mount left unparked | `ekos_stopped_mount_unparked` | emergency | Ekos stops, crashes or disconnects INDI while the mount was last seen unparked, and isn't back within 2 minutes. Nothing is watching the mount any more. *Any time.* | — |
| Battery critical | `battery_critical` | emergency | The power box's voltage at or under the critical level for a minute (AGM: 11.5 V, LiFePO4: 12.0 V). *Any time.* | `power: battery`, `critical_volts` |
| Meridian flip failed | `meridian_flip_failed` | emergency | Ekos logs that the meridian flip failed. | — |
| Meridian flip overdue | `meridian_flip_overdue` | emergency | Ekos is set to flip, and the mount is still tracking on the pre-flip side 15 minutes (plus one exposure) past the flip point. Quotes Ekos's reason. | Ekos's flip settings |

## High

| Alert | Name | Default | When | Tune |
| --- | --- | --- | --- | --- |
| Guiding lost | `guiding_lost` | high | Guiding stopped and isn't back within 5 minutes. Planned stops (slews, flips, focus, plate solving, calibration frames) don't count. Names PHD2's reason when there is one. | `alerts: guiding_recovery_minutes` |
| PHD2 warning | `phd2_warning` | high | PHD2 raises a warning or error, e.g. "not able to make sufficient corrections in RA". Once per message per session; PHD2's tips and warnings caused by Ekos shutting down are skipped. | — |
| Guiding poor | `guiding_poor` | high | Guiding over Ekos's own guide-deviation limit for 10 minutes: Ekos keeps suspending exposures and none finishes, or (when the job doesn't enforce the limit) the guide RMS stays over it. | `alerts: guiding_poor_minutes` (0: off); the limit is Ekos's (Capture job settings) |
| Capture aborted | `capture_aborted` | high | A running capture aborts with a reason from Ekos (guiding stopped, autofocus failed…) and doesn't restart within a minute. Your own Stop isn't reported. | — |
| Scheduler aborted | `scheduler_aborted` | high | The Scheduler aborts after running, with its last log line. | — |
| Capture stalled | `capture_stalled` | high | Capture is busy but nothing has finished for twice the exposure plus download (at least 2 minutes). Waiting (for guiding, focus, temperature, the flip) doesn't count. | — |
| Autofocus failed | `autofocus_failed` | high | Two autofocus runs in a row fail. Ekos's own retries are part of a run. | — |
| Plate solve failed | `plate_solve_failed` | high | Ekos can't solve a frame and doesn't manage on a retry within 3 minutes. Says whether it was after the flip or a new target, with Ekos's reason. | — |
| Clouds | `clouds` | high | The guide star fades by half for 2 minutes (PHD2's SNR), or two frames in a row lose half their stars (when Ekos counts stars). A star lost while still bright isn't clouds: that's Guiding lost. The thresholds are a first guess. | `alerts: clouds_drop_percent` (0: off) |
| Dew risk | `dew_risk` | high | The dew margin (ambient − dew point) under 2 °C, or humidity over 90 %, for 5 minutes. Names dew heaters that are off. Needs a weather sensor on INDI. | `alerts: dew_margin_c` (0: off) |
| Cooler can't hold | `cooler_cant_hold` | high | The camera reached its setpoint, then drifted more than 2 °C above it for 10 minutes. | `alerts: cooler_tolerance_c` (0: off) |
| Cooler can't reach setpoint | `cooler_cant_reach` | high | The camera is still more than 2 °C above its setpoint 20 minutes after it was set. | `alerts: cooler_tolerance_c` (0: off) |
| Battery low | `battery_low` | high | The power box's voltage at or under the low level for a minute (AGM: 12.0 V, LiFePO4: 12.8 V). Not sent as well when Battery critical has been. *Any time.* | `power: battery`, `low_volts` |
| Power output off | `power_output_off` | high | A power box output that was powering something switches off. Not when Ekos is shutting down (boxes can switch off on disconnect). | — |
| No current on an output | `power_output_dead` | high | An output stays on but stops drawing current for 30 seconds: a fuse, a cable, or the device. Boxes that report per-output current only. | — |
| Disk low | `disk_low` | high | Under 10 GB free where Ekos saves frames. Says how many more frames fit and for how long. | `alerts: disk_low_gb` (0: off) |

## Normal, sent at once

| Alert | Name | Default | When | Tune |
| --- | --- | --- | --- | --- |
| Starting session | `session_started` | normal | A session starts. With pre-flight checks: battery, disk space, dew margin, camera temperature, each warning first. | — |
| Session summary | `session_summary` | normal | The run ends (the Scheduler stops, or a sequence completes): frames per filter, integration, median HFR and guide RMS, temperature range, lowest battery, the problems raised, and whether the mount is parked. Not sent when you stopped Ekos yourself. | — |
| Twilight coming | `twilight_coming` | normal | 45 minutes before astronomical dawn, at the site in KStars's location. | `alerts: twilight_warning_minutes` |
| Target low | `target_low` | normal | 30 minutes before the target sinks below Ekos's altitude limit (the Mount module's, else the Scheduler's, else 10°). | `alerts: target_low_warning_minutes` |
| Flats reminder | `flats_reminder` | normal | With the summary: filters with lights tonight but no flats tonight. Once per filter per night. | — |
| Darks reminder | `darks_reminder` | off | A dark job starts: "cover the telescope". Once per night. *Any time.* Off unless you turn it on. | `messages: darks_reminder: normal` |

## All-clears

Each follows its problem's setting when that's `off` or `digest`.

| Alert | Name | Default | When |
| --- | --- | --- | --- |
| Mount parked | `mount_unparked_cleared` | digest | The mount is seen parked after "Mount left unparked". |
| Battery OK | `battery_ok` | normal | The voltage is back 0.3 V over the low level, after Battery low or critical. |
| Meridian flip done | `meridian_flip_done` | normal | The flip completed, after a failed or overdue flip. |
| Guiding recovered | `guiding_recovered` | normal | Guiding is back, after Guiding lost. |
| Guiding back within limits | `guiding_poor_cleared` | normal | A frame finishes, after Guiding poor. |
| Capture running again | `capture_running` | normal | The next frame, after Capture stalled. |
| Plate solve succeeded | `plate_solve_ok` | normal | The next solve, after Plate solve failed. |
| Clouds passed | `clouds_cleared` | normal | The guide star has been bright again for 5 minutes, and the latest frame's stars are back. |
| Dew risk passed | `dew_risk_cleared` | digest | 10 minutes with the margin 1 °C over the limit and humidity under 85 %. |
| Cooler at setpoint | `cooler_ok` | normal | The camera is back at its setpoint. |
| Power output back | `power_output_back` | normal | The output is on and drawing current again. |
| Disk space OK | `disk_ok` | normal | 1 GB over the limit again. |

## Quiet

| Alert | Name | Default | When | Tune |
| --- | --- | --- | --- | --- |
| Meridian flip soon | `meridian_flip_soon` | digest | 15 minutes before Ekos's flip, when Ekos is set to flip. Listed in the digest. | — |
| Hourly digest | `digest` | quiet | On the hour during a session: "All's well", "No light frames since…", or "Attention: …" (then Normal). Target, progress, guiding, conditions, and everything since the last one. | `digest: interval_minutes`, `align_to_clock`, `enabled` |
