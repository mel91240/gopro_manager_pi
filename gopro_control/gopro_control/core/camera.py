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
import time
import urllib.error
import urllib.request

UHUBCTL = "/usr/sbin/uhubctl"
HTTP_PORT = 8080

# Open GoPro status field IDs (from /gopro/camera/state -> "status")
ST_BATTERY_PRESENT = "1"
ST_BUSY = "8"
ST_ENCODING = "10"
ST_SD = "33"          # 0=OK 1=full 2=removed 3=needs-format 4=busy
ST_SPACE_KB = "54"

PRESET_GROUP_VIDEO = 1000
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
        self._request("/gopro/camera/control/wired_usb?p=1", timeout=4)
        time.sleep(1.5)
        return self.set_preset_group(PRESET_GROUP_VIDEO)

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

    def sd_present(self) -> bool:
        # A removed/swapped card can still report status 33 == 0 from cache, so
        # also require non-zero free space to call the SD usable.
        st = self.state()
        return bool(st and st.get(ST_SD) == 0 and (st.get(ST_SPACE_KB) or 0) > 0)

    def set_setting(self, setting_id: int, option: int) -> bool:
        """Apply one Open GoPro setting (resolution, fps, fov, ...)."""
        code, _ = self._request(
            f"/gopro/camera/setting?setting={setting_id}&option={option}", timeout=4)
        return code == 200

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
            "sd_ok": bool(st and st.get(ST_SD) == 0 and (st.get(ST_SPACE_KB) or 0) > 0),
            "can_power_cycle": self.can_power_cycle(),
        }

    # --- power -------------------------------------------------------------
    def can_power_cycle(self) -> bool:
        return bool(self.hub and self.port)

    def power_cycle(self, off_seconds: int = 4, boot_timeout: float = 35.0) -> bool:
        """Cut Vbus on this camera's hub port and bring it back. The camera
        fully reboots (~15s). Returns True once its HTTP API answers again."""
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
    """Start or stop recording on all cameras as close to simultaneously as the
    network allows, by firing the HTTP calls concurrently. Returns {label: ok}."""
    if not cameras:
        return {}
    action = (lambda c: c.start()) if start else (lambda c: c.stop())
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(cameras)) as pool:
        results = pool.map(action, cameras)
    return {cam.label: ok for cam, ok in zip(cameras, results)}


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
    # stable ordering by camera IP, then label
    cams.sort(key=lambda c: c.ip)
    for i, cam in enumerate(cams):
        cam.label = labels[i] if labels and i < len(labels) else f"CAM{i}"
    return cams
