# gopro_manager_pi — autonomous wired GoPro control for the AUV

Drive a pair of **GoPro Hero 12** cameras from a Raspberry Pi over wired USB:
start/stop recording, change settings, offload and wipe footage -- all under
software control, no one touching the cameras. Built for an AUV, so the recording
subsystem is **autonomous and reboot-proof**. The cameras run **with no battery**
(USB-powered) and are driven over the **Open GoPro wired HTTP API** (`.51:8080`).

Validated live on the vehicle Pi (`auv006`) with 2x Hero 12 Black.

---

## Install

A **self-contained add-on** to a Pi that already has the base AUV setup (the
`cosma_auv` Docker image + the `swarm-vehicle` workspace). It adds two ROS 2
packages and a `gopro_scripts/` folder, changes **no** existing package, and needs
**no Docker image change**.

**Prerequisites on the Pi:**
- the **`cosma_auv:latest` Docker image** and the **`~/dev/swarm-vehicle` workspace** (the base AUV setup),
- **internet access** (for `git clone` + `apt` — see "First boot" below),
- the **correct date/time** (the Pi has no RTC),
- **`uhubctl`** — the auto-revive watcher power-cycles cameras through it (`sudo apt install -y uhubctl`).

### First boot on a freshly flashed Pi — get it online

A fresh image usually boots with **no internet and the wrong clock**, and both
make `git clone` / `apt` fail confusingly. Sort them out first:

```bash
# 1. connect WiFi for internet (BlueOS / any Pi on NetworkManager -- SSH in over ethernet first):
sudo nmcli device wifi connect "<SSID>" password "<PASSWORD>" ifname wlan0
ping -c2 8.8.8.8                              # confirm raw internet works

# 2. set the clock (no RTC -> it resets on every boot):
sudo timedatectl set-ntp true                 # with internet it self-syncs, or:
sudo date -u -s "YYYY-MM-DD HH:MM:SS"          # set UTC by hand
```
> ⚠️ A **wrong clock breaks TLS**, so `git clone` / `apt update` return confusing
> errors (and `curl https://…` returns `000`) even when the network is perfectly
> fine. Fix the clock before blaming the connection.

### Install

```bash
sudo apt update && sudo apt install -y uhubctl   # auto-revive dependency (not preinstalled on a fresh image)
git clone https://github.com/mel91240/gopro_manager_pi.git
cd gopro_manager_pi
./install.sh                    # build + host scripts + sudoers + boot services (idempotent)
./setup.sh                      # REQUIRED for SSD offload: disable UAS for the SSD (plug it in first) -> reboots
# non-default locations:  GOPRO_WS=/path/to/swarm-vehicle COSMA_IMAGE=myimg:tag ./install.sh
# update later:           git pull && ./install.sh
# remove:                 ./uninstall.sh        (footage is never touched)
```

`install.sh` copies the ROS packages into the workspace and `colcon build`s them
in the `cosma_auv` container, installs the host scripts into `gopro_scripts/`, the
`uhubctl` sudoers rule, and the two systemd boot services. After it, **the manager
and the auto-revive watcher start on every boot and the cameras arm themselves** --
no manual step.

> ⚠️ **`./setup.sh` is required before the first SSD offload** (run it once, SSD
> plugged in). It auto-detects the drive's VID:PID and adds
> `usb-storage.quirks=<VID:PID>:u` to the kernel cmdline (backup, idempotent), then
> **reboots**. Without it, heavy writes wedge the Pi 4's shared USB controller (the
> #1 cause of the "cameras drop / SSD falls off the bus" crashes). `download.sh`
> warns on every run until it is done. After the reboot, re-set the clock (no RTC).

---

## Commands

Everything runs from the scripts in `gopro_scripts/` -- there is no interactive menu.

**Recording** -- these talk to the running manager, so its watchdog/EMERGENCY
logic stays in charge:
```bash
./gopro_ctl.sh start             # start recording on all cameras   (alias: record)
./gopro_ctl.sh stop              # stop
./gopro_ctl.sh status            # READY/RECORDING + per-camera SD
./gopro_ctl.sh settings resolution=4K fps=30 fov=Linear   # only the fields you pass
./gopro_ctl.sh solo LEFT         # keep one camera, power the other off
./gopro_ctl.sh duo               # re-enable both   (alias: on)
./gopro_ctl.sh off               # power BOTH cameras off (idle, anti-overheat) until 'on'
```
Running `settings` with a wrong/missing value prints the full list of valid options.

> ⚠️ **Do not use 5K / 5.3K.** On this rig the cameras run on USB power with **no
> battery**, and 5.3K capture draws more than the bus can reliably sustain -- it
> browns the camera into a zombie (answers `/state`, refuses `/shutter`). Stay at
> **4K or below** for dependable recording.

