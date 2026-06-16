# gopro_manager_pi — autonomous wired GoPro control for the AUV

Two self-contained ROS 2 packages + host-side helpers that turn a pair of
**GoPro Hero 12** cameras (driven over wired USB from a Raspberry Pi through a
powered hub) into an **autonomous, reboot-proof** recording subsystem for the
AUV. No existing package is modified; the cameras are driven over the **Open
GoPro wired HTTP API** (camera at `.51:8080`).

Validated live on the vehicle Pi (`auv006`) with 2× Hero 12 Black:
- Hero 12 runs and records **with no battery**, powered over USB.
- The whole flow survives the operator disconnecting, and survives a Pi reboot.

---

## Architecture

Three pieces, by design separate:

| Piece | Where | Role | Lifetime |
|---|---|---|---|
| **`gopro_manager`** | Docker container (`cosma_auv`) | owns the cameras: arm, record, watchdog, auto-resume | persistent — systemd boot service (`gopro-manager.service`) + Docker `--restart`, survives SSH/reboot |
| **`pi_menu`** | transient container | recording console (start/stop/settings), reached from `gopro.sh [1]` | open & close at will |
| **auto-revive watcher** | host (systemd) | power-cycle a camera that fell **off the bus** (Vbus stays on the host) | persistent (starts on boot) |
| **`gopro.sh`** + offload tools | host | unified operator menu: record / offload to SSD / wipe cards / manager | run on demand |

