#!/usr/bin/env python3
"""Offload recorded videos from the GoPros to the Pi (or any destination).

ROBUST by design -- this rig's cameras occasionally reboot or drop into the
"USB Connected" file-transfer mode mid-copy (the :8080 server vanishes). So every
file is downloaded with:
  * a SHORT per-read socket timeout (a stalled camera is detected in seconds,
    not minutes),
  * automatic RETRY per file,
  * a re-init of the camera (wired_usb?p=1) on failure, which pulls it back out
    of "USB Connected" mode,
  * byte-level RESUME via HTTP Range: an interrupted file keeps its <name>.part
    and continues where it left off (across retries AND across whole re-runs).

PARALLEL across cameras: benchmarking (gopro_bench.py) showed that when BOTH
cameras sit on USB3 the two downloads run at full speed at once (~77 MB/s
aggregate). But with a mixed USB2+USB3 wiring the USB2 camera collapses to
~1.4 MB/s under contention. So the default is AUTO: parallel only when every
camera reports a USB3 (5000 Mbps) link, otherwise sequential. Override with
--parallel / --sequential. (One connection per file: on this rig two
USB2-class cameras already pulled in parallel are at the ceiling -- splitting a
file into more connections only adds bus contention, measured slower.)

Each clip is saved as  <dest>/<CAMERA>/<UTC-timestamp>_<name>.MP4 . The camera
clock is set by the operator to match the Pi/navigation clock, so the timestamp
lines clips up across both cameras and across a reboot. A file already present at
the right size is skipped -> an interrupted run just resumes.

  ./download.sh                  all cameras -> ~/gopro_footage  (auto par/seq)
  ./download.sh --parallel       force both cameras at once
  ./download.sh --sequential     force one camera at a time
  ./download.sh --pick           list the clips and ask which to copy
  ./download.sh --minsec 10      skip clips shorter than 10 s (queries duration)
  ./download.sh --all            include tiny (<2 MB) test clips too
  GOPRO_DEST=/mnt/ssd ./download.sh

Importable by the pi_menu: see discover() / gather() / download_all().
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
from urllib.request import urlopen, Request

PORT = 8080
LABELS = ["LEFT", "RIGHT", "CAM2", "CAM3"]
DEFAULT_MINSIZE = 2_000_000          # bytes; skips 0 s test clips with no extra call

READ_TIMEOUT = 20                    # s; a per-read stall longer than this -> retry
RETRIES = 6                          # attempts per file before giving up
BACKOFF = 2                          # s; pause after a failure (the camera re-init settles)
REINIT_MIN_GAP = 3                   # s; don't re-init the same camera more often than this
_LIMITER = None                      # optional aggregate byte-rate cap (set from --maxrate).
                                     # USB3 RFI scales with bus activity; capping total throughput
                                     # keeps the noise under the level that drops the 2.4GHz WiFi.
SYNC_EVERY = 128 << 20               # BYTES BETWEEN fsyncs -- NOT a rate cap. Throughput stays
                                     # at full speed (~70 MB/s); this only bounds how many dirty
                                     # pages pile up. On a Pi 4 writing tens of GB to a USB disk,
                                     # caching the whole transfer grows dirty pages faster than
                                     # the disk drains them -> the global dirty throttle freezes
                                     # ALL userspace (even sshd over ethernet stops answering).
                                     # Flushing every 128 MB caps that backlog while keeping the
                                     # fsync overhead negligible. Tune after measuring with btop.


# --- camera discovery / HTTP -------------------------------------------------
def discover():
    """Return [(label, ip, iface), ...] for each GoPro on the host's USB-ethernet
    buses (Open GoPro answers on .51 of each 172.2x subnet), labelled like the
    manager. iface lets us read the USB link speed to auto-pick parallel/seq."""
    out = subprocess.run(["ip", "-4", "-o", "addr", "show"],
                         capture_output=True, text=True).stdout
    found = {}
    for line in out.splitlines():
        m = re.search(r"^\d+:\s+(\S+)\s+inet\s+(172\.2[0-9]\.[0-9]+)\.[0-9]+", line)
        if m:
            found[m.group(2) + ".51"] = m.group(1)        # cam_ip -> iface
    cams = []
    for i, ip in enumerate(sorted(found)):
        cams.append((LABELS[i] if i < len(LABELS) else f"CAM{i}", ip, found[ip]))
    return cams


def usb_speed(iface):
    """USB link speed in Mbps for a camera's ethernet-gadget iface (480=USB2,
    5000=USB3), or None if it can't be read."""
    try:
        p = os.path.realpath(f"/sys/class/net/{iface}/device")
        for _ in range(8):
            sp = os.path.join(p, "speed")
            if os.path.isfile(sp):
                return int(open(sp).read().strip())
            p = os.path.dirname(p)
            if p in ("/", "/sys"):
                break
    except Exception:
        pass
    return None


