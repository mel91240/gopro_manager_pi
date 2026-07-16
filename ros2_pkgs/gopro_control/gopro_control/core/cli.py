#!/usr/bin/env python3
"""Live test CLI for the GoPro core. Run on the Pi that hosts the cameras.

Examples:
  python3 -m gopro_control.core.cli discover
  python3 -m gopro_control.core.cli status
  python3 -m gopro_control.core.cli record 5      # record 5s on all cameras at once
  python3 -m gopro_control.core.cli cycle LEFT     # Vbus power-cycle one camera
  python3 -m gopro_control.core.cli recover RIGHT  # power-cycle + re-init + verify
"""
from __future__ import annotations

import sys
import time

from .camera import (GoPro, discover, ST_BATTERY, ST_BUSY, ST_ENCODING,
                     ST_SD, ST_SPACE_KB)


def _find(cams: list[GoPro], label: str) -> GoPro | None:
    for c in cams:
        if c.label == label or c.ip == label or c.iface == label:
            return c
    return None


def cmd_discover(cams: list[GoPro]) -> int:
    if not cams:
        print("No GoPro found.")
        return 1
    for c in cams:
        print(f"  {c.label:6} ip={c.ip:16} power={'yes' if c.can_power_cycle() else 'NO'}"
              f" ({c.hub}/p{c.port})  iface={c.iface}")
    return 0


def cmd_status(cams: list[GoPro]) -> int:
    sd = {0: "OK", 1: "FULL", 2: "MISSING", 3: "NEEDS-FORMAT", 4: "BUSY"}
    for c in cams:
        st = c.state()
        if st is None:
            print(f"  {c.label:6} UNREACHABLE")
            continue
        space_gb = (st.get(ST_SPACE_KB) or 0) / 1e6
        print(f"  {c.label:6} batt={st.get(ST_BATTERY)} sd={sd.get(st.get(ST_SD),'?')}"
              f" free={space_gb:.0f}GB encoding={st.get(ST_ENCODING)} busy={st.get(ST_BUSY)}")
    return 0


def cmd_record(cams: list[GoPro], secs: float) -> int:
    print("Init...")
    for c in cams:
        ok = c.init()
        print(f"  {c.label}: init {'OK' if ok else 'FAILED'}")
    print(f"Simultaneous START ({secs}s)...")
    results = {c.label: c.start() for c in cams}
    for label, ok in results.items():
        print(f"  {label}: start {'OK' if ok else 'FAILED (500?)'}")
    time.sleep(secs)
    enc = {c.label: c.encoding() for c in cams}
    print(f"  encoding during: {enc}")
    print("STOP...")
    for c in cams:
        c.stop()
    return 0 if all(results.values()) else 1


def cmd_cycle(cams: list[GoPro], label: str) -> int:
    c = _find(cams, label)
    if not c:
        print(f"Camera '{label}' not found.")
        return 1
    if not c.can_power_cycle():
        print(f"{c.label}: no power-cut available (hub/port not resolved).")
        return 1
    print(f"Vbus power-cycle of {c.label} ({c.hub}/p{c.port})...")
    ok = c.power_cycle()
    print(f"  -> {'came back' if ok else 'STILL MISSING'} after reboot")
    return 0 if ok else 1


def cmd_recover(cams: list[GoPro], label: str) -> int:
    c = _find(cams, label)
    if not c:
        print(f"Camera '{label}' not found.")
        return 1
    print(f"Recovery of {c.label}...")
    ok = c.recover()
    print(f"  -> {'OK (recording again)' if ok else 'FAILED'}")
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    cams = discover(labels=["LEFT", "RIGHT"])
    if cmd == "discover":
        return cmd_discover(cams)
    if cmd == "status":
        return cmd_status(cams)
    if cmd == "record":
        return cmd_record(cams, float(rest[0]) if rest else 5.0)
    if cmd == "cycle":
        return cmd_cycle(cams, rest[0]) if rest else 1
    if cmd == "recover":
        return cmd_recover(cams, rest[0]) if rest else 1
    print(f"Unknown command: {cmd}")
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
