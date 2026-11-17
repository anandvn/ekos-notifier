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
RMS and every Ekos state change. Stop it with Ctrl-C; nothing queued is lost. It doesn't send
alerts yet, and there's no GUI or systemd service yet.

| Option | Meaning |
| --- | --- |
| `--db PATH` | Database file to use instead of the default. |
| `-v`, `--verbose` | Debug logging. |
