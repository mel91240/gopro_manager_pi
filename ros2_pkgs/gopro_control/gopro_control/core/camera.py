#!/usr/bin/env python3
"""ROS-independent control of GoPro Hero cameras over wired USB (Open GoPro)
plus per-port Vbus switching via uhubctl — no MOSFET / GPIO needed.

Designed for a Raspberry Pi driving N GoPros through a powered USB hub that
supports PPPS (per-port power switching), e.g. VIA Labs based hubs.

Depends only on the Python standard library (urllib) so it runs unmodified
inside the vehicle's ROS 2 container.

Validated live on a Pi with 2x Hero 12 Black (2026-06-10):
  - Hero 12 runs and records with NO battery, powered over USB.
  - The hub really cuts Vbus on `uhubctl ... -a cycle` (camera reboots ~15s),
    which is the only reliable cure for a "zombie" camera (API answers /state
    with 200 but /shutter/start with 500).
"""
from __future__ import annotations

import concurrent.futures
import datetime
import glob
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request

UHUBCTL = "/usr/sbin/uhubctl"
HTTP_PORT = 8080

# Open GoPro status field IDs (from /gopro/camera/state -> "status")
ST_BUSY = "8"
ST_ENCODING = "10"
ST_SD = "33"            # 0=OK 1=full 2=removed 3=needs-format 4=busy
ST_REMAINING_PHOTOS = "34"
ST_REMAINING_SEC = "35"  # remaining video seconds -- the RELIABLE "card usable" signal

PRESET_GROUP_VIDEO = 1000
AUTO_POWER_OFF = 59          # Open GoPro setting id (Hero 12): Auto Power Off
AUTO_POWER_OFF_NEVER = 0     # option 0 = Never (verified: camera reads setting 59 = 0 when set to "Never")
MODE_PRESET_GROUP = {"Video": 1000, "Photo": 1001, "Timelapse": 1002}

# Open GoPro wired host IPs look like 172.2X.1YY.5Z ; camera is always .51.
_GOPRO_SUBNET_RE = re.compile(r"^172\.2[0-9]\.[0-9]+\.[0-9]+$")


