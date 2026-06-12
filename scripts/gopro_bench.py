#!/usr/bin/env python3
"""Benchmark the GoPro -> Pi transfer path and find the bottleneck.

Measures, per camera and across cameras:
  1. single-stream throughput (the cap of one HTTP connection),
  2. the USB link speed of each camera (480 = USB2 high-speed, 5000 = USB3),
  3. whether splitting ONE file into N parallel Range connections is faster,
  4. whether downloading both cameras at once adds throughput or hits a shared
     ceiling (proves the parallel-across-cameras strategy actually helps),
  5. context: lsusb -t and the Pi's SD write speed.

Reads are discarded (counted, not written) so tests isolate network/camera
throughput from the Pi's disk -- except the explicit dd write test. Each timed
download is capped (~30 MB) so the whole run is a couple of minutes.

  python3 gopro_bench.py            # default 30 MB cap, 4-way chunking
  CAP=50 CHUNKS=8 python3 gopro_bench.py
"""
import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.request import urlopen, Request

PORT = 8080
LABELS = ["LEFT", "RIGHT", "CAM2", "CAM3"]
CAP = int(os.environ.get("CAP", "30")) * 1_000_000     # bytes to transfer per timed test
CHUNKS = int(os.environ.get("CHUNKS", "4"))            # parallel connections for the split test


def sh(*a):
    return subprocess.run(list(a), capture_output=True, text=True).stdout


# --- topology ----------------------------------------------------------------
def discover():
    """[(label, cam_ip, iface), ...] sorted by IP (same labelling as the manager)."""
    res = []
    for line in sh("ip", "-4", "-o", "addr", "show").splitlines():
        m = re.search(r"^\d+:\s+(\S+)\s+inet\s+(172\.2[0-9]\.[0-9]+)\.[0-9]+", line)
        if m:
            res.append((m.group(1), m.group(2) + ".51"))
    res.sort(key=lambda t: t[1])
    return [(LABELS[i] if i < len(LABELS) else f"CAM{i}", ip, iface)
            for i, (iface, ip) in enumerate(res)]


def usb_speed(iface):
    """USB link speed (Mbps) for the camera's ethernet-gadget interface."""
    try:
        p = os.path.realpath(f"/sys/class/net/{iface}/device")
        for _ in range(8):
            sp = os.path.join(p, "speed")
            if os.path.isfile(sp):
                return open(sp).read().strip()
            p = os.path.dirname(p)
            if p in ("/", "/sys"):
                break
    except Exception:
        pass
    return "?"


def biggest_file(ip):
    """(dir, name, size) of the largest clip -- best signal for a throughput test."""
    d = json.loads(urlopen(f"http://{ip}:{PORT}/gopro/media/list", timeout=15).read())
    best = None
    for m in d.get("media", []):
        for f in m.get("fs", []):
            s = int(f.get("s", 0))
            if best is None or s > best[2]:
                best = (m["d"], f["n"], s)
    return best


# --- transfer primitives (discard data, just count + time) -------------------
def _url(ip, d, name):
    return f"http://{ip}:{PORT}/videos/DCIM/{d}/{name}"


def stream(ip, d, name, cap):
    """One plain GET, read up to `cap` bytes then stop. Returns (MB/s, bytes, s)."""
    t = time.time()
    n = 0
    with urlopen(_url(ip, d, name), timeout=180) as r:
        while n < cap:
            b = r.read(1 << 20)
            if not b:
                break
            n += len(b)
    dt = time.time() - t
    return (n / dt / 1e6 if dt else 0), n, dt


def _range(ip, d, name, start, end):
    req = Request(_url(ip, d, name), headers={"Range": f"bytes={start}-{end}"})
    n = 0
    with urlopen(req, timeout=180) as r:
        code = r.getcode()
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            n += len(b)
    return n, code


def range_supported(ip, d, name):
    req = Request(_url(ip, d, name), headers={"Range": "bytes=0-1048575"})
    try:
        with urlopen(req, timeout=10) as r:
            code = r.getcode()
            cr = r.headers.get("Content-Range")
            ar = r.headers.get("Accept-Ranges")
            r.read()
        return (code == 206 or bool(cr)), f"HTTP {code} Accept-Ranges={ar} Content-Range={cr}"
    except Exception as e:
        return False, f"error: {e}"


def chunked(ip, d, name, cap, chunks):
    """Download the first `cap` bytes via `chunks` parallel Range connections."""
    step = cap // chunks
    ranges = [(i * step, (cap - 1 if i == chunks - 1 else (i + 1) * step - 1)) for i in range(chunks)]
    t = time.time()
    with ThreadPoolExecutor(max_workers=chunks) as ex:
        res = list(ex.map(lambda rg: _range(ip, d, name, rg[0], rg[1])[0], ranges))
    dt = time.time() - t
    n = sum(res)
    return (n / dt / 1e6 if dt else 0), n, dt


