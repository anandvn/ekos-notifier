"""``python -m ekos_notifier``: the CLI (the systemd unit runs it this way)."""

from ekos_notifier.cli import main

raise SystemExit(main())
