# Replay fixtures

Recorded nights that drive the test suite and the user-facing "simulate a night" feature.
Recorded with `tools/record_night.py`; simulated nights are scripted with `tools/sim_night.py`.

## Format: `town-crier-replay/0` (gzipped NDJSON)

Line 1 is a `meta` record (`format`, `started`, `kstars`, `python`). Every other line is:

```json
{"t": 1794757136.653, "src": "<stream>", ...}
```

| `src` | Fields | Contents |
| --- | --- | --- |
| `dbus` | `msg` | One D-Bus message to or from `org.kde.kstars` as printed by `busctl monitor --json=short` (`type`, `sender`, `path`, `interface`, `member`, `payload`). |
| `phd2` | `msg` | One PHD2 event-server message, verbatim. |
| `indi` | `xml` | One top-level INDI element (`defXXXVector`, `setXXXVector`, `delProperty`, `message`). BLOBs are never enabled, so no images. |
| `recorder` | `event`, `stream` | Stream connected/disconnected/unavailable. |
| `scenario` | `phase`, … | Phase markers from `sim_night.py` (simulated nights only; merged after recording). |

`t` is Unix time in seconds. D-Bus records use the bus's own timestamp; the others use the time of
receipt.

Version 0 means unstable: the format may change until the replay engine exists.

## Privacy

Recordings are scrubbed with `tools/scrub_replay.py` before they're committed: site coordinates
(decimal and degrees-minutes forms) are replaced with a fixed public location, and hostnames with
`rig`. It also drops noise: D-Bus traffic that doesn't involve KStars (desktop services, some with
file paths) and INDI driver debug messages. Check new recordings the same way: search the output
for the real coordinates, hostname and username before committing.

## Fixtures

### `sim-m31-scheduler-kstars-3.8.5-flatpak.ndjson.gz` (1.6 MB)

Simulators on KStars 3.8.5 (Flatpak), PHD2 2.6.14 with the Guide Simulator, recorded 2026-11-16
(times below are EST, UTC−5). Scrubbed with `tools/scrub_replay.py` (site → 51.48, 0.0, 46 m).
Recording starts ~6 min before the Scheduler and ends just after its shutdown.

| Time | What happens |
| --- | --- |
| 20:48:38 | Recording starts. Ekos connected, Scheduler idle, mount unparked. |
| 20:54:23 | Scheduler starts job "M 31": slew → autofocus → plate solve → PHD2 calibration → guiding. |
| 20:56:42 | Capture starts: 12 × 60 s each of Hα, OIII, SII (pier side changes at 21:28). |
| 21:27:52 | Meridian flip; 21:28:24 flip complete; post-flip re-solve; Ekos logs a transient "post meridian flip calibration error", then PHD2 recalibrates; guiding settled 21:29:41. |
| 21:34:42 | 20 × 10 s each of L, R, G, B. |
| 21:48:24 | Panel flats, 4 × 1 s for each of the six filters (no mount movement). |
| 21:49:17 | Job complete. The Scheduler stops guiding, disconnects INDI and stops Ekos (21:49:21) **without parking the mount**: the "Ekos stopped with the mount unparked" case. |
| 21:50:00 | Recording ends. |

140 `captureComplete` signals. Light frames report `hfr = -1` and `starCount = 0` (see
`docs/open-questions.md`). INDI driver debug was off.

A longer night (IC 410, 21:39–03:46 UTC+3, 9.4 MB, ends in a KStars crash) is kept outside the
repository; see the "How to store fixtures" item in `docs/open-questions.md`.