def auto_parallel(cams):
    """True only when EVERY camera is on a USB3 (>=5000 Mbps) link -- the one
    case the benchmark proved parallel helps instead of starving a USB2 cam."""
    speeds = [usb_speed(iface) for _, _, iface in cams]
    return bool(speeds) and all(s is not None and s >= 5000 for s in speeds)


def _get(ip, path, timeout=15):
    with urlopen(f"http://{ip}:{PORT}{path}", timeout=timeout) as r:
        return r.read()


def media_list(ip):
    """Flat list of video files on one camera: dict(dir,name,size,cre,dur)."""
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


# --- robust download ---------------------------------------------------------
_reinit_state = {}                   # ip -> last reinit monotonic time
_reinit_lock = threading.Lock()


def _reinit(ip):
    """Pull a camera back into Open GoPro wired-control mode (out of "USB
    Connected"). Throttled so concurrent camera threads don't storm it."""
    with _reinit_lock:
        last = _reinit_state.get(ip, 0)
        now = time.monotonic()
        if now - last < REINIT_MIN_GAP:
            return
        _reinit_state[ip] = now
    try:
        _get(ip, "/gopro/camera/control/wired_usb?p=1", timeout=8)
    except Exception:
        pass


def _url(ip, f):
    return f"http://{ip}:{PORT}/videos/DCIM/{f['dir']}/{f['name']}"


def _pull_range(ip, f, end, part, target, progress=None):
    """Download file f into `part`, resuming from whatever it already holds via
    an HTTP Range request. Returns once `part` reaches `target` bytes; raises on
    any network/stall error (caught by the retry loop). Each read is reported to
    `progress` for the live status line."""
    have = os.path.getsize(part) if os.path.exists(part) else 0
    if have >= target:
        return
    req = Request(_url(ip, f), headers={"Range": f"bytes={have}-{end}"})
    r = urlopen(req, timeout=READ_TIMEOUT)
    try:
        code = r.getcode()
        if have and code != 206:                  # server ignored our resume offset
            if os.path.exists(part):
                os.remove(part)
            raise IOError(f"range not honored (HTTP {code}); restarting file")
        with open(part, "ab" if (have and code == 206) else "wb") as o:
            since_sync = 0
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                o.write(b)
                since_sync += len(b)
                if since_sync >= SYNC_EVERY:       # flush to disk so dirty pages stay bounded
                    o.flush()
                    os.fsync(o.fileno())           # -> Pi stays responsive (sshd survives)
                    since_sync = 0
                if progress is not None:
                    progress.add(len(b))
                if _LIMITER is not None:
                    _LIMITER.throttle(len(b))       # pace total throughput (WiFi-safe rate)
    finally:
        r.close()


def _retrying(ip, fn):
    """Run fn() with RETRIES attempts; re-init the camera + back off on failure."""
    last = None
    for _ in range(RETRIES):
        try:
            fn()
            return
        except Exception as e:
            last = e
            _reinit(ip)
            time.sleep(BACKOFF)
    raise last if last else IOError("download failed")


# --- live progress (one self-updating line: % / GB / MB/s / ETA / files) ------
_TTY = sys.stdout.isatty()
_print_lock = threading.Lock()


def _emit(msg):
    """Print a permanent line without clobbering the live progress line."""
    with _print_lock:
        sys.stdout.write(("\r\033[K" if _TTY else "") + msg + "\n")
        sys.stdout.flush()


def _fmt_eta(sec):
    """Human time-remaining: 4h05 / 12min30 / 45s (readable even for a 1 TB copy)."""
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}"
    if m:
        return f"{m}min{s:02d}"
    return f"{s}s"


