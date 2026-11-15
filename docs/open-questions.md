# Open questions — verify on the StellarMate VM first

Work through these with the INDI simulators and PHD2's simulator camera running.
Record each finding here (tick the box, add a line under it) and update `docs/design.md`
and `CLAUDE.md` if a decision changes.

## Ekos / KStars

- [x] Does Ekos expose the polar alignment result over D-Bus, or must it be read from the KStars log?
      **Not exposed (KStars 3.8.5).** No Ekos or INDI interface mentions polar alignment; Align
      offers only plate-solve results (`newSolution`, `getSolutionResult`). Read it from the KStars
      log or the `.analyze` file.
- [x] Verify exact Ekos D-Bus object paths, signal names and payloads against the installed KStars
      (3.8.x): Capture, Guide, Focus, Align, Mount, Scheduler. Save `busctl --user introspect`
      output to `tests/fixtures/dbus/` for the record.
      **Saved to `tests/fixtures/dbus/kstars-3.8.5-flatpak/`** with Ekos running against the
      simulators. The objects are created when Ekos starts: `/KStars/Ekos/{Align,Capture,Focus,
      Guide,Mount,Observatory,OpticalTrain,Scheduler}` and `/KStars/INDI/{GenericDevice,Weather}/N`.
      Interfaces are `org.kde.kstars.Ekos.<Module>`. Useful signals: Capture `captureComplete`,
      `meridianFlipStarted`, `newStatus`; Guide `newAxisSigma`, `newAxisDelta`; Focus `newHFR`;
      Align `newSolution`; Mount `pierSideChanged`, `newParkStatus`, `newMeridianFlipSetup`.
      Mount exposes `altitudeLimits`, `hourAngleLimit`, `hourAngle`, `pierSide`, `parkStatus` as
      properties (enough for the mount-near-limit rule). **Focus has no properties**: state comes
      only from getter methods and signals. Payload contents still need a recorded night.
- [x] How does Ekos report scheduler start/stop and job transitions (needed for automatic session start)?
      Scheduler has a `status` property (+ `newStatus` signal), `currentJobName`, `currentJobJson`
      and `jsonJobs`. Mapping the status integers to states needs a recorded scheduler run.

## Flatpak (StellarMate Arch-based release)

- [x] Can a host-side process call the Flatpak KStars D-Bus interface (`org.kde.kstars`) on the
      session bus? Try `busctl --user list | grep kstars` and introspecting from the host.
      **Yes (VM, 2026-11-15, Flatpak KStars 3.8.5).** `org.kde.kstars` is on the host user bus
      (`/run/user/1000/bus`); `busctl --user tree` and `get-property … /KStars/Ekos ekosStatus` work.
      With Ekos idle, only `/KStars/Ekos` and `/KStars/Ekos/Scheduler` exist: the Capture, Guide,
      Focus, Align and Mount objects must appear once Ekos starts, so the adapter has to watch for
      objects appearing and disappearing, not introspect once at startup.
- [x] Where does Flatpak KStars keep its data? Expect `~/.var/app/org.kde.kstars/...`. Find the
      `.analyze` files and logs; Town Crier must search both native and Flatpak paths, and
      `doctor` must report which it found.
      **`~/.var/app/org.kde.kstars/data/kstars/`** (has `analyze/`, `logs/`, `astrometry/`);
      config in `~/.var/app/org.kde.kstars/config/kstars`. A stale `~/.local/share/kstars/` with
      an empty `logs/` also exists on the VM, so "directory exists" isn't enough: prefer the path
      matching the running KStars (Flatpak vs native), then the most recently written.
- [x] Are PHD2 (TCP 4400) and the INDI server (TCP 7624) reachable on localhost when they run as Flatpaks?
      **INDI: yes.** In local mode, Ekos starts the INDI server *bundled in the KStars Flatpak*
      (`/app/bin/indiserver`, libindi 2.2.5), not the host's native libindi, and it listens on
      host port 7624. The Flatpak has `shared=network` and `sockets=session-bus`, which is why
      both D-Bus and the port work from the host. `doctor` should check these permissions, since
      a future Flatpak that tightens them would break Town Crier. PHD2 is native here, so its
      Flatpak case is untested.
      **Native case verified (VM, 2026-11-15):** `indiserver` with the five simulators answers
      `getProperties` on 7624 (84 vectors, 5 devices); PHD2 2.6.14 on 4400 sends `Version`
      (MsgVersion 1) and `AppState` on connect and answers `get_app_state`, even with no
      equipment profile. Both listen on `0.0.0.0` with no authentication, so they're open to the
      LAN. Town Crier's clients should connect to localhost, and the docs shouldn't suggest
      opening these ports further.
- [x] Are PHD2 and INDI Flatpaks too, or native packages? Record versions.
      **On the VM:** PHD2 2.6.14 is native. A native `libindi 2.2.5` is installed, but Ekos uses
      the copy bundled in the KStars Flatpak (also 2.2.5) — see above.
- [ ] Confirm Town Crier installs and runs on the host via pipx (Arch Python version, `dbus-fast` works).
      Host Python is 3.14.7; an editable install, tests and the CLI work. pipx + `dbus-fast` not yet tried.

## Other platforms (Astroberry, Raspberry Pi OS, Ubuntu, AstroArch)

- [ ] **Which session bus can the service see?** The systemd user service reaches KStars only if
      KStars uses the systemd user bus (`/run/user/<uid>/bus`). True on the VM. On desktops that
      start a private bus per session (`dbus-launch`, e.g. some VNC setups as on Astroberry's
      XFCE/web desktop) it won't be. Check on Astroberry OS v3 and Raspberry Pi OS. Fallback
      design: find the running `kstars` process and read `DBUS_SESSION_BUS_ADDRESS` from its
      `/proc/<pid>/environ` (same user, so readable).
