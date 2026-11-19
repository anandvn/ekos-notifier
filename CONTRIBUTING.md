# Contributing to Ekos Notifier

Thanks for helping. The easiest ways in don't need much Python:

- **Device presets** — YAML files in `src/ekos_notifier/indi/presets/` that map an INDI device's
  properties to Ekos Notifier's metrics. Run `ekos-notifier indi-dump` and open a "Device
  support" issue.
- **Translations** — Ekos Notifier's messages in your language. No Python needed; see below.
- **Docs** — if something confused you during setup, that's a docs bug. Please report it.

## Translating

The messages are in `src/ekos_notifier/locale/ekos-notifier.pot`, a gettext template. A
translation is one file next to it, `<language>.po`: `de.po`, `fr.po`, `pt_BR.po`.

1. Start a new language from the template, with Poedit ("Create new translation"), or:

   ```bash
   msginit -l de -i src/ekos_notifier/locale/ekos-notifier.pot -o src/ekos_notifier/locale/de.po
   ```

   For a language that already exists, edit its `.po`.
2. Translate. Keep every `{placeholder}` exactly as it is (`{time}`, `{volts}`): a translation
   may move them, but not rename, add or drop one. Plural messages have one line per plural
   form of your language. Untranslated or "fuzzy" entries are shown in English, so a partial
   translation is fine to send.
3. Try it: set `language: de` in your config and run `ekos-notifier simulate`.
4. Check it, and open a pull request with the `.po` file:

   ```bash
   python tools/update_pot.py --check   # placeholders, and messages that no longer exist
   ```

When messages change, the template is regenerated (`python tools/update_pot.py`, which a test
requires); bring a translation up to date with Poedit's "Update from POT file" or
`msgmerge --update src/ekos_notifier/locale/de.po src/ekos_notifier/locale/ekos-notifier.pot`.

A developer adding a message marks it for translation: `_("Ekos solved the frame at {time}.")
.format(time=…)`, `ngettext("{n} dither failed", "{n} dithers failed", n)`, and `N_("…")` for
a table entry translated where it's used. Put values in as named placeholders, formatted
first (`volts=f"{v:.2f}"`), and write whole sentences rather than joining fragments.

## Development setup

```bash
git clone https://github.com/anandvn/ekos-notifier && cd ekos-notifier
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check .
python tools/update_pot.py --check   # the translation template is current
sh tools/check_install.sh   # install.sh end to end in a throwaway home (needs the internet)
```

A KStars/Ekos install with the INDI simulators (telescope, CCD, focuser, filter wheel, weather)
and PHD2's simulator camera is enough to develop against. No real equipment is required.

## Ground rules

- Read `docs/design.md` before larger changes; open an issue to discuss anything that changes it.
- Ekos Notifier never controls equipment. It observes and reports.
- Nothing may assume a specific device, notifier or network: use the adapter interfaces.
- User-visible changes update the docs in the same pull request.
- Add a changelog entry under "Unreleased" in `CHANGELOG.md`.

## Releasing (maintainers)

1. On a branch: set `__version__` in `src/ekos_notifier/__init__.py`, and turn CHANGELOG's
   "Unreleased" into `## X.Y.Z — YYYY-MM-DD` (keep an empty "Unreleased" above it). Merge it.
2. Tag main and push the tag:

   ```bash
   git checkout main && git pull
   git tag -a vX.Y.Z -m "Ekos Notifier X.Y.Z" && git push origin vX.Y.Z
   ```

3. The Release workflow checks the tag matches the version, builds and checks the package,
   publishes it to PyPI (trusted publishing: no token), and creates the GitHub release from the
   CHANGELOG section. A PyPI version can never be uploaded twice: a mistake means a new version.