def _ipv4_of(iface: str) -> str | None:
    try:
        out = subprocess.check_output(
            ["ip", "-4", "-o", "addr", "show", iface],
            text=True, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
    return m.group(1) if m else None


def _usb_hub_port(iface: str):
    """Map a USB-ethernet interface to its (uhubctl_location, port) so we can
    cut its Vbus. Device node '1-1.2.1' -> ('1-1.2', '1'); '2-2.3' -> ('2-2','3').
    Returns (None, None) if it can't be resolved (then power-cycle is disabled)."""
    link = f"/sys/class/net/{iface}/device"
    if not os.path.exists(link):
        return None, None
    real = os.path.realpath(link)                    # .../1-1.2.1/1-1.2.1:1.0
    dev = os.path.basename(os.path.dirname(real))    # 1-1.2.1
    if "." not in dev:
        return None, None
    hub, port = dev.rsplit(".", 1)
    return hub, port


class GoPro:
    """One wired GoPro. Identified by its (stable) camera IP and hub port;
    the host interface name (eth1/eth2/...) is NOT stable across reboots, so
    everything keys off the camera IP and the physical hub/port instead."""

    def __init__(self, label: str, ip: str, hub: str | None,
                 port: str | None, iface: str | None = None):
        self.label = label          # human label, e.g. "LEFT" / "A"
        self.ip = ip                # camera IP, e.g. 172.24.163.51
        self.hub = hub              # uhubctl -l location, e.g. 1-1.2
        self.port = port            # uhubctl -p port, e.g. 1
        self.iface = iface          # last-seen host iface (informational)

    # --- low level ---------------------------------------------------------
    def _request(self, path: str, timeout: float = 5.0):
        """GET an Open GoPro endpoint. Returns (status_code, json).
        status_code is the HTTP code (200, 500, ...) or None if unreachable;
        json is the parsed body, {} when empty, or None on error."""
        url = f"http://{self.ip}:{HTTP_PORT}{path}"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                body = resp.read()
                code = resp.status
        except urllib.error.HTTPError as e:
            return e.code, None
        except (urllib.error.URLError, socket.timeout, OSError):
            return None, None
        try:
            return code, (json.loads(body) if body else {})
        except ValueError:
            return code, None

    def reachable(self, timeout: float = 2.0) -> bool:
        code, _ = self._request("/gopro/version", timeout=timeout)
        return code == 200

    def state(self, timeout: float = 5.0) -> dict | None:
        code, data = self._request("/gopro/camera/state", timeout=timeout)
        if code == 200 and data:
            return data.get("status", {})
        return None

    # --- setup / recording -------------------------------------------------
    def init(self) -> bool:
        """Arm wired control and select video mode. Must run before recording,
        otherwise shutter/start returns HTTP 500 (camera still in MTP mode)."""
        wired_code, _ = self._request("/gopro/camera/control/wired_usb?p=1", timeout=4)
        time.sleep(1.5)
        # Disable idle auto-power-off (setting 59 = Never). The cameras run on USB
        # with NO battery, so this reverts to a few minutes after any power loss;
        # re-assert it on every arm so an idle camera never sleeps into a
        # capture-dead state (answers /state 200 but /shutter 500) mid-mission.
        # Best-effort: it is re-enforced continuously by the manager, so a transient
        # failure here must NOT make a camera that IS in wired video mode look unarmed.
        self.set_setting(AUTO_POWER_OFF, AUTO_POWER_OFF_NEVER)
        time.sleep(0.3)
        preset_ok = self.set_preset_group(PRESET_GROUP_VIDEO)
        # init() must reflect the wired-control switch too: if wired_usb did not
        # return 200 the camera is still in MTP mode and shutter/start will 500, so
        # report failure instead of a false 'armed' from a lucky preset call.
        return preset_ok and wired_code == 200

    def enable_wired_control(self) -> None:
        """Re-assert wired USB control without touching the preset/settings. A
        camera silently falls back to MTP mode after sitting idle or re-enumerating,
        which makes shutter/start return HTTP 500 -> a spurious 'FAILED to start';
        calling this right before a (re)start avoids it. Lighter than init()."""
        self._request("/gopro/camera/control/wired_usb?p=1", timeout=4)

    def set_preset_group(self, group_id: int) -> bool:
        """Select a preset group (1000=Video, 1001=Photo, 1002=Timelapse)."""
        code, _ = self._request(
            f"/gopro/camera/presets/set_group?id={group_id}", timeout=4)
        return code == 200

    def start(self) -> bool:
        code, _ = self._request("/gopro/camera/shutter/start", timeout=4)
        return code == 200

    def stop(self) -> bool:
        code, _ = self._request("/gopro/camera/shutter/stop", timeout=4)
        return code == 200

    def encoding(self) -> bool:
        st = self.state()
        return bool(st and st.get(ST_ENCODING) == 1)

    def recording_now(self, retries: int = 4, delay: float = 0.6) -> bool:
        """True if the camera is currently encoding, robust to a transient first
        failure. Right after the manager (re)starts, the camera's HTTP server can
        refuse the very first connection -- so we retry while it is unreachable
        before concluding it is idle. This guards the "adopt an in-progress
        recording" path: misjudging a recording camera as idle would re-arm it
        (loud beep) and could stop the take."""
        for _ in range(max(1, retries)):
            st = self.state()
            if st is not None:
                return st.get(ST_ENCODING) == 1
            time.sleep(delay)
        return False

    @staticmethod
    def _sd_usable(st: dict | None) -> bool:
        """Whether the SD card can actually be recorded to.
        Hero 12 quirk (fw 02.32.70): status 33 stays 0 even with no card or an
        unformatted/FTL-broken card -- the reliable signal is remaining video
        time (35) / photos (34) dropping to 0. So require that, not just 33."""
        if not st:
            return False
        if st.get(ST_SD) in (1, 2, 3):      # full / removed / needs-format
            return False
        return (st.get(ST_REMAINING_SEC) or 0) > 0 or (st.get(ST_REMAINING_PHOTOS) or 0) > 0

    def sd_present(self) -> bool:
        return self._sd_usable(self.state())

    def set_setting(self, setting_id: int, option: int) -> bool:
        """Apply one Open GoPro setting (resolution, fps, fov, ...)."""
        code, _ = self._request(
            f"/gopro/camera/setting?setting={setting_id}&option={option}", timeout=4)
        return code == 200

    def ensure_auto_power_off_never(self) -> bool:
        """Re-assert Auto Power Off = Never if it has drifted (cameras run on USB
        with no battery, so the setting reverts after a power loss, and an operator
        may change it). Returns True if a correction was applied. Cheap: it only
        writes when the camera reports a non-Never value, a no-op on a healthy cam."""
        code, data = self._request("/gopro/camera/state", timeout=4)
        if code != 200 or not data:
            return False
        if data.get("settings", {}).get(str(AUTO_POWER_OFF)) == AUTO_POWER_OFF_NEVER:
            return False
        return self.set_setting(AUTO_POWER_OFF, AUTO_POWER_OFF_NEVER)

    def set_datetime(self) -> bool:
        """Set the camera clock to the host's current UTC time.
        tzone=0&dst=0 makes the camera store the given UTC verbatim (otherwise
        it re-applies an internal offset and media timestamps drift by hours).
        The GoPro clock drifts (~1s/10min), so call this just before recording."""
        now = datetime.datetime.now(datetime.timezone.utc)
        d, t = now.strftime("%Y_%m_%d"), now.strftime("%H_%M_%S")
        code, _ = self._request(
            f"/gopro/camera/set_date_time?date={d}&time={t}&tzone=0&dst=0", timeout=4)
        return code == 200

    def shutter_works(self) -> bool:
        """The only honest readiness test: a real (brief) shutter call.
        A zombie camera answers /state with 200 but /shutter/start with 500."""
        if not self.start():
            return False
        time.sleep(0.4)
        self.stop()
        return True

    def health(self) -> dict:
        """Cheap status snapshot for monitoring / publishing."""
        st = self.state()
        return {
            "label": self.label,
            "ip": self.ip,
            "reachable": st is not None,
            "recording": bool(st and st.get(ST_ENCODING) == 1),
            "busy": bool(st and st.get(ST_BUSY) == 1),   # SD/file op in progress
            "sd_ok": self._sd_usable(st),
            "remaining_sec": (st.get(ST_REMAINING_SEC) if st else None),
            "can_power_cycle": self.can_power_cycle(),
        }

    # --- power -------------------------------------------------------------
    def can_power_cycle(self) -> bool:
        return bool(self.hub and self.port)

    def power_cycle(self, off_seconds: int = 8, boot_timeout: float = 35.0) -> bool:
        """Cut Vbus on this camera's hub port and bring it back. The camera
        fully reboots (~15s). Returns True once its HTTP API answers again.
        Default 8s OFF matches revive.sh's off-bus cut: a no-battery Hero 12
        sometimes fails to cold-boot on a shorter (4s) cut."""
        if not self.can_power_cycle():
            return False
        subprocess.run(
            ["sudo", UHUBCTL, "-l", self.hub, "-p", self.port,
             "-a", "cycle", "-d", str(off_seconds)],
            check=False, capture_output=True)
        deadline = time.time() + boot_timeout
        while time.time() < deadline:
            if self.reachable(timeout=2):
                return True
            time.sleep(2)
        return False

    def recover(self) -> bool:
        """Full recovery for a stuck camera: power-cycle Vbus, then re-init and
        verify it will actually record."""
        if not self.power_cycle():
            return False
        time.sleep(3)
        self.init()
        return self.shutter_works()

    def __repr__(self) -> str:
        loc = f"{self.hub}/p{self.port}" if self.can_power_cycle() else "no-ppps"
        return f"<GoPro {self.label} ip={self.ip} hub={loc} iface={self.iface}>"


def sync_datetime(cameras: list[GoPro]) -> dict[str, bool]:
    """Set the SAME UTC timestamp on every camera at (nearly) the same instant.

    Timestamp accuracy/agreement is the priority for the AUV: the GoPro clock has
    1-second resolution, so we compute the time string ONCE and push that exact
    string to all cameras through a barrier. Both cameras then store the same
    second (they agree with each other -- what matters for re-aligning footage in
    post), and it is as close to the Pi's true UTC time as the 1s resolution and
    HTTP latency allow. Requires the Pi clock to be NTP-synced (see
    system_clock_synced). tzone=0&dst=0 stores the UTC verbatim."""
    if not cameras:
        return {}
    now = datetime.datetime.now(datetime.timezone.utc)
    path = (f"/gopro/camera/set_date_time?date={now.strftime('%Y_%m_%d')}"
            f"&time={now.strftime('%H_%M_%S')}&tzone=0&dst=0")
    barrier = threading.Barrier(len(cameras), timeout=5)

    def worker(cam):
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        code, _ = cam._request(path, timeout=4)
        return cam.label, code == 200

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(cameras)) as pool:
        return dict(pool.map(worker, cameras))


