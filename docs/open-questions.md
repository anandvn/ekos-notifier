# Open questions — verify on the StellarMate VM first

Work through these with the INDI simulators and PHD2's simulator camera running.
Record each finding here (tick the box, add a line under it) and update `docs/design.md`
and `CLAUDE.md` if a decision changes.

## Ekos / KStars

- [ ] Does Ekos expose the polar alignment result over D-Bus, or must it be read from the KStars log?
- [ ] Verify exact Ekos D-Bus object paths, signal names and payloads against the installed KStars
      (3.8.x): Capture, Guide, Focus, Align, Mount, Scheduler. Save `busctl --user introspect`
      output to `tests/fixtures/dbus/` for the record.
- [ ] How does Ekos report scheduler start/stop and job transitions (needed for automatic session start)?

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
- [ ] Are PHD2 (TCP 4400) and the INDI server (TCP 7624) reachable on localhost when they run as Flatpaks?
      Still open for StellarMate images that ship INDI as a Flatpak, as the 2.3.0 changelog says.
      **Native case verified (VM, 2026-11-15):** `indiserver` with the five simulators answers
      `getProperties` on 7624 (84 vectors, 5 devices); PHD2 2.6.14 on 4400 sends `Version`
      (MsgVersion 1) and `AppState` on connect and answers `get_app_state`, even with no
      equipment profile. Both listen on `0.0.0.0` with no authentication, so they're open to the
      LAN. Town Crier's clients should connect to localhost, and the docs shouldn't suggest
      opening these ports further.
- [x] Are PHD2 and INDI Flatpaks too, or native packages? Record versions.
      **Native on the VM:** `libindi 2.2.5` (`/usr/bin/indiserver`), `phd2 2.6.14`. Only KStars is
      a Flatpak. Native ports need no sandbox crossing.
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
