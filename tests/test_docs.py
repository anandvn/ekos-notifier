"""The docs that describe the code are checked against it."""

import re
from pathlib import Path

from ekos_notifier.messages import ALERTS

DOCS = Path(__file__).resolve().parents[1] / "docs"


def test_the_alerts_reference_has_every_alert_with_its_default():
    rows = re.findall(
        r"^\| [^|]+ \| `([a-z_0-9]+)` \| ([a-z]+) \|", (DOCS / "alerts.md").read_text(), re.M
    )
    documented = dict(rows)
    assert len(rows) == len(documented), "an alert is listed twice"
    assert set(documented) == set(ALERTS), (
        f"missing: {sorted(set(ALERTS) - set(documented))}, "
        f"unknown: {sorted(set(documented) - set(ALERTS))}"
    )
    wrong = {n: (d, ALERTS[n].default) for n, d in documented.items() if d != ALERTS[n].default}
    assert not wrong, f"documented default differs from the code: {wrong}"