The manager has **no USB-power privileges** (it can't corrupt anything); the
only component that touches Vbus is the host watcher, and only on a camera
confirmed off the bus.

```
gopro_control/gopro_control/
  core/            # ROS-independent engine (urllib only, no extra deps)
    camera.py      # GoPro class: discovery, state, control
    settings.py    # Open GoPro setting maps + validation + ordered apply
    cli.py         # dev CLI for live testing without ROS
  nodes/
    gopro_manager_node.py   # the manager node
    pi_menu_node.py         # the recording console
scripts/           # host-side operation (run on the Pi)
  gopro.sh                  # unified operator menu (entry point): record / offload / wipe / manager
  manager_up.sh / manager_down.sh / manager_log.sh / menu.sh
  revive.sh                 # one-shot or --watch auto-revive
  gopro_download.py         # offload footage -> SSD (robust, resumable, live progress)
  gopro_delete.py           # delete / wipe media on the cameras
  mp4meta.py                # MP4 start time + GPMF/IMU stream detection (no ffmpeg)
  gopro_bench.py            # transfer-speed diagnostic
  gopro-manager.service / gopro-autorevive.service   # systemd boot units
  install_service.sh        # installs+enables both boot services (run once)
gopro_msgs/        # GoProStatus.msg, GoProSystem.msg, GoProSettings.srv
tests/             # automated ROS-interface test suite
```

---

## Features

**Recording & operator flow**
- **Persistent manager**, decoupled from the menu — keeps running while the AUV
  is underwater and after the operator drops SSH.
- **Barrier-synced** two-camera start/stop **and clock-sync** — both cameras get
  the same UTC second, so segments re-align in post even if start isn't perfectly
  simultaneous.
- **Operator console** (`pi_menu`) with **live status refresh** (the banner
  updates the instant a camera drops — `DEGRADED`/`EMERGENCY` — without a
  keypress) and clear guards (no double-start beep, can't change settings while
  recording).
- **Settings** (resolution/fps/fov/hypersmooth/wind/mode) with **validation**
  (rejects impossible combos) and **inter-setting delays** the Hero 12 needs.

**Resilience (autonomous)**
- **Adopt in-progress recording**: if the manager restarts while filming, it
  detects and adopts the recording (no re-arm, no beep).
- **Watchdog**: a camera that stops/drops → `DEGRADED` while its **first**
  recovery attempt is in flight (re-arm + restart, **no power-cycle**); the AUV
  can slow down. Recovers as soon as it answers again.
- **EMERGENCY signal** (`FAULT` = the AUV should stop): raised as soon as a
  dropped camera's **first recovery attempt has failed** (kept retrying), **or**
  all cameras drop at once (nothing is filmed → immediate), **or** a camera is
  SD-unusable, **or** — as a backstop — any camera stays lost past `fault_after`
  (e.g. a slow self-repair that never reached a retry). `FAULT` is what the
  autonomy layer consumes (`state == GoProSystem.STATE_FAULT`); it self-clears
  the instant the camera records again.
- **Auto-resume after reboot**: a persistent intent flag means an involuntary
  reboot that interrupted a mission **resumes recording on its own** (a new,
  timestamped segment).
- **Boot-robust discovery**: retries discovery at startup (USB may not be up yet
  when the auto-restarted manager starts).
- **Auto-revive** (host watcher): a camera that falls **off the USB bus** is
  power-cycled automatically, then re-armed by the manager — fully autonomous.
  Safe by construction (see below).

**Safety (SD card)**
- Vbus is cut **only** on a camera **confirmed off the bus** for several
  consecutive scans (~6 s) — never a visible camera (it might be recording),
  never on a 1 s blip. A camera off the bus is idle, so its SD can't be corrupted.
- Cameras are tracked **by hub port**, not serial — a flooded camera swapped for
  a fresh one in the same socket works with no code change.

---

## Operating

One-time setup installs the boot services, so the manager + watcher start
automatically on power-up and the cameras arm themselves — no manual step:

```bash
./install_service.sh     # installs+enables gopro-manager + gopro-autorevive
```

`./gopro.sh` is then the single operator entry point (run on the Pi):

```
=== AUV GoPro ===  (manager: UP | SSD: mounted)
  [1] Recording (record / stop / settings)    -> the ROS console
  [2] Copy footage -> SSD                      -> live %/speed/ETA
  [3] Delete / wipe the cards
  [4] Start / stop the manager
```

Typical mission: power on (cameras auto-arm) → `gopro.sh [1]` to start the take,
put the AUV in the water, close SSH (the manager keeps running and auto-resumes
a new segment if a reboot interrupts the take) → after the dive, reconnect,
`gopro.sh [1]` [2] to stop, then [2] to offload to the SSD and [3] to wipe the
cards once the footage is safe.

Recovery helper (also runs automatically as the watcher service):
```bash
./revive.sh            # one-shot: power-cycle a camera confirmed off the bus
```

---

## ROS 2 interface

Node `gopro_manager` (private namespace):

| Interface | Type | Purpose |
|---|---|---|
| `~/record` | `std_srvs/SetBool` | `true` start / `false` stop, all cameras (barrier-synced) |
| `~/settings` | `gopro_msgs/GoProSettings` | apply capture settings to all cameras |
| `~/status` | `gopro_msgs/GoProStatus` | per-camera health, published each tick |
| `~/system` | `gopro_msgs/GoProSystem` | overall state: `INITIALIZING`/`READY`/`RECORDING`/`DEGRADED`/`FAULT` |

`FAULT` is the **emergency** signal for the autonomy layer.

### Parameters (`params/gopro_params.yaml`)
`camera_labels`, `tick_period` (1 s), `strikes_before_restart` (2),
`record_grace_period` (10 s), `restart_cooldown` (0 s), `fault_after` (30 s),
`discovery_timeout` (20 s), `resume_on_restart` (true), `state_file`.

---

## Build / deploy

ROS 2 Humble is provided by the `cosma_auv` Docker image (not on the host).
Copy the two packages into the workspace and build only them:

```bash
# packages live in ~/dev/swarm-vehicle/ros2_ws/src/  (next to auv, auv_msgs, ...)
docker run --rm -v ~/dev/swarm-vehicle:/home/cosma_auv/swarm-vehicle \
  --entrypoint bash cosma_auv:latest -lc \
  "source /opt/ros/humble/setup.bash && cd /home/cosma_auv/swarm-vehicle/ros2_ws && \
   colcon build --packages-select gopro_msgs gopro_control"
```

The host watcher needs passwordless `uhubctl`:
`/etc/sudoers.d/uhubctl` → `pi ALL=(ALL) NOPASSWD: /usr/sbin/uhubctl`
(`install_service.sh` assumes this is in place).

The manager + menu containers use `--network host --ipc host -e ROS_DOMAIN_ID=0
-e ROS_LOCALHOST_ONLY=1` (required for cross-container DDS discovery on the same
host). The scripts set these.

---

## Hardware notes / lessons learned

- **USB cable matters.** The Hero 12 is a USB 2.0 device. A USB 3 cable forces a
  marginal SuperSpeed link that drops under recording load. **Use USB 2.0 cables.**
- **Power budget.** Two Hero 12 recording 4K can peak ~1.5 A each. A 5 V/3 A hub
  supply is marginal (brown-out → "NOT ENOUGH POWER" → zombie state on a camera).
  **Use 5 V/4–5 A+.**
- **Reboot interrupts recording** (USB bus reset), but the GoPro recovers the
  in-progress file on next boot, and the manager auto-resumes a new segment.
- **SD safety.** Cutting Vbus while a camera is *visible/recording* can corrupt
  the card; cutting Vbus on a camera that is *off the bus* (idle) is safe and is
  the only cure for a "zombie" camera (answers `/state` but `/shutter` 500).
```
