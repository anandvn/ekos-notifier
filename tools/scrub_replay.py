#!/usr/bin/env python3
"""Scrub private details from a replay recording before committing it (stdlib only).

- Site coordinates: INDI GEOGRAPHIC_COORD values are replaced with --lat/--lon/--elev, and any
  other text containing the original latitude or longitude is rewritten too, both as decimals
  (to 2 places) and as degrees and minutes ("24:31:00", "24° 31' 00\""). A slew to the zenith,
  e.g. for flats, has a declination equal to the site latitude, so it turns up in messages.
- Hostnames: PHD2's "Host" field and any occurrence of --hostname become "rig".
- Noise (dropped unless --keep-noise): D-Bus traffic that doesn't involve KStars (the recorder's
  `busctl monitor` also catches desktop services, some carrying file paths), and INDI driver
  debug messages (`[DEBUG]`, `[SCOPE]`, `[ALIGNMENT]`, …), which verbose logging turns on and
  which can be most of a recording. INDI `[INFO]`, `[WARNING]` and `[ERROR]` messages are kept.
- Several inputs (consecutive parts of one night) are joined in time order; the first one's meta
  record is kept. --since/--until trim to a window: Unix seconds, or ISO 8601 with an offset
  ("2026-11-17T03:46:00+03:00"); without an offset, the machine's current local time is used.
- Optionally merges scenario markers (--markers) into the stream, sorted by time.

Usage:
  python tools/scrub_replay.py IN.ndjson.gz [IN2.ndjson.gz ...] OUT.ndjson.gz \
      --lat 51.48 --lon 0.0 --elev 46 [--hostname myrig] [--markers markers.ndjson] \
      [--since 2026-11-16T21:30:00+03:00] [--until 2026-11-17T03:45:00+03:00] [--keep-noise]
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import socket
from datetime import datetime

INDI_KEEP_LEVELS = {"INFO", "WARNING", "ERROR"}
INDI_LEVEL = re.compile(r'<message\b[^>]*\bmessage="\[(\w+)\]')


def parse_when(value: str) -> float:
    """Unix seconds, or ISO 8601. Without an offset it means the machine's local time, which is
    wrong if the timezone changed since the recording; prefer an explicit offset."""
    try:
        return float(value)
    except ValueError:
        return datetime.fromisoformat(value).timestamp()


class NoiseFilter:
    """Keep KStars D-Bus traffic (and replies to it) and INDI messages above debug level."""

    def __init__(self) -> None:
        self.kstars_names: set[str] = set()
        self.callers: set[str] = set()

    def keep(self, rec: dict) -> bool:
        if rec.get("src") == "indi":
            m = INDI_LEVEL.match(rec.get("xml", ""))
            return m is None or m.group(1) in INDI_KEEP_LEVELS
        if rec.get("src") != "dbus":
            return True
        msg = rec.get("msg", {})
        # KStars appearing, vanishing or crashing on the bus.
        if msg.get("member") == "NameOwnerChanged":
            return "org.kde.kstars" in json.dumps(msg.get("payload"))
        about = " ".join(str(msg.get(k, "")) for k in ("interface", "path", "destination"))
        # Only KStars's own interfaces: KStars also talks to desktop services (file dialogs,
        # KDirNotify), and those carry file paths.
        if "kstars" in about.lower() and not str(msg.get("interface", "")).startswith(
            ("org.freedesktop.impl.portal", "org.freedesktop.portal", "org.kde.KDirNotify")
        ):
            if msg.get("type") == "method_call":
                self.callers.add(msg.get("sender"))
            else:
                self.kstars_names.add(msg.get("sender"))
            return True
        # Replies from KStars to a caller that called it.
        return (
            msg.get("type") in ("method_return", "error")
            and msg.get("sender") in self.kstars_names
            and msg.get("destination") in self.callers
        )


def find_site(lines) -> tuple[float, float] | None:
    for line in lines:
        if "GEOGRAPHIC_COORD" not in line:
            continue
        rec = json.loads(line)
        xml = rec.get("xml", "")
        lat = re.search(r'name="LAT"[^>]*>\s*([-\d.]+)', xml)
        lon = re.search(r'name="LONG"[^>]*>\s*([-\d.]+)', xml)
        if lat and lon:
            return float(lat.group(1)), float(lon.group(1))
    return None


def number_pattern(value: float) -> re.Pattern:
    """Match a decimal rendering of value that agrees to at least 2 decimal places."""
    whole, frac = f"{value:.6f}".split(".")
    return re.compile(rf"(?<![\d.]){re.escape(whole)}\.{frac[:2]}\d*")


def sexagesimal_pattern(value: float, new: float) -> tuple[re.Pattern, object]:
    """Match value as degrees and minutes (any seconds), and render new in the same style."""
    d, m = divmod(round(abs(value) * 60), 60)
    sign = "-" if value < 0 else r"\+?"
    pat = re.compile(
        rf"(?<![\d.:+-]){sign}0*{d}(\s*[:°]\s*|\s+)0*{m:02d}(\s*[:']\s*|\s+)\d+(\.\d+)?"
    )
    nd, nm = divmod(round(abs(new) * 60), 60)
    nsign = "-" if new < 0 else ""

    def render(match: re.Match) -> str:
        return f"{nsign}{nd:02d}{match.group(1)}{nm:02d}{match.group(2)}00"

    return pat, render


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("src", nargs="+", help="input recording(s), in order")
    ap.add_argument("dst")
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--elev", type=float, required=True)
    ap.add_argument("--hostname", default=socket.gethostname())
    ap.add_argument("--markers", help="scenario marker NDJSON to merge in")
    ap.add_argument("--since", type=parse_when, help="drop records before this time")
    ap.add_argument("--until", type=parse_when, help="drop records after this time")
    ap.add_argument(
        "--keep-noise", action="store_true", help="keep non-KStars D-Bus and INDI debug"
    )
    args = ap.parse_args()

    meta, body = None, []
    for src in args.src:
        with gzip.open(src, "rt", encoding="utf-8") as f:
            lines = f.read().splitlines()
        meta = meta or lines[0]
        body += lines[1:]
    if args.markers:
        with open(args.markers, encoding="utf-8") as f:
            body += [ln for ln in f.read().splitlines() if ln.strip()]
    recs = sorted((json.loads(ln) for ln in body), key=lambda r: r["t"])
    total = len(recs)
    noise = None if args.keep_noise else NoiseFilter()
    # The site is looked up before trimming: GEOGRAPHIC_COORD is sent once, at connect.
    site = find_site(json.dumps(r) for r in recs)
    recs = [
        r
        for r in recs
        if (args.since is None or r["t"] >= args.since)
        and (args.until is None or r["t"] <= args.until)
        and (noise is None or noise.keep(r))
    ]

    subs: list[tuple[re.Pattern, str]] = []
    if site:
        subs += [(number_pattern(site[0]), f"{args.lat}"), (number_pattern(site[1]), f"{args.lon}")]
        # INDI expresses longitude east-positive 0..360; cover the west-negative form too.
        if site[1] < 0:
            subs.append((number_pattern(site[1] + 360), f"{args.lon % 360}"))
        subs += [sexagesimal_pattern(site[0], args.lat), sexagesimal_pattern(site[1], args.lon)]
        if site[1] > 180:
            subs.append(sexagesimal_pattern(site[1] - 360, args.lon))
    host = re.compile(rf"\b{re.escape(args.hostname)}\b") if args.hostname else None

    geo = re.compile(r'(<defNumber name="|<oneNumber name=")(LAT|LONG|ELEV)("[^>]*>)\s*[-\d.e]+\s*')
    replacement = {"LAT": args.lat, "LONG": args.lon % 360, "ELEV": args.elev}

    def fix_geo(m: re.Match) -> str:
        return f"{m.group(1)}{m.group(2)}{m.group(3)}{replacement[m.group(2)]}"

    out, changed = [], 0
    for rec in recs:
        line = json.dumps(rec, separators=(",", ":"), ensure_ascii=False)
        # GEOGRAPHIC_COORD is rewritten in the parsed XML, where quotes aren't JSON-escaped.
        if rec.get("src") == "indi" and "GEOGRAPHIC_COORD" in rec.get("xml", ""):
            rec = {**rec, "xml": geo.sub(fix_geo, rec["xml"])}
        new = json.dumps(rec, separators=(",", ":"), ensure_ascii=False)
        for pat, rep in subs:
            new = pat.sub(rep, new)
        if host:
            new = host.sub("rig", new)
        changed += new != line
        out.append(new)

    with gzip.open(args.dst, "wt", encoding="utf-8") as f:
        f.write(meta + "\n")
        f.write("\n".join(out) + "\n")
    print(
        f"site found: {site is not None}; kept {len(out)}/{total} records; "
        f"{changed} rewritten -> {args.dst}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
