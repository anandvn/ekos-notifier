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

Recordings are scrubbed before they're committed: site coordinates are replaced with a fixed
public location and hostnames with `rig`. Check new recordings the same way.

## Fixtures

None committed yet; see the "Fixtures to record" item in `docs/open-questions.md`.

The planned first fixture: simulators on KStars 3.8.5 (Flatpak). Setup (autofocus, PHD2
calibration, trial guiding) → Scheduler job (L×14, R×6, 30 s) → meridian flip → mount left
tracking → park. No plate solving (no index files).