# --- main --------------------------------------------------------------------
def main():
    cams = discover()
    if not cams:
        print("No GoPro on the USB bus.")
        return 1

    print(f"=== GoPro transfer benchmark  (cap={CAP//1_000_000} MB/test, chunks={CHUNKS}) ===\n")
    info = []
    print("cam    ip                iface           USB-link        test file")
    print("-----  ----------------  --------------  --------------  ----------------------")
    for label, ip, iface in cams:
        try:
            big = biggest_file(ip)
        except Exception as e:
            print(f"{label:<5}  {ip:<16}  {iface:<14}  unreachable ({e})")
            continue
        spd = usb_speed(iface)
        tag = {"480": "USB2 hi-speed", "5000": "USB3 SuperS.", "12": "USB1!"}.get(spd, f"{spd} Mbps")
        d, name, size = big
        print(f"{label:<5}  {ip:<16}  {iface:<14}  {tag:<14}  {name} ({size//1_000_000} MB)")
        info.append((label, ip, d, name, size, tag))
    print()

    # 1) single-stream per camera (sequential, so they don't contend)
    print("--- 1) single-stream throughput (1 connection) ---")
    single = {}
    for label, ip, d, name, size, tag in info:
        mbps, n, dt = stream(ip, d, name, min(CAP, size))
        single[label] = mbps
        print(f"  {label:<5} {mbps:6.1f} MB/s  ({mbps*8:5.0f} Mbps)   [{n//1_000_000} MB in {dt:.1f}s, {tag}]")

    # 2) range support + chunked single-file
    print(f"\n--- 2) split ONE file into {CHUNKS} parallel connections (Range) ---")
    for label, ip, d, name, size, tag in info:
        ok, detail = range_supported(ip, d, name)
        if not ok:
            print(f"  {label:<5} Range NOT supported -> can't split a file.  ({detail})")
            continue
        mbps, n, dt = chunked(ip, d, name, min(CAP, size), CHUNKS)
        base = single.get(label, 0)
        gain = f"{mbps/base:.1f}x vs 1-stream" if base else ""
        print(f"  {label:<5} {mbps:6.1f} MB/s  ({mbps*8:5.0f} Mbps)   {gain}   [{detail}]")

    # 3) both cameras at once -> does parallel add throughput?
    if len(info) >= 2:
        print("\n--- 3) all cameras in parallel (the download.sh strategy) ---")
        t0 = time.time()
        out = {}

        def work(label, ip, d, name, size):
            out[label] = stream(ip, d, name, min(CAP, size))

        with ThreadPoolExecutor(max_workers=len(info)) as ex:
            list(ex.map(lambda c: work(c[0], c[1], c[2], c[3], c[4]), info))
        wall = time.time() - t0
        agg = sum(v[1] for v in out.values()) / wall / 1e6
        for label, ip, d, name, size, tag in info:
            mbps, n, dt = out[label]
            solo = single.get(label, 0)
            drop = f"({mbps/solo*100:.0f}% of solo)" if solo else ""
            print(f"  {label:<5} {mbps:6.1f} MB/s under contention {drop}")
        sum_solo = sum(single.values())
        print(f"  AGGREGATE {agg:6.1f} MB/s   (sum of solo speeds was {sum_solo:.1f} MB/s)")
        print(f"  -> parallel {'HELPS' if agg > max(single.values())*1.3 else 'is capped (shared bottleneck)'}")

    # 4) context
    print("\n--- 4) context ---")
    print("lsusb -t:")
    print("  " + sh("lsusb", "-t").replace("\n", "\n  ").rstrip())
    dest = os.environ.get("GOPRO_DEST", os.path.expanduser("~/gopro_footage"))
    os.makedirs(dest, exist_ok=True)
    tw = os.path.join(dest, ".bench_write")
    err = subprocess.run(["dd", "if=/dev/zero", f"of={tw}", "bs=1M", "count=200", "oflag=direct"],
                         capture_output=True, text=True).stderr
    try:
        os.remove(tw)
    except OSError:
        pass
    wline = [l for l in err.splitlines() if "copied" in l or "s," in l]
    print(f"Pi SD write speed ({dest}): {wline[-1] if wline else err.strip()}")

    print("\nReading the result:")
    print("  * If chunked (test 2) >> single (test 1): the camera's HTTP server caps")
    print("    ONE connection -> splitting files speeds up even a single clip.")
    print("  * If aggregate (test 3) ~= sum of solos: parallel-across-cameras works.")
    print("    If it's capped near one camera's speed: shared bottleneck (USB host / Pi).")
    print("  * Compare USB3 (5000) vs USB2 (480) camera single-stream: if similar and")
    print("    both low, the cable type isn't the limit -- the GoPro HTTP server is.")
    print("  * If SD write speed < transfer speed, the Pi's card is the bottleneck.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