class RateLimiter:
    """Aggregate byte-rate cap shared across all camera threads. Paces the total
    transfer to <= max_bytes_per_sec so the USB3 bus activity (and thus its 2.4GHz
    RFI) stays below the level that knocks the Pi off WiFi. Slowing the copy is the
    price for keeping the wireless link alive without extra hardware."""

    def __init__(self, max_bytes_per_sec):
        self.rate = max_bytes_per_sec
        self.lock = threading.Lock()
        self.t0 = time.monotonic()
        self.sent = 0

    def throttle(self, n):
        if self.rate <= 0:
            return
        with self.lock:
            self.sent += n
            target = self.t0 + self.sent / self.rate
        delay = target - time.monotonic()
        if delay > 0:
            time.sleep(delay)


class Progress:
    """Shared, thread-safe transfer progress for the live status line."""

    def __init__(self, total_bytes, total_files):
        self.total = total_bytes
        self.total_files = total_files
        self.done = 0
        self.files_done = 0
        self.lock = threading.Lock()
        self.t0 = time.monotonic()
        self.stop = False

    def add(self, n):
        with self.lock:
            self.done += n

    def file_done(self):
        with self.lock:
            self.files_done += 1

    def render(self):
        with self.lock:
            done, total = self.done, self.total
            fd, ft = self.files_done, self.total_files
        el = time.monotonic() - self.t0
        spd = done / el / 1e6 if el > 0 else 0.0
        pct = done / total * 100 if total else 100.0
        eta = (total - done) / (done / el) if (done > 0 and el > 0 and total > done) else 0
        filled = int(pct / 5)
        bar = "#" * filled + "-" * (20 - filled)
        return (f">>> [{bar}] {pct:4.1f}%  {done/1e9:.2f}/{total/1e9:.2f} GB  "
                f"{spd:5.1f} MB/s  restant {_fmt_eta(eta):>7}  files {fd}/{ft}")


def _reporter(progress):
    """Redraw the live status line ~3x/s. The bar/%/GB advance with every chunk
    read, so a stable speed/ETA is normal -- but a genuinely STALLED camera (a
    drop into "USB Connected" while the retry loop re-inits it) freezes the bytes
    for up to READ_TIMEOUT seconds. Flag that explicitly so a stall reads as a
    stall, not as a hang of the tool itself."""
    last_done = -1
    last_move = time.monotonic()
    while not progress.stop:
        with progress.lock:
            cur = progress.done
        now = time.monotonic()
        if cur != last_done:
            last_done, last_move = cur, now
        line = progress.render()
        stalled = now - last_move
        if stalled > 3:
            line += f"  [stalled {int(stalled)}s -- recovering camera]"
        with _print_lock:
            sys.stdout.write("\r\033[K" + line)
            sys.stdout.flush()
        time.sleep(0.3)


def _download_one(ip, label, f, dest, lock, c, progress=None):
    name = f"{_ts(f['cre'])}_{f['name']}"
    outdir = os.path.join(dest, label)
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, name)
    size = f["size"]
    if os.path.exists(out) and os.path.getsize(out) == size:
        if progress is not None:
            progress.add(size)
            progress.file_done()
        return
    tmp = out + ".part"
    try:
        # one resumable connection per file (robust; resumes a flaky cam). On this
        # rig -- two USB2-class cameras already pulled in parallel -- splitting a
        # file into more connections only adds bus contention (measured slower).
        _retrying(ip, lambda: _pull_range(ip, f, size - 1, tmp, size, progress))

        if os.path.getsize(tmp) == size:
            os.replace(tmp, out)
            with lock:
                c["n"] += 1
                c["bytes"] += size
            if progress is not None:
                progress.file_done()
        else:
            with lock:
                c["fail"] += 1
            _emit(f"  [{label}] FAIL  {name} (size mismatch; .part kept for resume)")
    except Exception as e:
        with lock:
            c["fail"] += 1
        _emit(f"  [{label}] FAIL  {name}: {e}  (.part kept for resume)")


