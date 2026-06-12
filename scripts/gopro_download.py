#!/usr/bin/env python3
"""Offload recorded videos from the GoPros to the Pi (or any destination).

Cameras are downloaded IN PARALLEL (one stream per camera = one per USB bus),
so two cameras transfer at once instead of one-after-the-other. Within a single
camera, files are pulled sequentially (one read stream per SD card).

Each clip is saved as  <dest>/<CAMERA>/<UTC-timestamp>_<name>.MP4 . The camera
clock is synced (tzone=0) so the timestamp is real UTC -- segments of one
mission line up across both cameras and across a reboot. Re-runnable: a file
already present at the right size is skipped, so an interrupted run just resumes.

  ./download.sh                  all cameras -> ~/gopro_footage  (parallel)
  ./download.sh --pick           list the clips and ask which to copy
  ./download.sh --minsec 10      skip clips shorter than 10 s (queries duration)
  ./download.sh --all            include tiny (<2 MB) test clips too
  GOPRO_DEST=/mnt/usb ./download.sh

This is also importable (used by the pi_menu): see download_all() / discover().
"""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.request import urlopen

PORT = 8080
LABELS = ["LEFT", "RIGHT", "CAM2", "CAM3"]
DEFAULT_MINSIZE = 2_000_000          # bytes; skips 0 s test clips with no extra call


# --- camera discovery / HTTP -------------------------------------------------
def discover():
    """Return [(label, ip), ...] for each GoPro on the host's USB-ethernet buses
    (Open GoPro answers on .51 of each 172.2x subnet), labelled like the manager."""
    out = subprocess.run(["ip", "-4", "-o", "addr", "show"],
                         capture_output=True, text=True).stdout
    ips = sorted({re.sub(r"\.\d+$", ".51", m)
                  for m in re.findall(r"172\.2[0-9]\.[0-9]+\.[0-9]+", out)})
    return [(LABELS[i] if i < len(LABELS) else f"CAM{i}", ip) for i, ip in enumerate(ips)]


def _get(ip, path, timeout=15):
    with urlopen(f"http://{ip}:{PORT}{path}", timeout=timeout) as r:
        return r.read()


def media_list(ip):
    """Flat list of video files on one camera: dict(dir,name,size,cre)."""
    data = json.loads(_get(ip, "/gopro/media/list"))
    files = []
    for m in data.get("media", []):
        d = m["d"]
        for f in m.get("fs", []):
            files.append({"dir": d, "name": f["n"],
                          "size": int(f.get("s", 0)),
                          "cre": int(f.get("cre", f.get("mod", 0))),
                          "dur": None})
    return files


def fetch_duration(ip, f):
    """Fill f['dur'] (seconds) from /gopro/media/info -- one extra call per file."""
    try:
        info = json.loads(_get(ip, f"/gopro/media/info?path={f['dir']}/{f['name']}", timeout=8))
        f["dur"] = int(float(info.get("dur", 0)))
    except Exception:
        f["dur"] = None
    return f


# --- download ----------------------------------------------------------------
def _ts(epoch):
    return time.strftime("%Y%m%d_%H%M%S", time.gmtime(epoch)) if epoch else "nodate"


def _download_one(ip, label, f, dest, lock, c):
    name = f"{_ts(f['cre'])}_{f['name']}"
    outdir = os.path.join(dest, label)
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, name)
    if os.path.exists(out) and os.path.getsize(out) == f["size"]:
        with lock:
            print(f"  [{label}] have  {name}")
        return
    url = f"http://{ip}:{PORT}/videos/DCIM/{f['dir']}/{f['name']}"
    tmp = out + ".part"
    try:
        with urlopen(url, timeout=1800) as r, open(tmp, "wb") as o:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                o.write(chunk)
        if os.path.getsize(tmp) == f["size"]:
            os.replace(tmp, out)
            with lock:
                c["n"] += 1
                c["bytes"] += f["size"]
                print(f"  [{label}] OK    {name} ({f['size'] // 1_000_000} MB)")
        else:
            os.remove(tmp)
            with lock:
                c["fail"] += 1
                print(f"  [{label}] FAIL  {name} (size mismatch)")
    except Exception as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        with lock:
            c["fail"] += 1
            print(f"  [{label}] FAIL  {name}: {e}")


