"""Names for the integer states Ekos sends over D-Bus.

The tables follow the enums in the KStars source (3.8.x). Codes listed in ``VERIFIED`` were
checked against a recording: each D-Bus ``newStatus`` was matched to the state name KStars
logged at the same moment (KStars 3.8.5 Flatpak, simulators, 2026-11-16). The rest come from the
source and haven't been seen yet.
"""

from __future__ import annotations

# Ekos.ekosStatus / Ekos.indiStatus (Ekos::CommunicationStatus)
COMMUNICATION = {0: "idle", 1: "pending", 2: "success", 3: "error"}

# Capture.newStatus (Ekos::CaptureState)
CAPTURE = {
    0: "idle",
    1: "in_progress",
    2: "capturing",
    3: "pause_planned",
    4: "paused",
    5: "suspended",
    6: "aborted",
    7: "waiting",
    8: "image_received",
    9: "dithering",
    10: "focusing",
    11: "filter_focus",
    12: "changing_filter",
    13: "guider_drift",
    14: "setting_temperature",
    15: "setting_rotator",
    16: "aligning",
    17: "calibrating",
    18: "meridian_flip",
    19: "complete",
}

# Guide.newStatus (Ekos::GuideState)
GUIDE = {
    0: "idle",
    1: "aborted",
    2: "connected",
    3: "disconnected",
    4: "capture",
    5: "looping",
    6: "dark",
    7: "subframe",
    8: "star_select",
    9: "calibrating",
    10: "calibration_error",
    11: "calibration_success",
    12: "guiding",
    13: "suspended",
    14: "reacquire",
    15: "dithering",
    16: "manual_dithering",
    17: "dithering_error",
    18: "dithering_success",
    19: "dithering_settle",
}

# Focus.newStatus (Ekos::FocusState)
FOCUS = {
    0: "idle",
    1: "complete",
    2: "failed",
    3: "aborted",
    4: "waiting",
    5: "in_progress",
    6: "framing",
    7: "changing_filter",
}

# Align.newStatus (Ekos::AlignState)
ALIGN = {
    0: "idle",
    1: "complete",
    2: "failed",
    3: "aborted",
    4: "in_progress",
    5: "successful",
    6: "syncing",
    7: "slewing",
    8: "rotating",
    9: "suspended",
}

# Mount.newStatus / Mount.status (ISD::Mount::Status)
MOUNT = {0: "idle", 1: "moving", 2: "slewing", 3: "tracking", 4: "parking", 5: "parked", 6: "error"}

# Mount.newParkStatus / Mount.parkStatus (ISD::ParkStatus)
PARK = {0: "unknown", 1: "parked", 2: "parking", 3: "unparking", 4: "unparked", 5: "error"}

# Mount.pierSideChanged / Mount.pierSide (ISD::Mount::PierSide)
PIER_SIDE = {-1: "unknown", 0: "west", 1: "east"}

# Scheduler.newStatus / Scheduler.status (Ekos::SchedulerState)
SCHEDULER = {
    0: "idle",
    1: "startup",
    2: "running",
    3: "paused",
    4: "shutdown",
    5: "aborted",
    6: "loading",
}

# captureComplete "type" (FITSScale FrameType)
FRAME_TYPE = {0: "light", 1: "bias", 2: "dark", 3: "flat"}

TABLES = {
    "communication": COMMUNICATION,
    "capture": CAPTURE,
    "guide": GUIDE,
    "focus": FOCUS,
    "align": ALIGN,
    "mount": MOUNT,
    "park": PARK,
    "pier_side": PIER_SIDE,
    "scheduler": SCHEDULER,
    "frame_type": FRAME_TYPE,
}

VERIFIED = {
    "communication": {0},
    "capture": {0, 1, 2, 8, 12, 16, 17, 18, 19},
    "guide": {1, 5, 8, 9, 11, 12, 13, 18},
    "focus": {1, 5},
    "align": {1, 4, 5, 6},
    "mount": {2, 3},
    "park": {1, 4},
    "pier_side": {0, 1},
    "scheduler": {0, 2},
    "frame_type": {0, 3},
}


def name(table: str, code: int) -> str:
    """The state's name, or ``unknown_<code>`` for a code this KStars version doesn't define."""
    return TABLES[table].get(code, f"unknown_{code}")
