# Ekos Notifier

**Your imaging night, watched and reported.**

Ekos Notifier is a companion service for KStars/Ekos astrophotography rigs. It runs on your imaging
computer, watches the night, and:

- **Alerts you when it matters**: guiding lost, capture aborted, clouds, dew coming, battery low,
  a failed meridian flip, the mount left unparked. Not every lost star.
- **Sends an hourly digest** ("23:00 — All's well") with the target, progress, guiding and
  conditions.
- **Records every night as a session**: every frame, guide step, Ekos state change and reading,
  in a local database.
- **Works offline at a dark site.** Internet is a bonus, never a requirement: everything is
  recorded first, and alerts go out when there's a connection.

It works with the notification service you already use (Pushover, ntfy, Telegram, Discord, email
and 100+ more via [Apprise](https://github.com/caronc/apprise/wiki)) and degrades gracefully: no
power box, no weather station, no internet — no problem. It **only watches**: it never controls
your equipment.

> **Status: pre-alpha, heading for field testing.** It runs and alerts today, but hasn't had a
> season on real rigs yet, and settings may change before 1.0. After 1.0: a web dashboard, and
> carrying each night's data home. See
> [the design](https://github.com/anandvn/ekos-notifier/blob/main/docs/design.md) and [the roadmap](https://github.com/anandvn/ekos-notifier/blob/main/docs/design.md#roadmap).

**Contents:** [What you need](#what-you-need) · [Install](#install) ·
[Set up](#set-up-in-ten-minutes) · [Your config file](#your-config-file) ·
[Day to day](#day-to-day) · [Upgrade and uninstall](#upgrade-and-uninstall) ·
[Troubleshooting](#troubleshooting) · Full docs:
[anandvn.github.io/ekos-notifier](https://anandvn.github.io/ekos-notifier/)

<!-- --8<-- [start:guide] -->
## What you need

- The computer that runs KStars/Ekos: StellarMate, Astroberry, or any Linux with KStars
  (native or Flatpak), in any language KStars offers. Ekos Notifier runs on the same computer.
  Its own messages are in English for now; they're ready to be translated (see
  [Language](#language)).
- Python 3.10 or newer, which these systems have. On Debian, Ubuntu and Raspberry Pi OS you
  may also need Python's `venv` module: `sudo apt install python3-venv`.
- A phone app to receive alerts, such as Pushover or ntfy (see step 1 below).
- Optional: PHD2 for guiding (Ekos's own guider works too), and a power box or weather station
  on INDI for the battery, dew and power alerts.

## Install

Open a terminal on the imaging computer (on the desktop, or over SSH) and run, as yourself
(not with `sudo`):

```bash
curl -fsSL https://raw.githubusercontent.com/anandvn/ekos-notifier/main/install.sh | sh
```

> That installs the latest release from [PyPI](https://pypi.org/project/ekos-notifier/). For a
> given version add `-s -- --from 0.2.0`; for the latest code from GitHub, `-s -- --from main`.

The installer:

1. creates a private Python environment in `~/.local/share/ekos-notifier/venv` (nothing is
   installed system-wide, and no root is needed);
2. adds the `ekos-notifier` command in `~/.local/bin`;
3. writes a config file for you to fill in, `~/.config/ekos-notifier/config.yaml`, readable
   only by you (it will hold your notification keys);
4. asks **"Start Ekos Notifier with your session?"**: answer **Y** (or just Enter). It then
   starts now and whenever you log in, as KStars does on StellarMate;
5. offers to **set up your notifications** (`ekos-notifier setup`, below): answer **Y** to do
   it now.

If it ends by saying `~/.local/bin` isn't on your `PATH`, run the line it prints, then open a
new terminal so `ekos-notifier` is found.

Already use pipx or uv? `pipx install ekos-notifier` (or `uv tool install ekos-notifier`), then
`ekos-notifier setup`, does the same (setup offers to install the service).

## Set up in ten minutes

Out of the box, Ekos Notifier records your nights but sends nothing: it needs to know where to
send alerts.

### 1. Get your notifier's details

Pick a service and get what it needs. Ekos Notifier sends through
[Apprise](https://github.com/caronc/apprise/wiki), so almost any service works; the common ones:

| Service | What to do | As a URL |
| --- | --- | --- |
| **Pushover** (recommended: its Emergency priority repeats until you acknowledge it) | Install the app and sign up at [pushover.net](https://pushover.net). Copy **Your User Key** from the dashboard. Then **Create an Application/API Token** (name it "Ekos Notifier") and copy its **API Token**. | `pover://USER_KEY@API_TOKEN` |
| **ntfy** (free, no account) | Install the ntfy app and subscribe to a topic name that's hard to guess, e.g. `rig-k7q2x9`. | `ntfys://ntfy.sh/rig-k7q2x9` |
| **Telegram** | Create a bot with @BotFather (it gives you a bot token), send the bot a message, then get your chat ID. | `tgram://BOT_TOKEN/CHAT_ID` |
| **Discord** | Channel settings → Integrations → Webhooks → New Webhook → Copy Webhook URL. | the webhook URL as it is |
| **Email** | An app password from your mail provider. | `mailto://user:app_password@gmail.com` |

### 2. Run setup

The installer offers this at the end; any time after, run:

```bash
ekos-notifier setup
```

It shows what it can see on your rig (KStars and Ekos, your INDI devices, PHD2, and your power
box's voltage), then asks:

- **your notifier**: pick the service and paste its details. It sends a test straight away
  and asks whether it arrived. Add as many as you like.
- **your battery**: 12 V AGM, 12 V LiFePO4, mains power (no battery alerts), or your own levels.
- **the hourly digest**: on or off, and how often.

Enter keeps the answer shown in `[brackets]`, and Ctrl-C leaves without saving. At the end it
shows what will change and saves only when you say yes. It changes just those lines in
`~/.config/ekos-notifier/config.yaml`, keeping your other settings and the comments, and the
old file is kept as `config.yaml.bak`. Run it again any time to change your answers.

### 3. Check the rest

With KStars and Ekos running and your equipment connected:

```bash
ekos-notifier doctor
```

It checks the install, the config, the service, KStars and Ekos (and, for the KStars Flatpak,
that it shares the network and D-Bus), the INDI server and the devices it recognised, PHD2
(when Ekos guides with it), the clock and the free disk space. Each line is `ok`, `note`,
`warn` or `FAIL`, and anything that isn't right comes with a `→` line saying what to do. It
only looks: it sends nothing and changes nothing.

### 4. That's it

The running service picks up your saved changes by itself within a few seconds: no restart
needed. (To apply them at once: `systemctl --user reload ekos-notifier`.) If the file has a
mistake, the service keeps its previous settings and logs why (starting up with a broken file,
it records the night without sending alerts until it's fixed); `config check` shows the
mistake.

Start a session in Ekos (run the Scheduler, or capture a light frame) and you'll get
**Starting session**, with a pre-flight check of your rig.

### 5. Preview a night (optional)

To see what a night's alerts look like before you're under the stars:

```bash
ekos-notifier simulate            # replays a sample night and lists the alerts it would send
ekos-notifier simulate --send     # …and sends them, marked [Simulated]
ekos-notifier simulate --list     # your own recorded sessions; replay one with --session ID
```

### Or edit the config by hand

Everything setup does is a few lines in the config file. Open it in a text editor (`nano` is
on every system; save with Ctrl-O then Enter, quit with Ctrl-X):

```bash
nano ~/.config/ekos-notifier/config.yaml
```

Near the top you'll find:

```yaml
notify:
  # - "pover://USER_KEY@APP_TOKEN"      # Pushover
  # - "ntfys://ntfy.sh/my-rig-topic"    # ntfy
```

Delete the `# ` in front of the line for your service and put your URL (from the table above)
in its place, keeping the two spaces, the dash and the quotes:

```yaml
notify:
  - "pover://your-user-key@your-api-token"
```

Several services? One line each, lined up the same way. Then check it and send a test:

```bash
ekos-notifier config check              # says OK, or exactly what's wrong
ekos-notifier test-notify               # a test message to your phone
ekos-notifier test-notify --emergency   # Pushover: a repeating Emergency (acknowledge it in the app)
```

## Your config file

`~/.config/ekos-notifier/config.yaml` is commented throughout; every setting is optional, and
the value shown is the default. A few rules keep YAML happy:

- **Indent with spaces, never tabs**, and keep a setting lined up with the ones around it.
- **To use an example, delete the `#` and the space after it**, nothing else.
- Put URLs in **quotes**.
- After saving, run `ekos-notifier config check`. The service applies a valid file by itself
  within a few seconds; a file with a mistake is ignored (the previous settings stay) until
  it's fixed.

Sections marked *NOT USED YET* in the file (sync, gui, units, privacy) are
for features still to come; changing them does nothing for now. The ones you're most likely to
change:

### Language

```yaml
language: auto             # your system's language, or a code: en, de, pt_BR …
```

Alerts and the terminal (`doctor`, `setup`…) are in English until a translation for your
language exists; translating is one file and needs no Python (see [CONTRIBUTING.md](https://github.com/anandvn/ekos-notifier/blob/main/CONTRIBUTING.md#translating)).

### Battery

Battery alerts read your power box's input voltage. Pick your battery type:

```yaml
power:
  battery: lifepo4_12v     # agm_12v (the default: low 12.0 V, critical 11.5 V)
                           # lifepo4_12v (low 12.8 V, critical 12.0 V)
                           # mains (no voltage alerts)
  # low_volts: 12.6        # your own levels instead of the preset's
  # critical_volts: 12.2
```

**Battery low** (High) and **Battery critical** (Emergency) are sent when the voltage stays at
or under their level for a minute, at any time, even with no session running. `config check`
tells you when your own levels replace the preset's.

### Which messages you get, and how loud

Every alert has a priority; the [Alerts reference](https://anandvn.github.io/ekos-notifier/alerts/)
says what sets each one off and how to tune it. See them all, with anything you've changed:

```bash
ekos-notifier config alerts
```

```text
alert                        default    yours      what
ekos_stopped_mount_unparked  emergency             Ekos stopped with the mount unparked
battery_critical             emergency             Battery at or under the critical voltage
guiding_lost                 high                  Guiding stopped and hasn't recovered
...
```

Change any of them in the `messages:` section. Each can be `off`, `digest` (no message of its
own, just a line in the next hourly digest), `quiet` (sent silently), `normal`, `high` or
`emergency`:

```yaml
messages:
  phd2_warning: digest        # PHD2's warnings in the hourly digest, not on their own
  twilight_coming: quiet
  disk_low: emergency         # wake me up for this one
  digest: off                 # no hourly digest at all
  darks_reminder: normal      # off by default: "cover the telescope" as darks start
```

`config check` catches a misspelt name and suggests the right one. When you turn a problem off
or send it to the digest, its all-clear ("Guiding recovered", "Battery OK", …) does the same.

### Thresholds

```yaml
alerts:
  dew_margin_c: 2.0              # Dew risk: ambient − dew point under this for 5 min
  guiding_recovery_minutes: 5    # Guiding lost: not back after this long
  guiding_poor_minutes: 10       # Guiding poor: over Ekos's guide-deviation limit this long
  cooler_tolerance_c: 2.0        # Cooler: this far over the setpoint
  disk_low_gb: 10                # Disk low: under this much free where Ekos saves frames
  clouds_drop_percent: 50        # Clouds: the guide star (or frames' star count) dims this much
  twilight_warning_minutes: 45   # warn this long before astronomical dawn
  target_low_warning_minutes: 30 # warn before the target sinks below Ekos's altitude limit
```

`0` turns most of them off. Some limits are Ekos's own and set there: the guide-deviation limit
(Capture job settings), the altitude limit (the Mount module's limits, or else the Scheduler's
altitude constraint), and your site (KStars's location, used for twilight).

### The hourly digest

```yaml
digest:
  enabled: true
  interval_minutes: 60
  align_to_clock: true     # on the hour, rather than every 60 min from the session start
```

### Power boxes and weather stations

Devices on INDI are found by themselves: power boxes using INDI's standard power interface
(battery voltage, outputs, dew heaters), weather stations, and camera temperatures. The log
says what it found, a few seconds after Ekos connects them:

```bash
journalctl --user -u ekos-notifier | grep "INDI recognised" | tail -1
# … INDI recognised: Power Box (battery voltage, current, outputs); QHY CCD (camera temperature)
```

If yours isn't recognised, list its properties and map the one you want:

```bash
ekos-notifier indi-dump      # with Ekos connected: lists your devices and the readings that could be used
```

```yaml
indi:
  metrics:
    power.voltage: "My Old Box / VOLTAGE.VALUE"     # "Device name / PROPERTY.ELEMENT"
    env.humidity: "My Sensor / HUMIDITY.VALUE"
```

Please also ask for a preset, so the next person with that device doesn't have to: run
`ekos-notifier indi-dump --device "Your box"` (the name as Ekos shows it), and paste the
`indi-dump.xml` it writes into a
[Device support issue](https://github.com/anandvn/ekos-notifier/issues/new?template=device_support.yml).
It's small enough to paste for one device; your site location, user name and anything that
looks like a password or key are removed.

## Day to day

There's nothing to do: once the service is running it follows KStars by itself, starts a
session when the Scheduler starts (or the first light frame is saved; quick shots from the
StellarMate app while you focus or align don't count), and closes it when the
run ends. A typical night sends you:

- **Starting session**, with pre-flight checks (battery, disk space, dew margin, camera
  temperature) and a warning for anything that's off;
- **an hourly digest**, silent when all's well: target, filter and progress, frames and
  integration, guiding, conditions, and anything that happened;
- **alerts** when something needs you, and an all-clear when it has sorted itself out;
- **Session summary** at the end: frames per filter, integration, median HFR and guide RMS,
  temperature range, lowest battery, the problems raised and whether the mount is parked; and a
  **Flats reminder** for filters with no flats tonight.

No internet at the site? Everything is recorded anyway. An alert that can't be delivered is
retried for 10 minutes (`notify_expire_minutes`), then dropped rather than sent hours late.

Useful commands:

```bash
systemctl --user status ekos-notifier     # is it running?
journalctl --user -u ekos-notifier -f     # its log, live (Ctrl-C to stop watching)
systemctl --user reload ekos-notifier     # apply config changes now (they apply by themselves anyway)
```

Your sessions are in `~/.local/share/ekos-notifier/ekos-notifier.db` (SQLite).

### If the rig itself goes silent

Ekos Notifier sends every alert from the rig, so if the rig dies (a crash, a power cut, the
internet gone) you simply hear nothing. If your imaging computer runs around the clock, as in an
observatory, a free "dead-man's switch" covers that, with no help from Ekos Notifier: the
computer pings an outside service every minute, and the service alerts your phone when the
pings stop.

1. Make a check on [Healthchecks.io](https://healthchecks.io) (free; or self-hosted, or an
   [Uptime Kuma](https://github.com/louislam/uptime-kuma) push monitor): period 1 minute, grace
   10–30 minutes. Connect it to the same Pushover (or other) account you get alerts on. It gives
   you a ping URL.
2. On the imaging computer, run `crontab -e` and add this line with your URL:

   ```text
   * * * * * curl -fsS -m 10 --retry 3 https://hc-ping.com/YOUR-UUID > /dev/null
   ```

It can't tell a dead rig from a dropped internet connection, so expect an alert whenever the
link goes down for longer than the grace. And it pings whenever the computer is on: on a rig you
switch off after each night, every shutdown looks like a failure, so pause the check (or don't
use one) there. Battery alerts already warn you before a battery runs the rig down.

## Upgrade and uninstall

**Upgrade:** run the install command again. It upgrades in place, restarts the service, and
keeps your config and sessions.

**Uninstall:**

```bash
curl -fsSL https://raw.githubusercontent.com/anandvn/ekos-notifier/main/install.sh | sh -s -- --uninstall
```

This removes the service, the program and the command. Your config
(`~/.config/ekos-notifier`) and sessions (`~/.local/share/ekos-notifier`) stay; delete those
folders yourself if you don't want them.

## Troubleshooting

Start with `ekos-notifier doctor` (with KStars and Ekos running): it finds most of these by
itself and says what to do.

| Problem | What to do |
| --- | --- |
| `ekos-notifier: command not found` | `~/.local/bin` isn't on your `PATH`: run `echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc` (`~/.zshrc` if you use zsh), then open a new terminal. Not `~/.profile`: terminals like Konsole don't read it, and on StellarMate a `~/.bash_profile` hides it even at login. Or use the full path, `~/.local/bin/ekos-notifier`. |
| `doctor` says the venv or the service's Python "is gone" | An OS update moved Python to a new version. Run the install command again: it rebuilds the venv and restarts the service. |
| `Python … has no venv module` | `sudo apt install python3-venv`, then run the installer again. |
| `Config error: … not valid YAML` | Usually indentation: a tab, or a line not lined up with its neighbours. The message gives the line number. Compare with the examples above. |
| `Config error: … unknown alert '…'` | A misspelt name under `messages:`. The message suggests the right one, and `ekos-notifier config alerts` lists them all. |
| No messages arrive | `ekos-notifier config check` (is your URL there, and valid?), then `ekos-notifier test-notify`. If the test arrives but alerts don't, check the service is running and look at its log (above). |
| Changed the config, nothing happened | Run `ekos-notifier config check`: a file with a mistake is ignored, and the service keeps its previous settings. The log says so too (`journalctl --user -u ekos-notifier`). |
| No alerts, and the log says "Running with recording only" | The config had a mistake when the service started. The night is still recorded; fix the file (`ekos-notifier config check`) and alerts resume by themselves. |
| The service stopped and the log says the database "has schema N" | It was written by a newer Ekos Notifier (you went back a version). Install the newer version again; the service won't keep retrying until then. |
| It stops when you log out | Run `loginctl enable-linger` once, so your user services keep running (and start at boot). |
| No battery, dew or power alerts | Check what it recognised: `journalctl --user -u ekos-notifier \| grep "INDI recognised"` lists each device and what it reads ("Power Box (battery voltage, current, outputs)"). Not listed? See [Power boxes and weather stations](#power-boxes-and-weather-stations). |

Still stuck? [Open an issue](https://github.com/anandvn/ekos-notifier/issues) with what you ran,
what it said, the output of `ekos-notifier doctor` (it hides your notifier keys), and the last
lines of `journalctl --user -u ekos-notifier`.
<!-- --8<-- [end:guide] -->

## Contributing

Device presets, translations and docs are the easiest ways in. See
[CONTRIBUTING.md](https://github.com/anandvn/ekos-notifier/blob/main/CONTRIBUTING.md) to set up a development checkout.

## License

MIT — see [LICENSE](https://github.com/anandvn/ekos-notifier/blob/main/LICENSE).
