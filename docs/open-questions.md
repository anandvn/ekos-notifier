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

- [ ] Can a host-side process call the Flatpak KStars D-Bus interface (`org.kde.kstars`) on the
      session bus? Try `busctl --user list | grep kstars` and introspecting from the host.
- [ ] Where does Flatpak KStars keep its data? Expect `~/.var/app/org.kde.kstars/...`. Find the
      `.analyze` files and logs; Town Crier must search both native and Flatpak paths, and
      `doctor` must report which it found.
- [ ] Are PHD2 (TCP 4400) and the INDI server (TCP 7624) reachable on localhost when they run as Flatpaks?
- [ ] Are PHD2 and INDI Flatpaks too, or native packages? Record versions.
- [ ] Confirm Town Crier installs and runs on the host via pipx (Arch Python version, `dbus-next` works).

## Devices and presets

- [ ] INDI property names for each power box preset, starting with Anand's own power box
      (needs `indi-dump` output from the real rig).
- [ ] Which INDI weather simulator properties exist, to build the "weather station only" fixture.

## Defaults and packaging

- [ ] Default dew margin for humid sites (2 °C vs wider).
- [ ] PyPI name: `ekos-town-crier` proposed (`town-crier` likely too similar to `towncrier`). Confirm.

## Fixtures to record

- [ ] One full simulated night (setup → scheduler session → flip → park) recorded as a replay
      fixture for CI and for "simulate a night".
