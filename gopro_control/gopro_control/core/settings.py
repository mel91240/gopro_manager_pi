#!/usr/bin/env python3
"""Open GoPro capture-setting maps, validation and ordered application.

GoPro settings are interdependent: fps options depend on resolution, some FOVs
need 4K/5.3K, and the camera needs ~1s to reconfigure its video pipeline after a
resolution change. Applying them too fast or in incompatible combinations is
silently ignored or rejected -- so we validate first, then apply in order with
the delays the camera needs (mirrors the field-tested Jetson sequence).
"""
from __future__ import annotations

import time

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


def validate(camera_mode="", resolution="", fps="", fov="",
             hypersmooth="", wind_reduction="") -> str | None:
    """Return a human-readable error if the combination is invalid, else None.
    Empty fields are skipped (they will be left unchanged on the camera)."""
    tables = [("camera_mode", camera_mode, MODE_PRESET_GROUP),
              ("resolution", resolution, RESOLUTION), ("fps", fps, FPS),
              ("fov", fov, FOV), ("hypersmooth", hypersmooth, HYPERSMOOTH),
              ("wind_reduction", wind_reduction, WIND)]
    unknown = [name for name, val, table in tables if val and val not in table]
    if unknown:
        return f"unknown value for: {', '.join(unknown)} (check spelling/capitalization)"

    if camera_mode in ("", "Video", "Timelapse") and resolution and fps:
        f = int(fps)
        if resolution == "5.3K" and f > 60:
            return "5.3K is limited to 60fps max"
        if resolution == "4K" and f > 120:
            return "4K is limited to 120fps max"
    if fov == "HyperView" and resolution and resolution not in ("4K", "5.3K"):
        return "HyperView FOV needs 4K or 5.3K"
    return None


def apply_settings(cam: GoPro, camera_mode="", resolution="", fps="", fov="",
                   hypersmooth="", wind_reduction="") -> tuple[bool, str]:
    """Validate then apply the non-empty settings to one camera, in the order
    and with the delays the camera needs. Returns (ok, message)."""
    err = validate(camera_mode, resolution, fps, fov, hypersmooth, wind_reduction)
    if err:
        return False, err

    photo = (camera_mode == "Photo")
    failed: list[str] = []

    if camera_mode:
        if not cam.set_preset_group(MODE_PRESET_GROUP[camera_mode]):
            failed.append("mode")
        time.sleep(0.5)

    if resolution:
        if not cam.set_setting(SID_RESOLUTION, RESOLUTION[resolution]):
            failed.append("resolution")
        time.sleep(1.0)   # camera reconfigures its pipeline; later settings need this

    # fps / hypersmooth / wind do not exist in Photo mode.
    plan = [("fov", SID_FOV, FOV, fov)] if photo else [
        ("fps", SID_FPS, FPS, fps),
        ("fov", SID_FOV, FOV, fov),
        ("hypersmooth", SID_HYPERSMOOTH, HYPERSMOOTH, hypersmooth),
        ("wind_reduction", SID_WIND, WIND, wind_reduction),
    ]
    if photo and any([fps, hypersmooth, wind_reduction]):
        # Caller asked for video-only settings in Photo mode; just note it.
        pass
    for name, sid, table, val in plan:
        if not val:
            continue
        if not cam.set_setting(sid, table[val]):
            failed.append(name)
        time.sleep(0.4)     # GoPro needs to settle between interdependent settings;
        #                     applying them back-to-back causes spurious 403/500.

    if failed:
        return False, "applied with errors on: " + ", ".join(failed)
    return True, "ok"
