#!/usr/bin/env python3
"""Open GoPro capture-setting maps and a helper to apply them.

Setting IDs and option codes follow the Open GoPro HTTP spec for Hero 11/12.
The string keys are the user-facing names exposed over the ROS service.
"""
from __future__ import annotations

from .camera import GoPro, MODE_PRESET_GROUP

# Open GoPro setting IDs
SID_RESOLUTION = 2
SID_FPS = 3
SID_FOV = 121
SID_HYPERSMOOTH = 135
SID_WIND = 149

RESOLUTION = {"1080p": 9, "2.7K": 4, "4K": 1, "5.3K": 100}
FPS = {"24": 10, "25": 9, "30": 8, "50": 6, "60": 5,
       "100": 2, "120": 1, "200": 13, "240": 0}
FOV = {"Wide": 0, "SuperView": 3, "Linear": 4, "MaxSuperView": 7,
       "LinearLeveling": 8, "HyperView": 9, "LinearLock": 10}
HYPERSMOOTH = {"Off": 0, "On": 1, "AutoBoost": 4}
WIND = {"Off": 0, "Auto": 2, "On": 4}

# Applied in this order: resolution must precede fps (fps options depend on it).
_PLAN = [
    ("resolution", RESOLUTION, SID_RESOLUTION),
    ("fps", FPS, SID_FPS),
    ("fov", FOV, SID_FOV),
    ("hypersmooth", HYPERSMOOTH, SID_HYPERSMOOTH),
    ("wind_reduction", WIND, SID_WIND),
]


def apply_settings(cam: GoPro, camera_mode: str = "", **fields) -> tuple[bool, str]:
    """Apply the given non-empty settings to one camera. Empty strings are
    skipped (left unchanged); unknown values are reported. Returns (ok, message)."""
    errors: list[str] = []

    if camera_mode:
        group = MODE_PRESET_GROUP.get(camera_mode)
        if group is None:
            errors.append(f"camera_mode={camera_mode}?")
        elif not cam.set_preset_group(group):
            errors.append("camera_mode set failed")

    for name, table, sid in _PLAN:
        value = fields.get(name)
        if not value:
            continue
        code = table.get(value)
        if code is None:
            errors.append(f"{name}={value}?")
        elif not cam.set_setting(sid, code):
            errors.append(f"{name} set failed")

    return (not errors), ("; ".join(errors) if errors else "ok")
