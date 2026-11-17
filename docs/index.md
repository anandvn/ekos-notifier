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
if PHD2 just isn't running yet, it keeps checking quietly. Stop it with Ctrl-C; nothing queued is lost. There's no GUI or
systemd service yet.

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
manual sequence completes (parked or not; it says whether the mount is parked), and the **Ekos
stopped with the mount unparked** Emergency (2 minutes after Ekos stops; on Pushover it repeats
every minute until you acknowledge it), and **Guiding lost** (High) when guiding stops and
hasn't recovered after 5 minutes (`alerts: guiding_recovery_minutes`), followed by **Guiding
recovered** when it's back, and **PHD2's own warnings** (High, e.g. "not able to make sufficient
corrections in RA"), once per message per session. Also High: **Guiding poor** (guiding over
Ekos's own guide-deviation limit for 10 minutes, `alerts: guiding_poor_minutes`; when the
sequence enforces the limit, the time Ekos has been holding exposures), **Capture aborted** (with
Ekos's reason; your own Stop isn't reported), **Scheduler aborted**, **Capture stalled** (no
progress for twice the exposure plus download), and **Autofocus failed** (two runs in a row).

Every hour on the hour during a session, an **hourly digest**: "23:00 — All's well" (sent
silently) or "23:00 — Attention: Guiding poor", with the target, filter and job progress, lights
and integration this hour and tonight, median guide RMS, failed dithers and everything that
happened since the last one. Set it in the config's `digest:` section (`enabled`,
`interval_minutes`, `align_to_clock`). Stops Ekos makes on purpose (meridian flip, slews, autofocus, plate
solving, flats with tracking off, the end of the run) don't count. If an alert can't be delivered because the rig is
offline, it's retried for 10 minutes and then dropped, never sent late; it stays in the session
record. Change the window with `notify_expire_minutes` in the config.
