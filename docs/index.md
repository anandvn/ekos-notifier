# Ekos Notifier

**Your imaging night, watched and reported.**

Ekos Notifier watches a KStars/Ekos imaging night, alerts you when something needs you, sends an
hourly "all's well" digest, and records the whole night. (Bringing the data home, and a web
dashboard, come after 1.0.)

It is in **pre-alpha**, heading for field testing on real rigs. New here? Start with the [Quick start](quickstart.md); every alert is in
the [Alerts reference](alerts.md), and how it all works in the [design](design.md).

## Install

On the computer running KStars, as yourself (not root):

```bash
curl -fsSL https://raw.githubusercontent.com/anandvn/ekos-notifier/main/install.sh | sh
```

That installs the latest release from PyPI; `| sh -s -- --from 0.2.0` installs a given version,
and `--from main` the latest code from GitHub. It needs only
Python 3.10+ with its `venv` module (on Debian, Ubuntu and Raspberry Pi OS: `sudo apt install
python3-venv`), and:

- puts Ekos Notifier in a venv in `~/.local/share/ekos-notifier/venv` and links
  `~/.local/bin/ekos-notifier` (it tells you if `~/.local/bin` isn't on your `PATH`);
- writes a commented config to fill in, `~/.config/ekos-notifier/config.yaml`, readable only by
  you (`ekos-notifier config init` does the same; an existing config is never touched);
- offers to start Ekos Notifier with your session (the systemd user service below; `--service`
  or `--no-service` answers for you);
- offers `ekos-notifier setup` when no notifier is set up yet (with a terminal to ask on).

Run it again to upgrade: it restarts the service on the new version, and rebuilds the venv if
an OS update moved Python to a new version. `sh install.sh --uninstall` (or `| sh -s --
--uninstall`) removes the service, the venv and the command, and keeps your config and
sessions. Using pipx or uv instead works too: install `ekos-notifier`, then run `ekos-notifier
setup` (it offers the service).

## What works today

`ekos-notifier run` runs the service in the foreground. On the computer running KStars, it finds
the session bus, follows Ekos (Capture, Guide, Focus, Align, Mount, Scheduler) and PHD2 (wherever Ekos is set
to find it), and records the night in
`~/.local/share/ekos-notifier/ekos-notifier.db`: a session per night, every frame, guiding RMS, every
Ekos state change, and PHD2's guide steps (star SNR, mass, HFD), lost stars, settling,
calibration and alerts. If Ekos is set to its internal guider, Ekos Notifier leaves PHD2 alone;
if PHD2 just isn't running yet, it keeps checking quietly. Stop it with Ctrl-C; nothing queued is
lost.

To have it start by itself with your session (and restart if it ever fails), install it as a
systemd user service:

```bash
ekos-notifier install-service           # write, enable and start it (run again: restarts it)
journalctl --user -u ekos-notifier -f   # its log
ekos-notifier install-service --uninstall
```

It starts when you log in, as KStars does on StellarMate; to start at boot without a login, run
`loginctl enable-linger`. Only one Ekos Notifier runs per database: a second one (say, `run` in a
terminal while the service is up) refuses to start rather than send every alert twice.

| Option | Meaning |
| --- | --- |
| `--db PATH` | Database file to use instead of the default. |
| `--config PATH` | Config file to use instead of `~/.config/ekos-notifier/config.yaml`. |
| `-v`, `--verbose` | Debug logging. |

### Notifications

`ekos-notifier setup` sets it up with a few questions: it shows what it sees on the rig, takes
your notifier's details (Pushover, ntfy, Telegram, Discord or any Apprise URL) and sends a test,
then asks about the battery and the hourly digest. It saves only when you say so, changing just
those lines of the config and keeping the rest (and the comments); the installer offers it.

Or put your notifier in `~/.config/ekos-notifier/config.yaml` (any
[Apprise URL](https://github.com/caronc/apprise/wiki); see `config.example.yaml`):

```yaml
notify:
  - "pover://USER_KEY@APP_TOKEN"   # Pushover: user key from your dashboard, token from an app
```

Then check it and send yourself a test:

```bash
ekos-notifier config check
ekos-notifier test-notify              # add --emergency to try a repeating Pushover Emergency
```

The running service applies a saved config by itself within a few seconds (checking the file
every 2 s, once a save has settled), or at once with `systemctl --user reload ekos-notifier`
(SIGHUP). New notifier URLs, message levels and thresholds take effect without losing the
night's state (open alerts, the digest's counts, baselines); the INDI connection restarts if
its settings changed. A config with a mistake is logged and ignored: the previous settings
stay. Starting with a broken config, the service records the night with no alerts until the
file is fixed, rather than not running at all.

If an alert can't be delivered because the rig is offline, it's retried for 10 minutes and then
dropped, never sent late; it stays in the session record. Change the window with
`notify_expire_minutes` in the config.

Alerts so far: **Starting session** (with pre-flight checks: battery, free disk space, dew
margin and humidity, and camera temperature against its setpoint, whatever your rig reports;
a failed check is a warning, never a stop), the **session summary** (frames, integration,
median HFR and guide RMS, temperature and humidity range, lowest battery, the problems raised)
as soon as the Scheduler stops or a
manual sequence completes (parked or not; it says whether the mount is parked), or 10 minutes
after Ekos crashes (if you stop Ekos yourself, the session closes quietly), and the **Ekos
stopped with the mount unparked** Emergency (2 minutes after Ekos stops; on Pushover it repeats
every minute until you acknowledge it), and **Guiding lost** (High) when guiding stops and
hasn't recovered after 5 minutes (`alerts: guiding_recovery_minutes`; stops Ekos makes on
purpose, such as a meridian flip, slews, autofocus, plate solving, flats with tracking off and
the end of the run, don't count), followed by **Guiding recovered** when it's back, and **PHD2's own warnings** (High, e.g. "not able to make sufficient
corrections in RA"), once per message per session. Also High: **Guiding poor** (guiding over
Ekos's own guide-deviation limit for 10 minutes, `alerts: guiding_poor_minutes`; when the
sequence enforces the limit, the time Ekos has been holding exposures), **Capture aborted** (with
Ekos's reason; your own Stop isn't reported), **Scheduler aborted**, **Capture stalled** (no
progress for twice the exposure plus download), and **Autofocus failed** (two runs in a row).

Timing alerts (Normal, sent at once): **Twilight coming** 45 minutes before astronomical dawn,
and **Target low** 30 minutes before the target sinks below Ekos's altitude limit (the mount's,
or the Scheduler's constraint). If Ekos flips, "Meridian flip soon" goes into the digest. The
site comes from KStars's location. If the flip goes wrong: **Meridian flip failed** (Emergency)
when Ekos reports a failed flip, and **Meridian flip overdue** (Emergency) when the mount is still
tracking on the same side of the pier 15 minutes (plus one exposure) after Ekos should have
flipped; "Meridian flip done" follows once it has.

**Plate solve failed** (High) when Ekos can't solve a frame and doesn't manage on a retry within
3 minutes, saying whether it was after the meridian flip or a new target, with Ekos's reason.

**Clouds** (High) when the guide star fades by half (PHD2's SNR) for 2 minutes, or two frames
in a row lose half their stars (when Ekos counts stars in frames), with the numbers then and
now; **Clouds passed** when the stars are back. `alerts: clouds_drop_percent` sets how far is a
fade (0 turns it off). Its numbers are a first guess until it has seen real clouds.

**Disk low** (High) when the disk Ekos saves to has under 10 GB free (`alerts: disk_low_gb`),
with how many more frames fit and how long that is at tonight's pace.

With a power box: **Battery critical** (Emergency, at or under 11.5 V for a minute) and **Battery
low** (High, at or under 12.0 V, once), both sent at any time, even outside a session, and
**Battery OK** when the voltage is back.
Pick your battery in the config (`power: battery: agm_12v | lifepo4_12v | mains | custom`, or set
`low_volts` / `critical_volts`); `mains` turns the voltage alerts off. Also High: **Power output
off** when an output that was powering something switches off during a session, and **No
current on** an output that's still on but has stopped drawing current (a fuse, a cable, the
device), followed by **Power output back**. The box's own output names are used ("Camera",
"Mount"). Outputs switching off as Ekos shuts down aren't reported.

