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
| **`gopro_manager`** | Docker container (`cosma_auv`) | owns the cameras: arm, record, watchdog, auto-resume | persistent (Docker `--restart`, survives SSH/reboot) |
| **`pi_menu`** | transient container | operator console (start/stop/settings) | open & close at will |
| **auto-revive watcher** | host (systemd) | power-cycle a camera that fell **off the bus** (Vbus stays on the host) | persistent (starts on boot) |

The manager has **no USB-power privileges** (it can't corrupt anything); the
only component that touches Vbus is the host watcher, and only on a camera
confirmed off the bus.

```
gopro_control/gopro_control/
  core/            # ROS-independent engine (urllib only, no extra deps)
    camera.py      # GoPro class, discovery, state, (uhubctl power-cycle helpers)
    settings.py    # Open GoPro setting maps + validation + ordered apply
    cli.py         # dev CLI for live testing without ROS
  nodes/
    gopro_manager_node.py   # the manager node
    pi_menu_node.py         # the operator console
scripts/           # host-side operation (run on the Pi)
  manager_up.sh / manager_down.sh / manager_log.sh
  menu.sh
  revive.sh                 # one-shot or --watch auto-revive
  gopro-autorevive.service  # systemd unit for the watcher
  install_service.sh        # installs+enables the service (run once)
gopro_msgs/        # GoProStatus.msg, GoProSystem.msg, GoProSettings.srv
tests/             # automated test suite + bench mission script
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
- **Watchdog**: a camera that stops/drops → `DEGRADED`, retried in software
  (re-arm + restart, **no power-cycle**). Recovers as soon as it answers again.
- **EMERGENCY signal**: unrecoverable past `fault_after` (or SD unusable) →
  state `FAULT` = the emergency signal the autonomy layer consumes
  (`state == GoProSystem.STATE_FAULT`) to surface the AUV. Keeps retrying, so it
  self-clears if the camera returns.
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
  consecutive scans (~15 s) — never a visible camera (it might be recording),
  never on a 1 s blip. A camera off the bus is idle, so its SD can't be corrupted.
- Cameras are tracked **by hub port**, not serial — a flooded camera swapped for
  a fresh one in the same socket works with no code change.

---

## Operating (mission flow)

On the Pi (`~/dev/swarm-vehicle/gopro_scripts/`):

```bash
# one-time setup: install the auto-revive watcher as a boot service
./install_service.sh

# --- start of mission ---
./manager_up.sh        # persistent manager (arms the cameras); reports the watcher
./manager_log.sh       # watch until both cameras are "armed & verified"
./menu.sh              # [1] start recording, then quit ([0]) -- recording continues

# ...put the AUV in the water, close SSH. Manager + watcher keep running...

# --- after the mission (reconnect SSH) ---
./menu.sh              # shows RECORDING (or it auto-resumed after a reboot); [2] stop
./manager_down.sh      # optional: stop the manager once the footage is safe
```

Recovery helper:
```bash
./revive.sh            # one-shot: power-cycle a camera confirmed off the bus
```

Use `./menu.sh` for start/stop — it's a reliable persistent client. (One-off
`ros2 service call` in a throwaway container is slow to discover the manager.)

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
`camera_labels`, `tick_period` (2 s), `strikes_before_restart` (2),
`record_grace_period` (10 s), `restart_cooldown` (8 s), `fault_after` (30 s),
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
