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

from .camera import GoPro, discover


def _find(cams: list[GoPro], label: str) -> GoPro | None:
    for c in cams:
        if c.label == label or c.ip == label or c.iface == label:
            return c
    return None


def cmd_discover(cams: list[GoPro]) -> int:
    if not cams:
        print("Aucune GoPro trouvée.")
        return 1
    for c in cams:
        print(f"  {c.label:6} ip={c.ip:16} power={'oui' if c.can_power_cycle() else 'NON'}"
              f" ({c.hub}/p{c.port})  iface={c.iface}")
    return 0


def cmd_status(cams: list[GoPro]) -> int:
    sd = {0: "OK", 1: "PLEINE", 2: "ABSENTE", 3: "A-FORMATER", 4: "BUSY"}
    for c in cams:
        st = c.state()
        if st is None:
            print(f"  {c.label:6} INJOIGNABLE")
            continue
        space_gb = (st.get("54") or 0) / 1e6
        print(f"  {c.label:6} batt={st.get('1')} sd={sd.get(st.get('33'),'?')}"
              f" libre={space_gb:.0f}GB encoding={st.get('10')} busy={st.get('8')}")
    return 0


def cmd_record(cams: list[GoPro], secs: float) -> int:
    print("Init…")
    for c in cams:
        ok = c.init()
        print(f"  {c.label}: init {'OK' if ok else 'ECHEC'}")
    print(f"START simultané ({secs}s)…")
    results = {c.label: c.start() for c in cams}
    for label, ok in results.items():
        print(f"  {label}: start {'OK' if ok else 'ECHEC (500?)'}")
    time.sleep(secs)
    enc = {c.label: c.encoding() for c in cams}
    print(f"  encoding pendant: {enc}")
    print("STOP…")
    for c in cams:
        c.stop()
    return 0 if all(results.values()) else 1


def cmd_cycle(cams: list[GoPro], label: str) -> int:
    c = _find(cams, label)
    if not c:
        print(f"Caméra '{label}' introuvable.")
        return 1
    if not c.can_power_cycle():
        print(f"{c.label}: pas de coupure d'alim possible (hub/port non résolu).")
        return 1
    print(f"Power-cycle Vbus de {c.label} ({c.hub}/p{c.port})…")
    ok = c.power_cycle()
    print(f"  -> {'revenue' if ok else 'TOUJOURS ABSENTE'} après reboot")
    return 0 if ok else 1


def cmd_recover(cams: list[GoPro], label: str) -> int:
    c = _find(cams, label)
    if not c:
        print(f"Caméra '{label}' introuvable.")
        return 1
    print(f"Recovery de {c.label}…")
    ok = c.recover()
    print(f"  -> {'OK (enregistre de nouveau)' if ok else 'ECHEC'}")
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
    print(f"Commande inconnue: {cmd}")
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
