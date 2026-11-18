# Ekos Notifier

**Your imaging night, watched and reported.**

Ekos Notifier watches a KStars/Ekos imaging night, alerts you when something needs you, sends an
hourly "all's well" digest, records the whole night, and brings the data home.

It is in **pre-alpha**. Start with the [design](design.md); the Quick start guide arrives with
version 0.2.

## What works today

`ekos-notifier run` runs the service in the foreground. On the computer running KStars, it finds
the session bus, follows Ekos (Capture, Guide, Focus, Align, Mount, Scheduler) and PHD2 (wherever Ekos is set
to find it), and records the night in
`~/.local/share/ekos-notifier/ekos-notifier.db`: a session per night, every frame, guiding RMS, every
Ekos state change, and PHD2's guide steps (star SNR, mass, HFD), lost stars, settling,
calibration and alerts. If Ekos is set to its internal guider, Ekos Notifier leaves PHD2 alone;
if PHD2 just isn't running yet, it keeps checking quietly. Stop it with Ctrl-C; nothing queued is lost. There's no GUI yet.

To have it start by itself with your session (and restart if it ever fails), install it as a
systemd user service:

```bash
ekos-notifier install-service           # write, enable and start it
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

Put your notifier in `~/.config/ekos-notifier/config.yaml` (any
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

Alerts so far: **Starting session**, the **session summary** as soon as the Scheduler stops or a
manual sequence completes (parked or not; it says whether the mount is parked), or 10 minutes
after Ekos crashes (if you stop Ekos yourself, the session closes quietly), and the **Ekos
stopped with the mount unparked** Emergency (2 minutes after Ekos stops; on Pushover it repeats
every minute until you acknowledge it), and **Guiding lost** (High) when guiding stops and
hasn't recovered after 5 minutes (`alerts: guiding_recovery_minutes`), followed by **Guiding
recovered** when it's back, and **PHD2's own warnings** (High, e.g. "not able to make sufficient
corrections in RA"), once per message per session. Also High: **Guiding poor** (guiding over
Ekos's own guide-deviation limit for 10 minutes, `alerts: guiding_poor_minutes`; when the
sequence enforces the limit, the time Ekos has been holding exposures), **Capture aborted** (with
Ekos's reason; your own Stop isn't reported), **Scheduler aborted**, **Capture stalled** (no
progress for twice the exposure plus download), and **Autofocus failed** (two runs in a row).

Timing alerts (Normal, sent at once): **Twilight coming** 45 minutes before astronomical dawn,
and **Target low** 30 minutes before the target sinks below Ekos's altitude limit (the mount's,
or the Scheduler's constraint). If Ekos flips, "Meridian flip soon" goes into the digest. The
site comes from KStars's location.

With a power box: **Battery critical** (Emergency, below 11.5 V for a minute, sent even outside a
session), **Battery low** (High, below 12.0 V, once) and **Battery OK** when the voltage is back.
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
silently) or "23:00 — Attention: Guiding poor", with the target, filter and job progress, lights
and integration this hour and tonight, median guide RMS, failed dithers and everything that
happened since the last one. Set it in the config's `digest:` section (`enabled`,
`interval_minutes`, `align_to_clock`).

### INDI devices

Ekos Notifier also reads your INDI devices (read only, from the server your Ekos profile uses):
weather stations, camera and focuser temperatures, and power boxes and dew controllers whose
driver uses INDI's standard power interface (libindi 2.x: Pegasus PPB/PPBA/SPB/UPB/UPB3,
SVBONY, Terrans, Celestron Dew Power, Gemini PBH, smPro, myDewControllerPro and others) are
recognised automatically. The digest shows camera temperature, ambient, humidity, dew margin and
power ("Power 12.4 V (low 12.1 V), 4.2 A"). For anything else (an older power box driver, a USB
temperature/humidity sensor), run `ekos-notifier indi-dump` with your equipment connected:
it lists your devices and the readings that could be used, and writes a file you can attach to a
"Device support" issue. Map a reading yourself in the config:

```yaml
indi:
  metrics:
    power.voltage: "Pegasus PPBA / POWER_SENSORS.SENSOR_VOLTAGE"
``` Stops Ekos makes on purpose (meridian flip, slews, autofocus, plate
solving, flats with tracking off, the end of the run) don't count. If an alert can't be delivered because the rig is
offline, it's retried for 10 minutes and then dropped, never sent late; it stays in the session
record. Change the window with `notify_expire_minutes` in the config.
