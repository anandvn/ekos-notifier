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
      `.analyze` files and logs; Ekos Notifier must search both native and Flatpak paths, and
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
      a future Flatpak that tightens them would break Ekos Notifier. PHD2 is native here, so its
      Flatpak case is untested.
      **Native case verified (VM, 2026-11-15):** `indiserver` with the five simulators answers
      `getProperties` on 7624 (84 vectors, 5 devices); PHD2 2.6.14 on 4400 sends `Version`
      (MsgVersion 1) and `AppState` on connect and answers `get_app_state`, even with no
      equipment profile. Both listen on `0.0.0.0` with no authentication, so they're open to the
      LAN. Ekos Notifier's clients should connect to localhost, and the docs shouldn't suggest
      opening these ports further.
- [x] Are PHD2 and INDI Flatpaks too, or native packages? Record versions.
      **On the VM:** PHD2 2.6.14 is native. A native `libindi 2.2.5` is installed, but Ekos uses
      the copy bundled in the KStars Flatpak (also 2.2.5) — see above.
- [ ] Confirm Ekos Notifier installs and runs on the host via pipx (Arch Python version, `dbus-fast` works).
      Host Python is 3.14.7; an editable install, tests and the CLI work. pipx + `dbus-fast` not yet tried.

## Other platforms (Astroberry, Raspberry Pi OS, Ubuntu, AstroArch)

- [ ] **Which session bus can the service see?** The systemd user service reaches KStars only if
      KStars uses the systemd user bus (`/run/user/<uid>/bus`). True on the VM. On desktops that
      start a private bus per session (`dbus-launch`, e.g. some VNC setups as on Astroberry's
      XFCE/web desktop) it won't be. Check on Astroberry OS v3 and Raspberry Pi OS. Fallback
      design: find the running `kstars` process and read `DBUS_SESSION_BUS_ADDRESS` from its
      `/proc/<pid>/environ` (same user, so readable).
- [x] **Boot without login:** `Linger=no` on the VM, so a user service starts at login. That's
      how StellarMate runs KStars itself (`org.kde.kstars.service`, a user unit wanted by
      `graphical-session.target`), so Ekos Notifier's unit (wanted by `default.target`) shares its
      lifetime. `install-service` tells the user how to enable lingering rather than doing it.
- [x] **PHD2's connect burst and event stream** (2.6.14; M 31 and IC 410 recordings, live VM
      2026-11-17): on connect PHD2 sends `Version`, then replays its current state
      (`LockPositionSet`, `StarSelected`, `CalibrationComplete`, `StartGuiding` as they apply) and
      ends with `AppState`. `AppState` is never sent again, so a client tracks the state from the
      events. While guiding, one `GuideStep` per guide exposure (2,595 in 53 min; 18,626 in the
      IC 410 night). Ekos pauses PHD2 for flats (`Paused`/`Resumed`) and stops it for the
      meridian flip, then recalibrates. When the Scheduler disconnects INDI, PHD2 raises `Alert`
      warnings "INDI camera disconnected" and "INDI mount was disconnected". Not yet seen live:
      `StarLost`, `CalibrationFailed`, a failed `SettleDone`. Ekos dithered in the M 31 run
      ("PHD2: Dithering successful" in its log) but PHD2 sent no `GuidingDithered`; the decoder
      follows PHD2's documented fields for these.
- [x] **Which guider Ekos uses, and PHD2's host and port** (KStars 3.8.5 Flatpak, 2026-11-17):
      each Ekos profile has its own guider, in `userdb.sqlite` table `profile` (`guidertype` 0
      internal, 1 PHD2, 2 lin_guider; `guiderhost`, `guiderport`). The selected profile is
      `kstarsrc` `[Ekos] profile` (default "Simulators"). Starting Ekos with an internal-guider
      profile rewrote `kstarsrc` at once: `profile=` and `[Guide] GuiderType=1` were both removed
      (KDE drops values equal to the default), so Ekos copies the profile's guider into the Guide
      module's global `GuiderType`/`PHD2Host`/`PHD2Port` at start, internal included. Ekos has no
      D-Bus property for the current profile; the Guide module's `guider` property is the ST4
      device ("Telescope Simulator"), not the guider type. The Flatpak reads
      `~/.var/app/org.kde.kstars/config/kstarsrc` and `…/data/kstars/userdb.sqlite`; an old
      native `~/.config/kstarsrc` on the VM is stale. Ekos Notifier reads both the profile and the
      Guide setting through the running process's environment and `/proc/<pid>/root`; "not PHD2"
      needs both to agree.
