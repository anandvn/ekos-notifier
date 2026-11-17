#!/usr/bin/env python3
"""Drive a short simulated night through Ekos, for recording replay fixtures.

DEVELOPER TOOL FOR THE INDI SIMULATORS ONLY. Ekos Notifier itself never controls equipment; this
script exists so a reproducible night can be recorded with tools/record_night.py.

Preconditions: KStars running with Ekos started on a simulator profile (telescope, CCD,
focuser, filter wheel, weather, and Guide Simulator as the guider) and PHD2 connected with
"Guide Simulator" as its camera. PHD2 must not share the CCD Simulator with Ekos: the two
fight over exposures and both focus and guiding fail.

Phases (each logged as a JSON marker line on stdout):
  setup     unpark, slew east of the meridian, autofocus, guide (calibrates), trial guiding
  session   Scheduler job: Track + Guide, an L/R sequence long enough to cross the meridian
  flip      Ekos flips the mount mid-sequence (meridian flip enabled at HA 0)
  idle      sequence done, mount left tracking for a while (the "nothing capturing" case)
  park      mount parked
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import time
from pathlib import Path

BUS = "org.kde.kstars"
# ISD::ParkStatus: 0 unknown, 1 parked, 2 parking, 3 unparking, 4 unparked, 5 error
PARKED, UNPARKED = 1, 4


def marker(phase: str, **info) -> None:
    print(json.dumps({"t": round(time.time(), 3), "src": "scenario", "phase": phase, **info}))


def busctl(*args: str) -> str:
    r = subprocess.run(["busctl", "--user", *args], capture_output=True, text=True, timeout=30)
    if r.returncode:
        raise RuntimeError(f"busctl {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def call(path: str, iface: str, method: str, sig: str = "", *vals) -> str:
    args = ["call", BUS, path, f"org.kde.kstars.{iface}", method]
    if sig:
        args += [sig, *map(str, vals)]
    return busctl(*args)


def prop(path: str, iface: str, name: str) -> list[str]:
    return busctl("get-property", BUS, path, f"org.kde.kstars.{iface}", name).split()[1:]


def phd2(method: str, params=None):
    with socket.create_connection(("127.0.0.1", 4400), timeout=5) as s:
        req = {"method": method, "id": 42}
        if params is not None:
            req["params"] = params
        s.sendall((json.dumps(req) + "\r\n").encode())
        s.settimeout(5)
        buf = b""
        while True:
            buf += s.recv(65536)
            for line in buf.decode(errors="replace").splitlines():
                try:
                    m = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if m.get("id") == 42:
                    return m.get("result", m.get("error"))


def indi_set_number(device: str, prop: str, element: str, value: float) -> None:
    """Set one INDI number directly (simulator setup only)."""
    xml = (
        f'<newNumberVector device="{device}" name="{prop}">'
        f'<oneNumber name="{element}">{value}</oneNumber></newNumberVector>\n'
    )
    with socket.create_connection(("127.0.0.1", 7624), timeout=5) as s:
        s.sendall(xml.encode())
        time.sleep(1)


def wait_for(desc: str, check, timeout: float, every: float = 2.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if check():
                return True
        except Exception:  # noqa: BLE001 - transient D-Bus/PHD2 errors while modules settle
            pass
        time.sleep(every)
    marker("timeout", waiting_for=desc, after_s=timeout)
    return False


def mount_status() -> dict:
    return {
        "ha": float(prop("/KStars/Ekos/Mount", "Ekos.Mount", "hourAngle")[0]),
        "pier": int(prop("/KStars/Ekos/Mount", "Ekos.Mount", "pierSide")[0]),
        "park": int(prop("/KStars/Ekos/Mount", "Ekos.Mount", "parkStatus")[0]),
    }


def write_plan(workdir: Path, pictures: Path, ra_h: float, dec: float, profile: str) -> Path:
    workdir.mkdir(parents=True, exist_ok=True)
    pictures.mkdir(parents=True, exist_ok=True)

    def job(filt: str, count: int, exp: float) -> str:
        return f"""<Job>