def download_all(jobs, dest, lock=None, counters=None, parallel=False):
    """jobs = [(label, ip, [file,...]), ...]. Returns the counters dict.

    parallel: download cameras at once (use only when all cameras are USB3 --
    see auto_parallel())."""
    lock = lock or threading.Lock()
    c = counters if counters is not None else {"n": 0, "bytes": 0, "fail": 0}

    total_bytes = sum(f["size"] for _, _, files in jobs for f in files)
    total_files = sum(len(files) for _, _, files in jobs)
    progress = Progress(total_bytes, total_files)
    reporter = None
    if _TTY:                              # live status line only makes sense on a terminal
        reporter = threading.Thread(target=_reporter, args=(progress,), daemon=True)
        reporter.start()

    def camera_worker(label, ip, files):
        for f in files:
            _download_one(ip, label, f, dest, lock, c, progress)

    try:
        if parallel and len(jobs) > 1:
            with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
                list(ex.map(lambda j: camera_worker(*j), jobs))
        else:
            for label, ip, files in jobs:
                camera_worker(label, ip, files)
    finally:
        progress.stop = True
        if reporter is not None:
            reporter.join(timeout=1.0)
            with _print_lock:
                sys.stdout.write("\r\033[K")
                sys.stdout.flush()
    return c


def _ts(epoch):
    return time.strftime("%Y%m%d_%H%M%S", time.gmtime(epoch)) if epoch else "nodate"


# --- candidate gathering / filtering -----------------------------------------
def gather(cams, minsize, minsec):
    """Return [(label, ip, [file,...]), ...] after size/duration filtering.
    cams = [(label, ip, iface), ...]. Duration is fetched only when minsec > 0."""
    jobs = []
    all_for_dur = []
    for label, ip, _iface in cams:
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
        sel = input("  Which to copy? (e.g. 1-3,5  /  'all'  /  q = annuler) : ")
    except EOFError:
        sel = "all"
    if sel.strip().lower() in ("q", "quit", "cancel", "annuler"):
        print(">>> annulé (rien copié).")
        return []
    keep = _parse_selection(sel, len(flat))
    chosen = {}
    for i, (lbl, ip, f) in enumerate(flat):
        if i in keep:
            chosen.setdefault((lbl, ip), []).append(f)
    return [(lbl, ip, files) for (lbl, ip), files in chosen.items()]


# --- CLI ---------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Offload GoPro videos (robust + resumable).")
    ap.add_argument("--pick", action="store_true", help="list clips and choose which to copy")
    ap.add_argument("--parallel", action="store_true", help="force both cameras at once")
    ap.add_argument("--sequential", action="store_true", help="force one camera at a time")
    ap.add_argument("--all", action="store_true", help="include tiny (<2 MB) test clips")
    ap.add_argument("--minsec", type=int, default=0, help="skip clips shorter than N seconds")
    ap.add_argument("--minsize", type=int, default=None, help="skip clips smaller than N bytes")
    ap.add_argument("--maxrate", type=float, default=0, help="cap total throughput to N MB/s "
                    "(0=unlimited; lower it to keep the 2.4GHz WiFi alive during the copy)")
    ap.add_argument("--dest", default=os.environ.get("GOPRO_DEST", os.path.expanduser("~/gopro_footage")))
    args = ap.parse_args()

    global _LIMITER
    if args.maxrate > 0:
        _LIMITER = RateLimiter(args.maxrate * 1e6)

    minsize = 0 if args.all else (args.minsize if args.minsize is not None else DEFAULT_MINSIZE)

    cams = discover()
    if not cams:
        print("No GoPro found on the USB bus.")
        return 1

    if args.parallel:
        parallel = True
    elif args.sequential:
        parallel = False
    else:
        parallel = auto_parallel(cams)

    links = ", ".join(f"{l}({ip},{usb_speed(ifc) or '?'}M)" for l, ip, ifc in cams)
    print(f">>> {len(cams)} camera(s): {links}")
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
    mode = "parallel" if parallel else "sequential"
    if not (args.parallel or args.sequential):
        mode += " (auto)"
    if args.maxrate > 0:
        mode += f", capped {args.maxrate:g} MB/s"
    print(f">>> downloading {total} clip(s) from {len(jobs)} camera(s) [{mode}] -> {args.dest}")
    t0 = time.time()
    c = download_all(jobs, args.dest, parallel=parallel)
    dt = time.time() - t0
    rate = (c["bytes"] / dt / 1e6) if dt else 0
    print(f">>> done: {c['n']} file(s), {c['bytes'] // 1_000_000} MB in {_fmt_eta(dt)} "
          f"({rate:.1f} MB/s) -> {args.dest}   (failures: {c['fail']})")
    return 1 if c["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