- [x] **What a guiding failure looks like** (live, VM, 2026-11-17, PHD2, manual capture): with
      the guide camera disconnected, in the same second Ekos's Guide went `aborted`, Capture
      logged "Autoguiding stopped. Aborting..." and went `aborted`, and PHD2 went `stopped`. So
      without the Scheduler, Ekos aborts the capture *because* guiding stopped; the guiding rule
      tells this from the user stopping capture by event order. Before that, the simulator mount
      wasn't tracking (Ekos `Mount.status` idle the whole time, nothing had slewed it), and PHD2
      repeatedly lost the star ("Star lost - low mass") and warned "not able to make sufficient
      corrections in RA"; a mount that isn't tracking is now reported in the alert rather than
      treated as a planned pause.
- [x] **Ekos's guide RMS is in arcseconds** (`Guide.newAxisSigma`): checked live against PHD2's
      own guide steps × its pixel scale (3.09″/px). DEC agreed (5.9″ Ekos vs 6.3″ PHD2 over the
      same 10 min); RA was 17″ vs 23″ while drifting badly, consistent with Ekos using a moving
      window. The RMS restarts from 0 when guiding restarts.
- [x] **Ekos's guide-deviation limit** is per sequence job (`.esq` `<GuideDeviation
      enabled='false'>2`; default `kstarsrc` `[Capture] GuideDeviation` 2″, `GuideDeviationReps`
      3 samples). Capture's D-Bus interface can set it (`setMaximumGuidingDeviation`) but not
      read it. When enforced, Ekos logs "Guiding deviation %1 exceeded limit value of %2 arcsecs
      for %4 consecutive samples, suspending exposure…", "…is still higher than limit value of %2
      arcsecs" and "…is now lower than limit value of %2 arcsecs, resuming exposure" (strings
      from the KStars 3.8.5 binary, confirmed live below). These are translated in other
      languages.
- [x] **What a user's own Capture Stop logs** (live 2026-11-17): only "[CCD Simulator] Camera
      capture stopped", then `aborted`. An Ekos abort adds its reason first ("Autoguiding stopped.
      Aborting...", "Autofocus failed. Aborting exposure..."). A failed *focus* exposure made Ekos
      report Capture `aborted` while it was idle.
- [x] **Ekos's guide-deviation limit, live** (0.5″ limit): Ekos suspended the exposure at
      "Guiding deviation 3.791 exceeded limit value of 0.5 arcsecs for 3 consecutive samples",
      but logged "still higher than" / "now lower than limit value of **2** arcsecs" and resumed
      under 2″, a separate resume limit (default 2″). It then tripped again within seconds; no
      60 s exposure finished for six minutes. Changing the limit in the GUI also wrote
      `[Capture] GuideDeviation=0.5` and `EnforceGuideDeviation=true` to `kstarsrc`.
- [x] **A failed autofocus on KStars 3.8.5 ends `aborted`, not `failed`** (live): each attempt
      logs "Autofocus failed, moving back to initial focus position N", Ekos logs "Restarting
      autofocus process..." and starts again (a new `in_progress`, no `aborted` between), and
      when it gives up it reports `aborted` once. A run whose retry succeeds reports `complete`.
- [x] **Stopping Ekos makes PHD2 warn** "INDI mount was disconnected" and "INDI camera
      disconnected" in the same second as "Disconnecting INDI devices...", just before INDI
      reports idle.
- [x] **INDI host and port:** read from the active Ekos profile (`userdb.sqlite` `profile`
      `host`/`port`; on the VM both are empty for local mode, meaning localhost:7624), the same
      way as the guider settings. Config can override them.
- [ ] **Minimum KStars version:** Seen: 3.8.2 (Astroberry OS v3.1), 3.8.3 (StellarMate 2.2.0),
      3.8.5 (VM). Distro repos (Debian/Ubuntu universe) lag further behind. Pick a floor (3.8 is
      the likely answer), and save D-Bus introspection fixtures for each version in the range.
- [ ] **KStars snap** (snapcraft `kstars`): data path and D-Bus name under snap confinement are
      unknown. Support it or state that it's unsupported.

## Devices and presets

- [ ] INDI property names for each power box preset, starting with Anand's own power box
      (needs `indi-dump` output from the real rig). **Partly answered:** libindi 2.x has a
      standard `INDI::PowerInterface` (`DRIVER_INTERFACE` bit 18; `POWER_SENSORS`
      `SENSOR_VOLTAGE`/`SENSOR_CURRENT`/`SENSOR_POWER`, `POWER_CHANNELS`/`POWER_CURRENTS`/
      `POWER_LABELS` with elements `POWER_CHANNEL_n`, and `DEW_*` alike), checked with a test
      driver on libindi 2.2.5; 15 drivers on the VM use it (Pegasus, SVBONY, Terrans, …). Still
      open: a real box's stream, and which libindi release first shipped the interface (older
      drivers need config mappings).
- [x] Which INDI weather simulator properties exist, to build the "weather station only" fixture.
      **Only after the device is connected.** Before that, every device shows just CONNECTION,
      DRIVER_INFO and housekeeping, so Ekos Notifier (observe-only) sees no metrics until Ekos connects
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
- [x] PyPI name: `ekos-notifier`, CLI `ekos-notifier` (renamed from `ekos-towncrier`/`town-crier`,
      2026-11-17).

## Fixtures to record

- [x] One full simulated night (setup → scheduler session → flip → park) recorded as a replay
      fixture for CI and for "simulate a night".
      **Not yet (2026-11-15).** `tools/record_night.py` (recorder) and `tools/sim_night.py`
      (scenario) work. Six attempts reached setup (autofocus OK, PHD2 calibration and guiding OK)
      but never captured a frame under the Scheduler. The blocker in the last run: after an Ekos
      restart, the Ekos Guide module silently ignores `guide()`, both from D-Bus and from the
      Scheduler's Guide step (status stays 0, nothing logged), even after
      `disconnectGuider`/`connectGuider`. Next try: restart KStars itself before recording, or drop
      the Guide step from the job and let the scenario run guiding itself. Partial recordings are
      kept off-repo for reference.
      **Nearly (2026-11-16).** With KStars freshly started and its simulation clock running
      (KStars had been launched with `--paused`; whether that caused the stall is unconfirmed),
      the Scheduler's Guide step worked. A manually built Scheduler job (IC 410, narrowband 300 s +
      LRGB 120 s, then flats) ran 21:34–03:39: slew → autofocus → plate solve → guiding → capture
      → meridian flip (01:41, guiding recalibrated and settled in under 2 min) → R/G/B panel
      flats. KStars then segfaulted (see below), so there is no clean job end or park. The
      scrubbed recording (21:39–03:46 +03, 9.4 MB) is kept off-repo until we decide how to store
      fixtures.
      **Done (2026-11-16, short run).** Scheduler job "M 31", sequence `LRGBSHO_Short_nowall`
      (12 × 60 s each of Hα/OIII/SII, 20 × 10 s each of LRGB, 4 × 1 s panel flats per filter),
      recorded from before the Scheduler started (times EST): start 20:54 → slew → autofocus →
      plate solve → guiding 20:56 → meridian flip 21:27:52–21:28:24 → post-flip re-solve and PHD2
      recalibration (guiding settled 21:29:41) → lights done 21:48 → panel flats, no wall slew →
      "Job 'M 31' is complete" 21:49:17 → "No pre-shutdown tasks, proceeding to stop Ekos/INDI"
      → INDI disconnected, Ekos stopped 21:49:21. No crash. **The mount was never parked**: the
      Scheduler's shutdown doesn't park when the shutdown procedure is off, so the fixture ends
      in the "Ekos stopped with the mount unparked" Emergency case. Scrubbed: 1.6 MB, 140 frames,
      kept off-repo with the IC 410 one until the storage decision.
- [x] **How to store fixtures.** The short run is 1.6 MB, the IC 410 night 9.4 MB (gzipped),
      mostly high-rate mount position vectors. **Decided (2026-11-16):** the short run is
      committed unthinned (`tests/fixtures/replay/sim-m31-scheduler-kstars-3.8.5-flatpak.ndjson.gz`);
      the IC 410 night goes on a GitHub release, not in the repository.

## Ekos behaviour found while recording (KStars 3.8.5)

These change how the Ekos adapter and alert rules must work.

- [x] **Focus signals carry the optical train.** `newStatus` payload is `[[state], "Primary"]`;
      `newHFR` is `[hfr, position, ?, "Primary"]` with `hfr = -1` meaning no stars found. States
      seen: 5 = running, 1 = complete, 3 = failed. (Per the KStars source 2 = failed and
      3 = aborted; the D-Bus recording of 2026-11-16 confirms only 1 and 5. The third `newHFR`
      argument is true during autofocus. Names used by the adapter: `src/ekos_notifier/ekos/states.py`.) Focus methods (`start`, `status`, `abort`)
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
      does not. On a real rig that leaves the mount tracking with nothing watching it, and Ekos
      Notifier loses the mount's properties. **Emergency rule "Ekos stopped with the mount unparked"
      adopted (2026-11-16)** and added to `design.md`.
- [x] **A run's end, as Ekos reports it** (M 31 replay and a live run, 2026-11-17): Capture
      `complete` (19), then INDI and Ekos idle, then Scheduler `idle`, within 4 s. Capture reports
      `complete` once for a manual sequence queue. The session now closes on Scheduler idle or
      aborted after running, or on Capture complete when the Scheduler isn't running.
- [x] **The Scheduler's snapshot can read `idle` mid-run.** After a service restart at 20:11 the
      `Scheduler.status` read gave `idle`; the `running` signal came 4 s later. Snapshots are not
      trusted to end a session.
- [ ] **KStars 3.8.5 (Flatpak) segfaults at the wall-flat step**, reproduced twice (19:08 and
      03:40 +03 on 2026-11-16) with identical backtraces in Qt widget painting. Both times it came
      right after the R/G/B flats, when the next flat job (Hα, the only one with `SkyFlat=true`)
      slewed to the wall position again ("Slew to wall position complete, tracking stopped"); the
      log ends there and the crash follows 70–85 s later. Guess (unconfirmed): a light-source
      prompt dialog. Both times the INDI server outlived KStars, leaving the mount **unparked
      with nothing watching it**: the Emergency rule's case, reached by a crash. Avoided by
      removing the hidden wall pre-action (next item); no crash in the short run. To report
      upstream, reproduce with the KStars debug extension for a symbolised backtrace.
- [x] **Light jobs carry a hidden "Goto Wall" pre-action that Ekos applies at the flats.** In a
      saved `.esq`, every light job had `<PreAction><Type>3</Type><Az>0</Az><Alt>90</Alt>` and
      `<SkyFlat>true</SkyFlat>`; flat jobs had `<Type>1</Type>` and no Az/Alt. `PreAction` looks
      like bit flags (1 = none, 2 = goto wall; inferred from which jobs carry Az/Alt, not checked
      against the source). The GUI disables the Calibration button on light jobs, so this can't
      be seen or cleared there, yet the IC 410 run slewed to the wall at the first flat. With
      the light jobs edited by hand to `Type 1`, no Az/Alt and `SkyFlat=false`, the short run's
      flats used the panel with no slew. Worth reporting upstream with the crash.
- [x] **The Scheduler's normal end stops Ekos without parking** ("No pre-shutdown tasks,
      proceeding to stop Ekos/INDI"; 21:49 EST in the short run). Confirms the item above on a
      clean, crash-free run.
- [x] **Post-flip recalibration logs an error, then recovers**: "Post meridian flip calibration
      error. Restarting..." 0.2 s after PHD2 cleared its calibration, then a normal calibration
      and "Post meridian flip calibration completed successfully". Intent-aware rules must not
      alert on it.
- [x] **Ekos disables the meridian flip during polar alignment** ("Meridian flip set inactive
      during polar alignment", re-activated when it finishes). Not a fault.
- [x] **The Scheduler logs each job stage as a readable line** ("Job 'X' is slewing to target",
      "… is focusing", "… guiding is in progress", "… capture is in progress"), plus per-filter
      progress and a time-to-complete estimate. A simpler source for job progress than mapping
      the `status` integers; worth checking whether the same strings reach D-Bus (`newLog`).
- [x] **A manual autofocus failure ran three attempts**, the third logged as both "aborted" and
      "failed", and Capture then aborted its exposure ("Autofocus failed. Aborting exposure").
      Confirms the retry-loop note above; the rule must also not count "aborted" twice.
- [ ] **`captureComplete` reports `hfr = -1` and `starCount = 0` for every light frame**, with
      stars plainly present (guiding and plate solving worked). Find out whether Capture only
      measures HFR when an option is on; the session summary's median HFR depends on it.
- [x] **Ekos state codes checked against a recording.** Each D-Bus `newStatus` in the M 31 run
      was matched to the state name KStars logged at the same moment; the confirmed codes are
      listed in `src/ekos_notifier/ekos/states.py` (`VERIFIED`). `Capture.meridianFlipStarted` exists
      in the introspection but wasn't emitted during the flip: the flip shows as Capture state 18
      and a `pierSideChanged`.
- [x] **The Flatpak KStars process reports a sandbox-only bus address** (`unix:path=/run/flatpak/bus`
      in `/proc/<pid>/environ`), so the "read it from the kstars process" fallback only helps for
      native KStars. The adapter tries each candidate by connecting, which weeds it out.
- [x] **Ekos module objects don't exist while Ekos is stopped**: `Properties.Get` on
      `/KStars/Ekos/Mount` returns `org.freedesktop.DBus.Error.UnknownObject`. `/KStars/Ekos` and
      `/KStars/Ekos/Scheduler` always exist.
- [x] **Verbose logging floods the INDI stream** with driver debug messages (`[DEBUG]`,
      `[SCOPE]`, `[ALIGNMENT]`: ~60 % of the records in a recording). The INDI adapter should ignore them;
      `scrub_replay.py` now drops them from fixtures.
- [x] **A slew to the zenith (e.g. for flats) has a declination equal to the site latitude**, so the
      latitude leaks into INDI messages as `dd:mm:ss`. `scrub_replay.py` now scrubs sexagesimal
      forms too.
- [ ] **Scheduler guiding-stage timeout fired 1 s after the stage began**, ~300 s after a previous
      (stopped) job's guiding stage. Possibly a timer not reset on `stop()`. Unconfirmed.

## Simulator setup for development

- PHD2 needs its **own** camera: add `Guide Simulator` as the guider in the Ekos profile and pick
  it in PHD2. Sharing `CCD Simulator` with Ekos makes exposures collide (focus loses its stars,
  PHD2 times out). The CCD Simulator's secondary guide chip rendered no stars.
- Set simulated seeing to 8″ (`CCD Simulator.SIM_FOCUSING.SIM_SEEING` and
  `Focuser Simulator.SEEING_SETTINGS`). At the default 3.5″, in-focus stars are about 1 px on this
  train (3.1″/px) and Ekos rejects them, so autofocus fails near best focus.
