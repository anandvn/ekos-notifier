# Town Crier

**Hear ye, hear ye — your imaging night, reported.**

Town Crier watches a KStars/Ekos imaging night, alerts you when something needs you, sends an
hourly "all's well" digest, records the whole night, and brings the data home.

It is in **pre-alpha**. Start with the [design](design.md); the Quick start guide arrives with
version 0.2.

## What works today

`town-crier run` runs the service in the foreground. On the computer running KStars, it finds
the session bus, follows Ekos (Capture, Guide, Focus, Align, Mount, Scheduler) and records the
night in `~/.local/share/town-crier/town-crier.db`: a session per night, every frame, guiding
RMS and every Ekos state change. Stop it with Ctrl-C; nothing queued is lost. There's no GUI or
systemd service yet.

| Option | Meaning |
| --- | --- |
| `--db PATH` | Database file to use instead of the default. |
| `--config PATH` | Config file to use instead of `~/.config/town-crier/config.yaml`. |
| `-v`, `--verbose` | Debug logging. |

### Notifications

Put your notifier in `~/.config/town-crier/config.yaml` (any
[Apprise URL](https://github.com/caronc/apprise/wiki); see `config.example.yaml`):

```yaml
notify:
  - "pover://USER_KEY@APP_TOKEN"   # Pushover: user key from your dashboard, token from an app
```

Then check it and send yourself a test:

```bash
town-crier config check
town-crier test-notify              # add --emergency to try a repeating Pushover Emergency
```

Alerts so far: **Starting session**, the **session summary** as soon as the Scheduler stops or a
manual sequence completes (parked or not; it says whether the mount is parked), and the **Ekos
stopped with the mount unparked** Emergency (2 minutes after Ekos stops; on Pushover it repeats
every minute until you acknowledge it). If an alert can't be delivered because the rig is
offline, it's retried for 10 minutes and then dropped, never sent late; it stays in the session
record. Change the window with `notify_expire_minutes` in the config.