- [ ] **Boot without login:** `Linger=no` on the VM, so a user service won't start until someone
      logs in. `install-service` must run `loginctl enable-linger` (or tell the user to).
- [ ] **INDI host and port:** Ekos profiles can point at a remote INDI server or a port other than
      7624. Read them from the active Ekos profile (KStars `userdb.sqlite`) and don't assume
      localhost:7624.
- [ ] **Minimum KStars version:** Seen: 3.8.2 (Astroberry OS v3.1), 3.8.3 (StellarMate 2.2.0),
      3.8.5 (VM). Distro repos (Debian/Ubuntu universe) lag further behind. Pick a floor (3.8 is
      the likely answer), and save D-Bus introspection fixtures for each version in the range.
- [ ] **KStars snap** (snapcraft `kstars`): data path and D-Bus name under snap confinement are
      unknown. Support it or state that it's unsupported.

## Devices and presets

- [ ] INDI property names for each power box preset, starting with Anand's own power box
      (needs `indi-dump` output from the real rig).
- [x] Which INDI weather simulator properties exist, to build the "weather station only" fixture.
      **Only after the device is connected.** Before that, every device shows just CONNECTION,
      DRIVER_INFO and housekeeping, so Town Crier (observe-only) sees no metrics until Ekos connects
      the equipment. Capability detection must re-run when devices connect, not once at startup.
      Connected, the readings are `WEATHER_PARAMETERS` with elements `WEATHER_FORECAST`,
      `WEATHER_TEMPERATURE`, `WEATHER_HUMIDITY`, `WEATHER_WIND_SPEED`, `WEATHER_WIND_GUST`,
      `WEATHER_RAIN_HOUR`. No dew point, so `env.dewpoint` is derived (Magnus), as designed.
      **Heuristic trap:** the vectors named `WEATHER_TEMPERATURE` and `WEATHER_HUMIDITY` hold
      *thresholds* (`MIN_OK`, `MAX_OK`, `PERC_WARN`, `ALERT_TYPE`), not readings, and
      `WEATHER_CONTROL` (simulator knobs: Temperature, Humidity, …) also matches by name.
      Detection must match element names within value vectors, skip threshold/limit vectors, and
      prefer presets. These three vectors should be negative cases in the detection tests.

## Defaults and packaging

- [ ] Default dew margin for humid sites (2 °C vs wider).
- [x] PyPI name: `ekos-towncrier` (confirmed). CLI command stays `town-crier`.

## Fixtures to record

- [ ] One full simulated night (setup → scheduler session → flip → park) recorded as a replay
      fixture for CI and for "simulate a night".
      **Not yet (2026-11-15).** `tools/record_night.py` (recorder) and `tools/sim_night.py`
      (scenario) work. Six attempts reached setup (autofocus OK, PHD2 calibration and guiding OK)
      but never captured a frame under the Scheduler. The blocker in the last run: after an Ekos
      restart, the Ekos Guide module silently ignores `guide()`, both from D-Bus and from the
      Scheduler's Guide step (status stays 0, nothing logged), even after
      `disconnectGuider`/`connectGuider`. Next try: restart KStars itself before recording, or drop
      the Guide step from the job and let the scenario run guiding itself. Partial recordings are
      kept off-repo for reference.

## Ekos behaviour found while recording (KStars 3.8.5)

These change how the Ekos adapter and alert rules must work.

- [x] **Focus signals carry the optical train.** `newStatus` payload is `[[state], "Primary"]`;
      `newHFR` is `[hfr, position, ?, "Primary"]` with `hfr = -1` meaning no stars found. States
      seen: 5 = running, 1 = complete, 3 = failed. Focus methods (`start`, `status`, `abort`)
      all take the train name.
- [x] **Ekos retries a failed autofocus by itself** ("Restarting autofocus process…"), emitting
      status 3 for each attempt. The "autofocus failed: 2 consecutive failures" rule must count
      Ekos's retry loop as one attempt, or it will fire on the first real failure.
- [x] **`Scheduler.loadScheduler` needs a plain local path.** A `file://` URL returns false even
      though the argument is annotated as a QUrl.
- [x] **The Scheduler's startup/shutdown procedures are global settings**, not per file. They can
      reference devices absent from the current profile (here a Dust Cover Simulator from another
      profile), and then the job aborts before it starts. `doctor` could flag this.
- [x] **When the Scheduler finishes or aborts by itself, it stops Ekos and disconnects INDI**,
      even with the shutdown procedure disabled and the mount unparked. A manual `Scheduler.stop()`
      does not. On a real rig that leaves the mount tracking with nothing watching it, and Town
      Crier loses the mount's properties. **Proposed Emergency rule: "Ekos stopped while the mount
      was unparked"** (needs a decision before it goes into `design.md`).
- [ ] **Scheduler guiding-stage timeout fired 1 s after the stage began**, ~300 s after a previous
      (stopped) job's guiding stage. Possibly a timer not reset on `stop()`. Unconfirmed.

## Simulator setup for development

- PHD2 needs its **own** camera: add `Guide Simulator` as the guider in the Ekos profile and pick
  it in PHD2. Sharing `CCD Simulator` with Ekos makes exposures collide (focus loses its stars,
  PHD2 times out). The CCD Simulator's secondary guide chip rendered no stars.
- Set simulated seeing to 8″ (`CCD Simulator.SIM_FOCUSING.SIM_SEEING` and
  `Focuser Simulator.SEEING_SETTINGS`). At the default 3.5″, in-focus stars are about 1 px on this
  train (3.1″/px) and Ekos rejects them, so autofocus fails near best focus.
