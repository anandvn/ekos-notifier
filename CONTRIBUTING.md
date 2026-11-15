# Contributing to Town Crier

Thanks for helping. The easiest ways in don't need much Python:

- **Device presets** — YAML files in `presets/` that map an INDI device's properties to Town
  Crier's metrics. Run `town-crier indi-dump` (once it exists) and open a "Device support" issue.
- **Message styles** — Jinja templates in `styles/<name>/`. Plain, Terse and Olde English are the
  first three; pirate is surely next.
- **Docs** — if something confused you during setup, that's a docs bug. Please report it.

## Development setup

```bash
git clone https://github.com/anandvn/town-crier && cd town-crier
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check .
```

A KStars/Ekos install with the INDI simulators (telescope, CCD, focuser, filter wheel, weather)
and PHD2's simulator camera is enough to develop against. No real equipment is required.

## Ground rules

- Read `docs/design.md` before larger changes; open an issue to discuss anything that changes it.
- Town Crier never controls equipment. It observes and reports.
- Nothing may assume a specific device, notifier or network: use the adapter interfaces.
- User-visible changes update the docs in the same pull request.
- Add a changelog entry under "Unreleased" in `CHANGELOG.md`.