With a weather station or a temperature/humidity sensor: **Dew risk** (High) when the dew margin
stays under 2 °C (`alerts: dew_margin_c`) or humidity over 90 % for 5 minutes, naming any dew
heater that's off; "Dew risk passed" goes in the digest. With a cooled camera: **Cooler can't
hold** (High) when it drifts more than 2 °C (`alerts: cooler_tolerance_c`) above its setpoint for
10 minutes, and **Cooler can't reach setpoint** when it's still that far off 20 minutes after
the setpoint was set; "Cooler at setpoint" follows. The setpoint is the one your capture job
sets, or the temperature your camera driver reports reaching.

Every hour on the hour during a session, an **hourly digest**: "23:00 — All's well" (sent
silently), "23:00 — No light frames since 21:40" (also silent) or "23:00 — Attention: Guiding
poor", with the target, filter and job progress, lights
and integration this hour and tonight, median guide RMS, failed dithers and everything that
happened since the last one. Set it in the config's `digest:` section (`enabled`,
`interval_minutes`, `align_to_clock`).

When a session ends, a **Flats reminder** lists the filters with lights but no flats tonight.
A **Darks reminder** ("cover the telescope" as a dark job starts) is off unless you turn it on
with `messages: darks_reminder: normal`.

### Choosing which messages you get