def system_clock_synced() -> bool:
    """True if the host clock is NTP-synchronized (so camera time will be right).
    Underwater the AUV loses the network, so sync must happen before diving."""
    try:
        out = subprocess.check_output(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            text=True, stderr=subprocess.DEVNULL).strip()
        return out == "yes"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def set_recording(cameras: list[GoPro], start: bool) -> dict[str, bool]:
    """Start or stop recording on all cameras at (nearly) the same instant.
    Each camera runs in its own thread and waits at a barrier, so every shutter
    request leaves within ~1ms of the others instead of being staggered by the
    order they were launched. Returns {label: ok}."""
    if not cameras:
        return {}
    action = (lambda c: c.start()) if start else (lambda c: c.stop())
    barrier = threading.Barrier(len(cameras), timeout=5)

    def worker(cam):
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return cam.label, action(cam)

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(cameras)) as pool:
        return dict(pool.map(worker, cameras))


def discover(labels: list[str] | None = None) -> list[GoPro]:
    """Find every wired GoPro by scanning network interfaces for the Open GoPro
    wired subnet, then resolving each one's hub port for power control.
    `labels` optionally names them in discovery order (e.g. ["LEFT","RIGHT"])."""
    cams: list[GoPro] = []
    for path in sorted(glob.glob("/sys/class/net/*")):
        iface = os.path.basename(path)
        if iface == "lo":
            continue
        host_ip = _ipv4_of(iface)
        if not host_ip or not _GOPRO_SUBNET_RE.match(host_ip):
            continue
        cam_ip = re.sub(r"\.\d+$", ".51", host_ip)
        hub, port = _usb_hub_port(iface)
        cams.append(GoPro(label="", ip=cam_ip, hub=hub, port=port, iface=iface))
    # Order by physical USB slot (hub, then port), so labels map to a socket and
    # stay put when a camera is swapped for another in the same socket. Cameras
    # without a resolvable slot fall back to IP order (sorted last).
    def _slot_key(c):
        p = int(c.port) if c.port and str(c.port).isdigit() else 0
        return (0, c.hub, p) if c.hub else (1, c.ip, 0)
    cams.sort(key=_slot_key)
    for i, cam in enumerate(cams):
        cam.label = labels[i] if labels and i < len(labels) else f"CAM{i}"
    return cams
