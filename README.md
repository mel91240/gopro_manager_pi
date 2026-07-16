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

**Prerequisites on the Pi:** the `cosma_auv` image, the `~/dev/swarm-vehicle`
workspace, internet access, `uhubctl`, and the **correct date/time**.

> ⚠️ **Set the clock first.** A freshly flashed Pi with no RTC/NTP can boot with
> its clock in the past, which makes `apt-get update` and TLS (`git clone`) fail
> confusingly. Fix it before anything else:
> ```bash
> sudo timedatectl set-ntp true                 # if the Pi has internet, or:
> sudo date -u -s "YYYY-MM-DD HH:MM:SS"          # set UTC manually
> ```

```bash
sudo apt update && sudo apt install -y uhubctl
git clone https://github.com/mel91240/gopro_manager_pi.git
cd gopro_manager_pi
./install.sh                    # build + host scripts + sudoers + boot services (idempotent)
# non-default locations:  GOPRO_WS=/path/to/swarm-vehicle COSMA_IMAGE=myimg:tag ./install.sh
# update later:           git pull && ./install.sh
# remove:                 ./uninstall.sh        (footage is never touched)
```

`install.sh` copies the ROS packages into the workspace and `colcon build`s them
in the `cosma_auv` container, installs the host scripts into `gopro_scripts/`, the
`uhubctl` sudoers rule, and the two systemd boot services. After it, **the manager
and the auto-revive watcher start on every boot and the cameras arm themselves** --
no manual step.

> One extra prerequisite for the SSD offload: the SSD must run with UAS disabled
> (`usb-storage.quirks=<VID:PID>:u` in `/boot/firmware/cmdline.txt`, then reboot),
> or heavy writes wedge the Pi 4's shared USB controller. `download.sh` warns if
> it is missing.

---

## Commands

Everything runs from the scripts in `gopro_scripts/` -- there is no interactive menu.

**Recording** -- these talk to the running manager, so its watchdog/EMERGENCY
logic stays in charge:
```bash
./gopro_ctl.sh record            # start recording on all cameras   (alias: start)
./gopro_ctl.sh stop              # stop
./gopro_ctl.sh status            # READY/RECORDING + per-camera SD
./gopro_ctl.sh settings resolution=4K fps=30 fov=Linear   # only the fields you pass
./gopro_ctl.sh solo LEFT         # keep one camera, power the other off
./gopro_ctl.sh duo               # re-enable both
```
Running `settings` with a wrong/missing value prints the full list of valid options.

**Offload / delete / logs:**
```bash
./download.sh                    # mount the SSD + resumable offload (live %/speed/ETA)
./download.sh --verify           # check every camera clip has a same-size copy on the SSD
./gopro_delete.py --pick         # delete selected clips   (--all wipes the cards; type "all" to confirm)
./manager_log.sh                 # live manager + auto-revive + download logs, in one stream
```

**Manager (normally automatic, boot service):**
```bash
./manager_up.sh   /   ./manager_down.sh      # manual start/stop (also the service Exec hooks)
```

**Typical mission:** power on (cameras auto-arm) → `./gopro_ctl.sh record`, put the
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
  gopro_ctl.sh              # operator control: record / stop / status / settings / solo / duo
  download.sh + gopro_download.py   # offload footage -> SSD (robust, resumable)
  gopro_delete.py           # delete / wipe media on the cameras
  revive.sh                 # auto-revive watcher (--watch); uses host uhubctl
  manager_up.sh / manager_down.sh / manager_log.sh
  systemd/                  # gopro-manager / gopro-autorevive service TEMPLATES
tests/             # automated ROS-interface test suite
```

---

## Hardware notes / lessons learned
- **USB cable / uplink matters most.** The Hero 12 is a USB 2.0 device. A marginal or USB-3 uplink to the hub forces a flaky SuperSpeed link that drops under recording load -- the **#1 cause of "cameras drop mid-record"**. Use good USB 2.0 cables, firmly seated.
- **Power budget.** Two Hero 12 recording 4K draw a lot (no battery, all on USB). A marginal hub supply browns out into a zombie state. Use a solid **5 V / 4-5 A+** supply.
- **A wedged hub survives a Pi reboot** (it is self-powered): if all cameras drop at once and `dmesg` shows repeated `usb ... error -110`, physically power-cycle the hub.
- **Reboot interrupts recording** (USB bus reset), but the GoPro recovers the in-progress file on next boot and the manager auto-resumes a new segment.
- **SD safety.** Cutting Vbus on a *visible/recording* camera can corrupt the card; on an *off-bus* (idle) camera it is safe -- and the only cure for a "zombie" camera (answers `/state` but `/shutter` 500).
```
