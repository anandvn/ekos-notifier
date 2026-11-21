"""Previews aren't lights: the StellarMate app's one-shot captures (issues #52 and #53).

Seen live (StellarMate OS 2.4.0, KStars 3.8.5 Flatpak, 2026-11-20): focusing and polar aligning
from the app ran a one-frame Capture "sequence" whose light frame was saved to
/tmp/mcp_cache_QHY_CCD_QHYminiCam8M.fits. It started a session, the sequence completing closed
it a second later (summary and Flats reminder), and Disk low checked /tmp, an 8 GB scratch area.
"""

from ekos_notifier.disk import GB, DiskRule
from ekos_notifier.events import Event
from ekos_notifier.pipeline import Pipeline, classify
from ekos_notifier.rules import Rules

CACHE = "/tmp/mcp_cache_QHY_CCD_QHYminiCam8M.fits"


def ev(t, kind, source="ekos", **data):
    return Event(1000.0 + t, kind, data, source)


def frame(t, path, kind="light"):
    return ev(t, "capture.frame", type=kind, filter="Lum", exposure=1.0, file=path)


def run(events, free=lambda path: 400 * GB):
    out = []
    disk = DiskRule(10.0, free=free, size=lambda path: None, home="/home/me")
    pipeline = Pipeline(out.append, rules=Rules(disk=disk))
    for e in events:
        pipeline.emit(e)
        pipeline.tick(e.ts)
    pipeline.tick(events[-1].ts + 120)
    return out


def alerts(out):
    return [e.data["title"] for e in out if e.kind == "alert"]


ONE_SHOT = [
    ev(0, "ekos.state", state="success"),
    ev(1, "capture.state", state="in_progress"),
    ev(2, "capture.state", state="capturing"),
    frame(5, CACHE),
    ev(5, "capture.state", state="image_received"),
    ev(5, "capture.state", state="complete"),
]


def test_a_frame_in_a_temporary_folder_is_a_preview():
    assert classify(frame(0, CACHE)).kind == "capture.preview"
    assert classify(frame(0, "/var/tmp/x.fits")).kind == "capture.preview"
    assert classify(frame(0, "/home/me/Pictures/M31/Light/x.fits")).kind == "capture.frame"
    assert classify(frame(0, "/tmpfiles/x.fits")).kind == "capture.frame"


def test_the_apps_one_shot_capture_starts_no_session():
    out = run(ONE_SHOT)
    assert not [e for e in out if e.kind == "session.started"]
    assert alerts(out) == []  # no Starting session, summary or Flats reminder
    assert [e.kind for e in out if e.data.get("file") == CACHE] == ["capture.preview"]  # recorded


def test_a_preview_during_a_session_doesnt_move_the_disk_check():
    def free(path):
        return 8 * GB if path.startswith("/tmp") else 400 * GB

    session = [
        ev(0, "ekos.state", state="success"),
        ev(1, "scheduler.state", state="running"),
        frame(10, "/home/me/Pictures/M31/Light/M31_001.fits"),
        frame(30, CACHE),  # the app's focusing shot, mid-session
        frame(200, "/home/me/Pictures/M31/Light/M31_002.fits"),
    ]
    out = run(session, free=free)
    assert "Disk low" not in alerts(out)
    # …while a real light saved on a nearly full disk still raises it
    out = run([*session[:2], frame(10, "/mnt/full/M31_001.fits")], free=lambda p: 2 * GB)
    assert "Disk low" in alerts(out)