Every alert can be turned off, held for the hourly digest, or sent at a different priority, in
the config's `messages:` section:

```yaml
messages:
  phd2_warning: digest      # list PHD2's warnings in the digest instead of sending each one
  twilight_coming: quiet    # send it, silently
  disk_low: emergency       # make it repeat until acknowledged
  dew_risk: off             # never send it (it's still recorded)
```

`ekos-notifier config alerts` lists every alert with its default and your setting. Levels:
`off`, `digest`, `quiet`, `normal`, `high`, `emergency`. All-clears (like "Guiding recovered")
follow their problem when you've set it to `off` or `digest`. `ekos-notifier config check` points
out a misspelt name and any Emergency you've lowered, and `ekos-notifier simulate` shows the
effect on a whole night.

### Check the setup

`ekos-notifier doctor` checks everything Ekos Notifier needs and prints a fix for anything
that's wrong: the install (including a venv broken by an OS update), the config (notifier URLs,
a file other users can read), the service (installed, running, out of date, or its Python
gone), the database, KStars and Ekos on D-Bus, a KStars Flatpak's network and D-Bus
permissions, KStars's language and number format, the INDI server with the devices it
recognised, PHD2 (when Ekos guides with it), the clock's sync and the free space where Ekos
saves frames. It's read-only and takes a few
seconds; start KStars and Ekos first to check everything.

### Preview the alerts

`ekos-notifier simulate` replays a night through the alert rules with your config and prints every
alert it would send, with its time and priority, so you can tune the thresholds in daylight:

```bash
ekos-notifier simulate                   # the sample night that ships with Ekos Notifier
ekos-notifier simulate --list            # your recent sessions
ekos-notifier simulate --session last    # your last session, with today's config
ekos-notifier simulate --brief           # titles only
ekos-notifier simulate --send            # also send them to your phone, marked [Simulated]
```

It reads your database without changing it. With `--send`, an Emergency is sent as High so your
phone doesn't start repeating it.

### INDI devices

Ekos Notifier also reads your INDI devices (read only, from the server your Ekos profile uses):
weather stations, camera and focuser temperatures, and power boxes and dew controllers whose
driver uses INDI's standard power interface (libindi 2.x: Pegasus PPB/PPBA/SPB/UPB/UPB3,
SVBONY, Terrans, Celestron Dew Power, Gemini PBH, smPro, myDewControllerPro and others) are
recognised automatically. A few seconds after Ekos connects them, the log says what was found,
one line for all of them ("INDI recognised: Power Box (battery voltage, current, outputs); QHY
CCD (camera temperature)"), and again whenever that changes; with nothing recognised it says so.
The digest shows camera temperature, ambient, humidity, dew margin and
power ("Power 12.4 V (low 12.1 V), 4.2 A"). For anything else (an older power box driver, a USB
temperature/humidity sensor), run `ekos-notifier indi-dump` with your equipment connected:
it lists your devices and the readings that could be used, and writes a file you can attach to a
"Device support" issue. Map a reading yourself in the config:

```yaml
indi:
  metrics:
    power.voltage: "Pegasus PPBA / POWER_SENSORS.SENSOR_VOLTAGE"
```

### Languages

KStars can run in any language it ships: Ekos Notifier reads Ekos's log through KStars's own
translations, and its numbers in KStars's own number format, so alerts work the same as in
English (`doctor` says which language and number format it found). Ekos Notifier's own
messages, on your phone and in the terminal, can be translated too; `language:` in the config
picks one (`auto`, the default, follows your system). No translations ship yet, so they're in
English; adding one is a single file and needs no Python (see CONTRIBUTING.md,
"Translating").