def download_all(jobs, dest, lock=None, counters=None):
    """jobs = [(label, ip, [file,...]), ...]. Cameras run in parallel (one worker
    each); files within a camera are sequential. Returns the counters dict."""
    lock = lock or threading.Lock()
    c = counters if counters is not None else {"n": 0, "bytes": 0, "fail": 0}

    def camera_worker(label, ip, files):
        for f in files:
            _download_one(ip, label, f, dest, lock, c)

    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as ex:
        for label, ip, files in jobs:
            ex.submit(camera_worker, label, ip, files)
    return c


# --- candidate gathering / filtering -----------------------------------------
def gather(cams, minsize, minsec):
    """Return [(label, ip, [file,...]), ...] after size/duration filtering.
    Duration is fetched (in parallel) only when minsec > 0."""
    jobs = []
    all_for_dur = []
    for label, ip in cams:
        try:
            files = media_list(ip)
        except Exception as e:
            print(f"  [{label}] {ip} unreachable -- skipped ({e})")
            continue
        files = [f for f in files if f["size"] >= minsize]
        if minsec > 0:
            all_for_dur += [(ip, f) for f in files]
        jobs.append([label, ip, files])

    if minsec > 0 and all_for_dur:        # fill durations in parallel, then filter
        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(lambda t: fetch_duration(*t), all_for_dur))
        for job in jobs:
            job[2] = [f for f in job[2] if (f["dur"] is None or f["dur"] >= minsec)]
    return [(lbl, ip, files) for lbl, ip, files in jobs if files]


# --- interactive selection (--pick) ------------------------------------------
def _parse_selection(s, n):
    s = s.strip().lower()
    if s in ("", "all", "tout", "*"):
        return set(range(n))
    keep = set()
    for part in re.split(r"[,\s]+", s):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            keep.update(range(int(a) - 1, int(b)))
        else:
            keep.add(int(part) - 1)
    return {i for i in keep if 0 <= i < n}


def pick(jobs):
    """Show a numbered table across all cameras and ask which to copy."""
    flat = [(lbl, ip, f) for lbl, ip, files in jobs for f in files]
    if not flat:
        return jobs
    print("\n  #   camera  date (UTC)        size     clip")
    print("  --  ------  ----------------  -------  --------------------")
    for i, (lbl, _ip, f) in enumerate(flat, 1):
        dur = f"{f['dur']}s" if f.get("dur") else ""
        print(f"  {i:>2}  {lbl:<6}  {_ts(f['cre'])}  {f['size'] // 1_000_000:>4} MB  {f['name']} {dur}")
    print()
    try:
        sel = input("  Which to copy? (e.g. 1-3,5  /  Enter or 'all' = everything): ")
    except EOFError:
        sel = "all"
    keep = _parse_selection(sel, len(flat))
    chosen = {}
    for i, (lbl, ip, f) in enumerate(flat):
        if i in keep:
            chosen.setdefault((lbl, ip), []).append(f)
    return [(lbl, ip, files) for (lbl, ip), files in chosen.items()]


# --- CLI ---------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Offload GoPro videos to the Pi (parallel).")
    ap.add_argument("--pick", action="store_true", help="list clips and choose which to copy")
    ap.add_argument("--all", action="store_true", help="include tiny (<2 MB) test clips")
    ap.add_argument("--minsec", type=int, default=0, help="skip clips shorter than N seconds")
    ap.add_argument("--minsize", type=int, default=None, help="skip clips smaller than N bytes")
    ap.add_argument("--dest", default=os.environ.get("GOPRO_DEST", os.path.expanduser("~/gopro_footage")))
    args = ap.parse_args()

    minsize = 0 if args.all else (args.minsize if args.minsize is not None else DEFAULT_MINSIZE)

    cams = discover()
    if not cams:
        print("No GoPro found on the USB bus.")
        return 1
    if (subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                       capture_output=True, text=True).stdout.split().count("gopro_manager")):
        print("(note: manager is running -- ./manager_down.sh first if a download stalls)")

    print(f">>> {len(cams)} camera(s): " + ", ".join(f"{l}({ip})" for l, ip in cams))
    jobs = gather(cams, minsize, args.minsec)
    if not jobs:
        print(">>> nothing to copy (after filtering).")
        return 0
    if args.pick:
        jobs = pick(jobs)
        if not jobs:
            print(">>> nothing selected.")
            return 0

    total = sum(len(f) for _, _, f in jobs)
    print(f">>> downloading {total} clip(s) from {len(jobs)} camera(s) in parallel -> {args.dest}")
    c = download_all(jobs, args.dest)
    print(f">>> done: {c['n']} file(s), {c['bytes'] // 1_000_000} MB -> {args.dest}   (failures: {c['fail']})")
    return 1 if c["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