**Offload / delete / logs:**
```bash
./download.sh                    # mount the SSD + resumable offload (live %/speed/ETA)
./download.sh --verify           # check every camera clip has a same-size copy on the SSD
./gopro_delete.py --pick         # delete selected clips   (--all wipes the cards; type "all" to confirm)
./manager_log.sh                 # live manager + auto-revive + download logs, in one stream
```

The same stream is also **saved to a file, one per manager (re)start**, by the
`gopro-logfile` service (bound to the manager). Files land in the workspace's
`log/gopro/` folder named `manager_<start-date>.log`, with `latest.log` pointing at
the current session; only the newest 50 are kept. So every session leaves a
permanent transcript on disk, even after the manager stops or reloads:
```bash
cat  ~/dev/swarm-vehicle/log/gopro/latest.log   # transcript of the current session
ls   ~/dev/swarm-vehicle/log/gopro/             # all past sessions, one dated file each
```

**Manager (normally automatic, boot service):**
```bash
./manager_up.sh   /   ./manager_down.sh      # manual start/stop (also the service Exec hooks)
```

**Typical mission:** power on (cameras auto-arm) → `./gopro_ctl.sh start`, put the
AUV in the water, close SSH (the manager keeps running; if a reboot interrupts the
take it auto-resumes a new segment) → after the dive, `./gopro_ctl.sh stop`,
`./download.sh` to offload, then `./gopro_delete.py --all` once the footage is safe.

---

## How it works

Three pieces, deliberately separate:

| Piece | Where | Role |
|---|---|---|
| **`gopro_manager`** | Docker (`cosma_auv` image) | owns the cameras: arm, record, watchdog, auto-resume, EMERGENCY |
| **auto-revive watcher** (`revive.sh`) | host (systemd) | power-cycle a camera that fell **off the bus** (Vbus lives on the host) |
| **host scripts** (`gopro_ctl.sh` / `download.sh` / `gopro_delete.py`) | host | operator control, offload, wipe |

The system spans two worlds -- **Docker** (where ROS 2 lives) and the **host**
(where `uhubctl` and the SSD live):

```
┌─ Raspberry Pi — HOST (native, no ROS; has uhubctl + the SSD) ───────────────┐
│                                                                             │
│  gopro_ctl.sh ──(docker exec: ros2 service call)────────────────┐          │
│  download.sh / gopro_download.py ──HTTP :8080──► cameras ──► SSD │          │
│  gopro_delete.py ──────────────── HTTP :8080──► cameras         │          │
│                                                                 ▼          │
│  revive.sh (watcher, systemd)   ┌──────── DOCKER (cosma_auv image) ──────┐ │
│    │  reads .revive_request      │                                       │ │
│    │  ◄──(a FILE)─────────────── │  gopro_manager  (persistent service)  │ │
│    ▼                             │    arm · record · watchdog · EMERGENCY│ │
│  uhubctl ── cuts Vbus on a       └───────────────────────────────────────┘ │
│  socket that is OFF the bus                                                 │
│         │                                                                   │
│  ┌──────────────┐   per-port Vbus (PPPS)                                    │
│  │   USB hub    │                                                           │
│  └──────┬───────┘   each GoPro = a USB-Ethernet device at 172.2x.x.51       │
│    ┌────┴────┐      (wired Open GoPro HTTP API on :8080)                     │
│   GoPro L   GoPro R                                                          │
└─────────────────────────────────────────────────────────────────────────────┘

  Control    gopro_ctl.sh runs `docker exec ... ros2 service call` INTO the
             manager container, so the manager (and its watchdog) stays in charge.
  Recovery   the manager (in Docker, no uhubctl) writes "hub:port" to
             .revive_request; revive.sh (on the host) reads it and power-cycles
             that socket -- an off-bus camera gets a Vbus cycle without giving the
             container any USB-power privileges.
```

Why the split: ROS nodes need the ROS image → Docker; `uhubctl` (USB power) and
the SSD need host access → host. Cameras are identified by **USB socket (hub,
port)**, never a fixed IP, so a swapped camera in the same port just works.

### Resilience (autonomous)
- **Adopt in-progress recording** -- if the manager restarts while filming, it adopts the recording (no re-arm, no beep).
- **Watchdog** -- a dropped camera → `DEGRADED` while the first recovery (re-arm + restart, **no power-cut**) is in flight; recovers as soon as it answers again.
- **EMERGENCY** (`FAULT` = the AUV should hold/stop) -- raised when recovery keeps failing, or all cameras drop at once, or a camera is SD-unusable, or one stays lost past `fault_after_seconds`. Self-clears the instant a camera films again.
- **Auto-resume after reboot** -- a persistent intent flag resumes recording (a new timestamped segment) after an involuntary reboot.
- **Auto-revive** -- a camera **off the USB bus** is power-cycled by the host watcher, then re-armed by the manager. Fully autonomous.

