#!/usr/bin/env python3
"""Delete recorded media from the GoPros (Open GoPro wired HTTP API).

⚠️  DESTRUCTIVE and IRREVERSIBLE -- there is NO undo on the camera.

The Open GoPro HTTP API has no low-level FORMAT command. "Delete all" here
removes every media file one by one (endpoint /gopro/media/delete/file). The
card is left EMPTY but not FAT-reformatted -- for a true format use the camera's
own menu or a computer. For freeing space / starting a clean card, delete-all is
equivalent.

Nothing is deleted without an explicit flag AND a confirmation. By default this
just LISTS what is on the cards.

  ./gopro_delete.py                 list media on both cameras (no deletion)
  ./gopro_delete.py --pick          choose which clips to delete (per-clip)
  ./gopro_delete.py --all           delete ALL media on both cameras (type DELETE)
  ./gopro_delete.py --cam RIGHT     restrict to one camera (LEFT/RIGHT/CAM2..)
  ./gopro_delete.py --all --yes     skip the typed confirmation (scripts only)

A camera that is currently RECORDING is refused (never delete mid-take).
Reuses discovery / media listing from gopro_download.py.
"""
import argparse
import json
import sys
import time
from urllib.request import urlopen

import gopro_download as gd

PORT = 8080


# --- camera helpers ----------------------------------------------------------
def cam_status(ip):
    try:
        return json.loads(gd._get(ip, "/gopro/camera/state")).get("status", {})
    except Exception:
        return {}


def is_recording(ip):
    return str(cam_status(ip).get("10")) == "1"      # status 10 = encoding


def delete_file(ip, f, retries=3):
    """Delete one media file. Returns True on success. Retries + re-inits the
    camera (wired_usb) on failure, in case it slipped into 'USB Connected'."""
    url = f"http://{ip}:{PORT}/gopro/media/delete/file?path={f['dir']}/{f['name']}"
    last = None
    for _ in range(retries):
        try:
            with urlopen(url, timeout=15) as r:
                if 200 <= r.getcode() < 300:      # 200 OR 204 No Content (firmware-dependent) = deleted
                    return True
                last = f"HTTP {r.getcode()}"
        except Exception as e:
            last = e
            gd._reinit(ip)
            time.sleep(1)
    print(f"      ! failed to delete {f['name']}: {last}")
    return False


# --- gather / select ---------------------------------------------------------
def gather(cams):
    """[(label, ip, [file,...]), ...] -- ALL media (no size filter; deletion
    should see tiny test clips too). Skips unreachable cameras."""
    jobs = []
    for label, ip, _iface in cams:
        try:
            files = gd.media_list(ip)
        except Exception as e:
            print(f"  [{label}] {ip} unreachable -- skipped ({e})")
            continue
        jobs.append((label, ip, files))
    return jobs


def show(jobs):
    flat = [(lbl, ip, f) for lbl, ip, files in jobs for f in files]
    if not flat:
        print("  (no media on the card(s))")
        return flat
    print("\n  #   camera  date (UTC)        size     clip")
    print("  --  ------  ----------------  -------  --------------------")
    for i, (lbl, _ip, f) in enumerate(flat, 1):
        print(f"  {i:>2}  {lbl:<6}  {gd._ts(f['cre'])}  {f['size'] // 1_000_000:>4} MB  {f['name']}")
    total = sum(f["size"] for _, _, f in flat)
    print(f"\n  total: {len(flat)} clip(s), {total // 1_000_000} MB")
    return flat


def run_deletion(targets, yes, what):
    """targets = [(label, ip, file), ...]. Confirm, then delete. Returns counts."""
    if not targets:
        print(">>> nothing to delete.")
        return 0, 0
    n = len(targets)
    size = sum(f["size"] for _, _, f in targets) // 1_000_000
    cams = ", ".join(sorted({lbl for lbl, _, _ in targets}))
    print(f"\n⚠️  About to DELETE {n} clip(s), {size} MB from {cams} ({what}).")
    print("    This is IRREVERSIBLE -- there is no undo on the camera.")
    if not yes:
        prompt = "Type DELETE to confirm: " if what == "ALL media" else "Delete these? type 'yes': "
        want = "DELETE" if what == "ALL media" else "yes"
        try:
            ans = input(f"    {prompt}")
        except EOFError:
            ans = ""
        if ans.strip() != want:
            print(">>> aborted (nothing deleted).")
            return 0, 0

    ok = fail = 0
    freed = 0
    tty = sys.stdout.isatty()
    for i, (lbl, ip, f) in enumerate(targets, 1):
        if delete_file(ip, f):
            ok += 1
            freed += f["size"]
        else:
            fail += 1
        pct = i / n * 100
        filled = int(pct / 5)
        bar = "#" * filled + "-" * (20 - filled)
        line = f">>> [{bar}] {pct:4.0f}%  deleting {i}/{n}  {freed // 1_000_000} MB freed"
        if tty:
            sys.stdout.write("\r\033[K" + line)
            sys.stdout.flush()
    if tty:
        sys.stdout.write("\n")
        sys.stdout.flush()
    print(f">>> deleted {ok} clip(s), {fail} failure(s).")
    return ok, fail


# --- CLI ---------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Delete GoPro media (DESTRUCTIVE).")
    ap.add_argument("--pick", action="store_true", help="choose which clips to delete")
    ap.add_argument("--all", action="store_true", help="delete ALL media on the card(s)")
    ap.add_argument("--cam", help="restrict to one camera label (LEFT/RIGHT/CAM2..)")
    ap.add_argument("--yes", action="store_true", help="skip the typed confirmation (scripts only)")
    args = ap.parse_args()

    cams = gd.discover()
    if args.cam:
        cams = [c for c in cams if c[0].upper() == args.cam.upper()]
        if not cams:
            print(f"No camera labelled {args.cam!r}.")
            return 1
    if not cams:
        print("No GoPro found on the USB bus.")
        return 1

    # never delete on a camera that is recording
    live = [c for c in cams if is_recording(c[1])]
    if live:
        print("⚠️  REFUSING: these cameras are RECORDING -- stop the take first: "
              + ", ".join(f"{l}({ip})" for l, ip, _ in live))
        return 1

    jobs = gather(cams)
    flat = show(jobs)
    if not flat:
        return 0

    if not (args.pick or args.all):
        print("\n(listing only -- pass --pick to choose, or --all to wipe everything)")
        return 0

    if args.all:
        targets = flat
        what = "ALL media"
    else:                                    # --pick
        try:
            sel = input("\n  Which to DELETE? (e.g. 1-3,5  /  'all'  /  q = annuler) : ")
        except EOFError:
            sel = ""
        if sel.strip().lower() in ("", "q", "quit", "cancel", "annuler"):
            print(">>> annulé (rien supprimé).")
            return 0
        keep = gd._parse_selection(sel, len(flat))
        targets = [flat[i] for i in sorted(keep)]
        what = "selected clips"

    ok, fail = run_deletion(targets, args.yes, what)

    if ok:
        remaining = sum(len(gd.media_list(ip)) for _, ip, _ in cams)
        print(f">>> remaining on card(s): {remaining} clip(s).")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
