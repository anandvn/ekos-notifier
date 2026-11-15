#!/usr/bin/env python3
"""Scrub private details from a replay recording before committing it (stdlib only).

- Site coordinates: INDI GEOGRAPHIC_COORD values are replaced with --lat/--lon/--elev, and any
  other text containing the original latitude or longitude (to 2 decimals) is rewritten too.
- Hostnames: PHD2's "Host" field and any occurrence of --hostname become "rig".
- Optionally merges scenario markers (--markers) into the stream, sorted by time.

Usage:
  python tools/scrub_replay.py IN.ndjson.gz OUT.ndjson.gz --lat 51.48 --lon 0.0 --elev 46 \
      [--hostname myrig] [--markers markers.ndjson]
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import socket


def find_site(lines: list[str]) -> tuple[float, float] | None:
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--elev", type=float, required=True)
    ap.add_argument("--hostname", default=socket.gethostname())
    ap.add_argument("--markers", help="scenario marker NDJSON to merge in")
    args = ap.parse_args()

    with gzip.open(args.src, "rt", encoding="utf-8") as f:
        lines = f.read().splitlines()
    meta, body = lines[0], lines[1:]
    if args.markers:
        with open(args.markers, encoding="utf-8") as f:
            body += [ln for ln in f.read().splitlines() if ln.strip()]
        body.sort(key=lambda ln: json.loads(ln)["t"])

    site = find_site(body)
    subs: list[tuple[re.Pattern, str]] = []
    if site:
        subs += [(number_pattern(site[0]), f"{args.lat}"), (number_pattern(site[1]), f"{args.lon}")]
        # INDI expresses longitude east-positive 0..360; cover the west-negative form too.
        if site[1] < 0:
            subs.append((number_pattern(site[1] + 360), f"{args.lon % 360}"))
    host = re.compile(rf"\b{re.escape(args.hostname)}\b") if args.hostname else None

    geo = re.compile(r'(<defNumber name="|<oneNumber name=")(LAT|LONG|ELEV)("[^>]*>)\s*[-\d.e]+\s*')
    replacement = {"LAT": args.lat, "LONG": args.lon % 360, "ELEV": args.elev}

    def fix_geo(m: re.Match) -> str:
        return f"{m.group(1)}{m.group(2)}{m.group(3)}{replacement[m.group(2)]}"

    out, changed = [], 0
    for line in body:
        new = geo.sub(fix_geo, line)
        for pat, rep in subs:
            new = pat.sub(rep, new)
        if host:
            new = host.sub("rig", new)
        changed += new != line
        out.append(new)

    with gzip.open(args.dst, "wt", encoding="utf-8") as f:
        f.write(meta + "\n")
        f.write("\n".join(out) + "\n")
    print(f"site found: {site is not None}; {changed}/{len(out)} records rewritten -> {args.dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