<Exposure>{exp}</Exposure><Format>Mono</Format><Encoding>FITS</Encoding>
<Binning><X>1</X><Y>1</Y></Binning><Frame><X>0</X><Y>0</Y><W>1280</W><H>1024</H></Frame>
<Temperature force='true'>-10</Temperature><Filter>{filt}</Filter><Type>Light</Type>
<Count>{count}</Count><Delay>0</Delay><GuideDitherPerJob>0</GuideDitherPerJob>
<FITSDirectory>{pictures}</FITSDirectory>
<PlaceholderFormat>/%t/%T/%F/%t_%T_%F</PlaceholderFormat><PlaceholderSuffix>3</PlaceholderSuffix>
<UploadMode>0</UploadMode><Properties></Properties>
</Job>"""

    esq = workdir / "sim_night.esq"
    esq.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<SequenceQueue version=\'2.6\'>\n'
        "<GuideDeviation enabled='false'>2</GuideDeviation>\n"
        "<GuideStartDeviation enabled='false'>2</GuideStartDeviation>\n"
        "<RefocusOnMeridianFlip enabled='false'/>\n"
        + job("Luminance", 14, 30)
        + "\n"
        + job("Red", 6, 30)
        + "\n</SequenceQueue>\n"
    )
    # Startup/shutdown procedures are global KStars settings and may reference devices that
    # aren't in this profile (e.g. the built-in profile's Dust Cover Simulator), which aborts
    # the job. Disable them here; this script unparks and parks the mount itself.
    esl = workdir / "sim_night.esl"
    esl.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<SchedulerList version='1.4'>
<Profile>{profile}</Profile>
<StartupProcedure enabled='false'></StartupProcedure>
<ShutdownProcedure enabled='false'></ShutdownProcedure>
<Job>
<Name>TC Sim Target</Name>
<Coordinates><J2000RA>{ra_h:.6f}</J2000RA><J2000DE>{dec:.6f}</J2000DE></Coordinates>
<Sequence>{esq}</Sequence>
<StartupCondition><Condition>ASAP</Condition></StartupCondition>
<Constraints></Constraints>
<CompletionCondition><Condition>Sequence</Condition></CompletionCondition>
<Steps><Step>Track</Step><Step>Guide</Step></Steps>
</Job>
</SchedulerList>
"""
    )
    return esl


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--profile", default="Ekos Notifier Sim")
    ap.add_argument("--workdir", type=Path, default=Path.home() / "Documents/ekos-notifier-sim")
    ap.add_argument("--pictures", type=Path, default=Path.home() / "Pictures/ekos-notifier-sim")
    ap.add_argument("--lead-hours", type=float, default=0.2, help="target HA at setup (east)")
    ap.add_argument("--dec", type=float, default=10.0)
    ap.add_argument("--idle-seconds", type=float, default=90)
    args = ap.parse_args()

    if prop("/KStars/Ekos", "Ekos", "ekosStatus") != ["2"]:
        marker("abort", reason="Ekos is not started")
        return 1

    # --- setup ---------------------------------------------------------------
    marker("setup_start")
    call("/KStars/Ekos/Mount", "Ekos.Mount", "setMeridianFlipValues", "bd", "true", 0.0)
    if mount_status()["park"] != UNPARKED:
        call("/KStars/Ekos/Mount", "Ekos.Mount", "unpark")
        wait_for("unpark", lambda: mount_status()["park"] == UNPARKED, 60)
    ra = float(prop("/KStars/Ekos/Mount", "Ekos.Mount", "equatorialCoords")[1])
    lst = (ra + mount_status()["ha"]) % 24
    target_ra = (lst + args.lead_hours) % 24
    marker("target", lst_h=round(lst, 4), ra_h=round(target_ra, 4), dec=args.dec)
    call("/KStars/Ekos/Mount", "Ekos.Mount", "slew", "dd", target_ra, args.dec)
    wait_for("slew", lambda: abs(mount_status()["ha"] + args.lead_hours) < 0.1, 120)

    # With the default 3.5" seeing, in-focus stars on this simulated train (~3.2"/px) are about
    # one pixel wide and Ekos rejects them, so autofocus "loses all stars" near best focus.
    # 8" seeing keeps them a few pixels wide. Then start defocused so autofocus has work to do.
    indi_set_number("CCD Simulator", "SIM_FOCUSING", "SIM_SEEING", 8)
    indi_set_number("Focuser Simulator", "SEEING_SETTINGS", "SIM_SEEING", 8)
    indi_set_number("Focuser Simulator", "ABS_FOCUS_POSITION", "FOCUS_ABSOLUTE_POSITION", 45000)
    time.sleep(15)
    marker("focus_start", start_position=45000, seeing_arcsec=8)
    # Focus methods are per optical train; use the one Capture is on.
    train = prop("/KStars/Ekos/Capture", "Ekos.Capture", "opticalTrain")[0].strip('"')
    call("/KStars/Ekos/Focus", "Ekos.Focus", "start", "s", train)
    time.sleep(5)
    focus_states = []

    def focus_done() -> bool:
        st = int(call("/KStars/Ekos/Focus", "Ekos.Focus", "status", "s", train).split()[1])
        if not focus_states or focus_states[-1] != st:
            focus_states.append(st)
        return len(focus_states) > 1 and st in (0, 1, 2, 3)

    wait_for("autofocus", focus_done, 300, every=3)
    marker("focus_end", status_sequence=focus_states)

    marker("guide_start")
    call("/KStars/Ekos/Guide", "Ekos.Guide", "connectGuider")
    time.sleep(3)
    call("/KStars/Ekos/Guide", "Ekos.Guide", "guide")
    wait_for("PHD2 guiding", lambda: phd2("get_app_state") == "Guiding", 360, every=3)
    marker("trial_guiding", seconds=120)
    time.sleep(120)
    # Stop trial guiding so the Scheduler's Guide step starts it from scratch. Handing the
    # Scheduler an already-guiding PHD2 made its guiding stage time out at once (3.8.5).
    call("/KStars/Ekos/Guide", "Ekos.Guide", "abort")
    wait_for("guiding stopped", lambda: phd2("get_app_state") == "Stopped", 30)
    marker("setup_end", mount=mount_status())

    # --- session ---------------------------------------------------------------
    esl = write_plan(args.workdir, args.pictures, target_ra, args.dec, args.profile)
    # Takes a plain local path: a file:// URL makes loadScheduler return false (KStars 3.8.5).
    ok = call("/KStars/Ekos/Scheduler", "Ekos.Scheduler", "loadScheduler", "s", str(esl))
    if ok != "b true":
        marker("abort", reason="Scheduler did not load the job", result=ok)
        return 1
    marker("scheduler_loaded", result=ok, file=esl.name)
    call("/KStars/Ekos/Scheduler", "Ekos.Scheduler", "start")
    marker("session_start")

    sched_seen: list[int] = []
    flipped = {"done": False}
    last_mount: dict = {}
    start_pier = mount_status()["pier"]

    def session_over() -> bool:
        st = int(prop("/KStars/Ekos/Scheduler", "Ekos.Scheduler", "status")[0])
        if not sched_seen or sched_seen[-1] != st:
            sched_seen.append(st)
            marker("scheduler_status", status=st)
        done = len(sched_seen) > 1 and st in (0, 5)
        try:  # the Mount object disappears if the Scheduler stops Ekos
            ms = mount_status()
        except RuntimeError:
            return done
        last_mount.update(ms)
        if not flipped["done"] and ms["pier"] != start_pier:
            flipped["done"] = True
            marker("flip_observed", mount=ms)
        return done

    wait_for("scheduler to finish", session_over, 45 * 60, every=5)
    marker("session_end", scheduler_statuses=sched_seen, flipped=flipped["done"])

    # --- Ekos may have been stopped by the Scheduler -------------------------------
    # When the Scheduler finishes on its own it shuts Ekos down, which kills the INDI server
    # even if the mount is unparked (KStars 3.8.5). Record that, then do what a user answering
    # the alert would: start Ekos again and park.
    if prop("/KStars/Ekos", "Ekos", "ekosStatus") != ["2"]:
        marker("ekos_stopped_by_scheduler", last_mount=last_mount)
        time.sleep(args.idle_seconds)
        call("/KStars/Ekos", "Ekos", "start")
        wait_for("Ekos restart", lambda: prop("/KStars/Ekos", "Ekos", "indiStatus") == ["2"], 120)
        time.sleep(10)
        marker("ekos_restarted", mount=mount_status())

    # --- idle tracking, then park ---------------------------------------------
    if mount_status()["park"] != PARKED:
        marker("idle_tracking", seconds=args.idle_seconds, mount=mount_status())
        time.sleep(args.idle_seconds)
        call("/KStars/Ekos/Guide", "Ekos.Guide", "abort")
        call("/KStars/Ekos/Mount", "Ekos.Mount", "park")
        wait_for("park", lambda: mount_status()["park"] == PARKED, 180)
    marker("parked", mount=mount_status())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
