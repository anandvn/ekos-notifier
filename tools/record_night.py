#!/usr/bin/env python3
"""Record an imaging night as a replay fixture (developer tool, stdlib only).

Captures three timestamped streams into one gzipped NDJSON file:

- ``dbus``: every D-Bus message to or from ``org.kde.kstars`` (via ``busctl monitor``)
- ``phd2``: every line of PHD2's event server (TCP 4400)
- ``indi``: every top-level INDI XML element (TCP 7624). BLOBs are never enabled, so images
  are not recorded.

Line 1 is a ``meta`` record. Every other line is ``{"t": <unix seconds>, "src": ..., ...}``.
The recorder only listens: it sends ``getProperties`` to INDI and nothing else anywhere.

Usage: python tools/record_night.py OUT.ndjson.gz [--duration SECONDS]
Stops on SIGINT/SIGTERM or after --duration.
"""

from __future__ import annotations

import argparse
import gzip
import json
import platform
import queue
import signal
import socket
import subprocess
import threading
import time
import xml.etree.ElementTree as ET

FORMAT = "town-crier-replay/0"

stop = threading.Event()
out_q: queue.Queue[dict] = queue.Queue()


def emit(src: str, t: float | None = None, **fields) -> None:
    out_q.put({"t": round(t if t is not None else time.time(), 6), "src": src, **fields})


def dbus_reader() -> None:
    cmd = ["busctl", "--user", "monitor", "org.kde.kstars", "--json=short"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        for line in proc.stdout:
            if stop.is_set():
                break
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Drop the monitor's own bus housekeeping.
            if "org.freedesktop.DBus" in (msg.get("sender"), msg.get("destination")):
                continue
            t = msg.pop("timestamp-realtime", None)
            for k in ("endian", "flags", "version", "cookie", "reply_cookie"):
                msg.pop(k, None)
            emit("dbus", t / 1e6 if t else None, msg=msg)
    finally:
        proc.terminate()


def tcp_lines(host: str, port: int, src: str, on_connect: bytes | None, handle) -> None:
    """Connect (retrying), optionally send a greeting, feed received bytes to handle()."""
    while not stop.is_set():
        try:
            with socket.create_connection((host, port), timeout=5) as s:
                emit("recorder", event="connected", stream=src)
                if on_connect:
                    s.sendall(on_connect)
                s.settimeout(1)
                handle(None)  # reset parser state
                while not stop.is_set():
                    try:
                        chunk = s.recv(65536)
                    except TimeoutError:
                        continue
                    if not chunk:
                        break
                    handle(chunk)
        except OSError as e:
            emit("recorder", event="unavailable", stream=src, error=str(e))
        if not stop.is_set():
            emit("recorder", event="disconnected", stream=src)
            stop.wait(5)


def phd2_reader(host: str, port: int) -> None:
    buf = bytearray()

    def handle(chunk: bytes | None) -> None:
        if chunk is None:
            buf.clear()
            return
        buf.extend(chunk)
        while b"\n" in buf:
            line, _, rest = bytes(buf).partition(b"\n")
            buf[:] = rest
            if line.strip():
                try:
                    emit("phd2", msg=json.loads(line))
                except json.JSONDecodeError:
                    emit("phd2", raw=line.decode(errors="replace"))

    tcp_lines(host, port, "phd2", None, handle)


def indi_reader(host: str, port: int) -> None:
    state: dict = {}

    def handle(chunk: bytes | None) -> None:
        if chunk is None:
            # INDI is a stream of top-level elements; wrap it so a pull parser accepts it.
            state["p"] = ET.XMLPullParser(events=("start", "end"))
            state["p"].feed(b"<stream>")
            state["depth"] = 0
            return
        p = state["p"]
        p.feed(chunk)
        for event, el in p.read_events():
            if event == "start":
                state["depth"] += 1
            else:
                state["depth"] -= 1
                if state["depth"] == 1:  # closed a top-level element
                    emit("indi", xml=ET.tostring(el, encoding="unicode").strip())
                    el.clear()

    tcp_lines(host, port, "indi", b'<getProperties version="1.7"/>\n', handle)


def kstars_version() -> str | None:
    try:
        r = subprocess.run(
            ["flatpak", "info", "org.kde.kstars"], capture_output=True, text=True, timeout=10
        )
        for line in r.stdout.splitlines():
            if line.strip().startswith("Version:"):
                return line.split(":", 1)[1].strip() + " (flatpak)"
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("out")
    ap.add_argument("--duration", type=float, help="stop after this many seconds")
    ap.add_argument("--indi", default="127.0.0.1:7624")
    ap.add_argument("--phd2", default="127.0.0.1:4400")
    args = ap.parse_args()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    ih, ip = args.indi.rsplit(":", 1)
    ph, pp = args.phd2.rsplit(":", 1)
    threads = [
        threading.Thread(target=dbus_reader, daemon=True),
        threading.Thread(target=phd2_reader, args=(ph, int(pp)), daemon=True),
        threading.Thread(target=indi_reader, args=(ih, int(ip)), daemon=True),
    ]
    meta = {
        "src": "meta",
        "format": FORMAT,
        "started": time.time(),
        "kstars": kstars_version(),
        "python": platform.python_version(),
    }
    count = 0
    deadline = time.time() + args.duration if args.duration else None
    with gzip.open(args.out, "wt", encoding="utf-8") as f:
        f.write(json.dumps(meta) + "\n")
        for t in threads:
            t.start()
        last_flush = time.time()
        while not stop.is_set() or not out_q.empty():
            if deadline and time.time() >= deadline:
                stop.set()
            try:
                rec = out_q.get(timeout=0.5)
            except queue.Empty:
                continue
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
            count += 1
            if time.time() - last_flush > 5:
                f.flush()
                last_flush = time.time()
    print(f"recorded {count} records to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
