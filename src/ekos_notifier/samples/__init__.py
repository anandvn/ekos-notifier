"""Sample data shipped with the package.

``SAMPLE_NIGHT``: a recorded night (``ekos-notifier-replay/0``) of the KStars 3.8.5 simulators on
the StellarMate VM, M 31 under the Scheduler: about 50 min of H-alpha lights with PHD2 guiding,
a meridian flip, then the Scheduler shutting Ekos down with the mount still tracking.
``ekos-notifier simulate`` replays it when given nothing else; the test suite replays it too.
The format is described in tests/fixtures/replay/README.md.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

SAMPLE_NIGHT = Path(
    str(resources.files(__package__) / "sim-m31-scheduler-kstars-3.8.5-flatpak.ndjson.gz")
)