### Safety (SD card)
Vbus is cut **only** on a camera confirmed **off the bus** for several scans
(~6 s) -- never a visible/recording camera, never on a 1 s blip. A camera off the
bus is idle, so its SD card cannot be corrupted.

---

## ROS 2 interface

Node `gopro_manager` (private namespace):

| Interface | Type | Purpose |
|---|---|---|
| `~/record` | `std_srvs/SetBool` | `true` start / `false` stop, all cameras (barrier-synced) |
| `~/settings` | `gopro_msgs/GoProSettings` | apply capture settings |
| `~/status` | `gopro_msgs/GoProStatus` | per-camera health, each tick |
| `~/system` | `gopro_msgs/GoProSystem` | overall state: `INITIALIZING`/`READY`/`RECORDING`/`DEGRADED`/`FAULT` |

`FAULT` is the **EMERGENCY** signal for the autonomy layer (`state == GoProSystem.STATE_FAULT`).

State machine (published on `~/system`):
```
   INITIALIZING ──(all cameras armed)──► READY ◄── stop ── RECORDING
        │ (cameras missing / SD bad)             start ─►     │
        ▼                                     a camera drops   │ (films again -> clears)
      FAULT ◄── recovery keeps failing / lost > fault_after_seconds /
        │        SD unusable / all cameras stopped at once ──► DEGRADED
        └─► EMERGENCY: the AUV should hold. Recovery is software-first; a real Vbus
            cycle is requested only for an off-bus / brown-out camera.
```

### Parameters (`params/gopro_params.yaml`, loaded at manager (re)start)
`camera_labels`, `tick_period` (1 s), `strikes_before_restart` (2),
`fault_after_attempts` (9), `unreliable_after` (3), `record_grace_period` (10 s),
`restart_cooldown` (5 s), `fault_after_seconds` (45 s), `discovery_timeout` (20 s),
`resume_on_restart` (true). Brown-out Vbus recovery: `vbus_recover_after` (3),
`vbus_cooldown` (45 s), `vbus_request_file`.

---

## Repo layout

```
install.sh / uninstall.sh   # one-command add/remove on any base-image Pi
ros2_pkgs/                  # the ROS half -> built in the cosma_auv container
  gopro_control/gopro_control/
    core/          # ROS-independent engine (urllib only, no extra deps)
      camera.py    # GoPro class: discovery, state, control, Vbus (uhubctl)
      settings.py  # Open GoPro setting maps + validation + ordered apply
      cli.py       # dev CLI for live testing without ROS
    nodes/gopro_manager_node.py   # the manager node
  gopro_msgs/      # GoProStatus.msg, GoProSystem.msg, GoProSettings.srv
host/                        # the HOST half -> installed into gopro_scripts/
  gopro_ctl.sh              # operator control: start / stop / status / settings / solo / duo / off / on
  download.sh + gopro_download.py   # offload footage -> SSD (robust, resumable)
  gopro_delete.py           # delete / wipe media on the cameras
  revive.sh                 # auto-revive watcher (--watch); uses host uhubctl
  manager_up.sh / manager_down.sh / manager_log.sh
  manager_logfile.sh        # saves each manager session to a dated file in <workspace>/log/gopro/
  systemd/                  # gopro-manager / gopro-autorevive / gopro-logfile service TEMPLATES
```

---

## Hardware notes / lessons learned
- **USB cable / uplink matters most.** The Hero 12 is a USB 2.0 device. A marginal or USB-3 uplink to the hub forces a flaky SuperSpeed link that drops under recording load -- the **#1 cause of "cameras drop mid-record"**. Use good USB 2.0 cables, firmly seated.
- **Power budget.** Two Hero 12 recording 4K draw a lot (no battery, all on USB). A marginal hub supply browns out into a zombie state. Use a solid **5 V / 4-5 A+** supply.
- **Never 5K / 5.3K.** Without a battery the USB rail cannot sustain 5.3K capture; the camera brown-outs into the zombie state (answers `/state`, refuses `/shutter`). Cap resolution at **4K**.
- **A wedged hub survives a Pi reboot** (it is self-powered): if all cameras drop at once and `dmesg` shows repeated `usb ... error -110`, physically power-cycle the hub.
- **Reboot interrupts recording** (USB bus reset), but the GoPro recovers the in-progress file on next boot and the manager auto-resumes a new segment.
- **SD safety.** Cutting Vbus on a *visible/recording* camera can corrupt the card; on an *off-bus* (idle) camera it is safe -- and the only cure for a "zombie" camera (answers `/state` but `/shutter` 500).
```
