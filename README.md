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

The repo is laid out to mirror the deployment, so `install.sh` just drops each
half in place (ROS packages -> the workspace, host scripts -> `gopro_scripts/`):

```
install.sh / uninstall.sh   # one-command add/remove on any base-image Pi
ros2_pkgs/                  # the ROS half -> built in the cosma_auv container
  gopro_control/gopro_control/
    core/          # ROS-independent engine (urllib only, no extra deps)
      camera.py    # GoPro class: discovery, state, control
      settings.py  # Open GoPro setting maps + validation + ordered apply
      cli.py       # dev CLI for live testing without ROS
    nodes/
      gopro_manager_node.py   # the manager node
      pi_menu_node.py         # the recording console
  gopro_msgs/      # GoProStatus.msg, GoProSystem.msg, GoProSettings.srv
host/                        # the HOST half -> installed into gopro_scripts/
  gopro.sh                  # unified operator menu (entry point): record / offload / wipe / manager
  manager_up.sh / manager_down.sh / manager_log.sh / menu.sh
  revive.sh                 # one-shot or --watch auto-revive (uses host uhubctl)
  gopro_download.py         # offload footage -> SSD (robust, resumable, live progress)
  gopro_delete.py           # delete / wipe media on the cameras
  systemd/                  # gopro-manager / gopro-autorevive service TEMPLATES (paths filled in at install)
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

One-time setup builds the packages, installs the host scripts + boot services,
so the manager + watcher start automatically on power-up and the cameras arm
themselves — no manual step:

```bash
./install.sh             # build + host scripts + sudoers + boot services (idempotent)
```

`gopro_scripts/gopro.sh` is then the single operator entry point (run on the Pi):

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
`fault_after_attempts` (2), `record_grace_period` (10 s), `restart_cooldown`
(0 s), `fault_after` (30 s), `discovery_timeout` (20 s), `resume_on_restart`
(true), `state_file`. Brown-out Vbus recovery: `vbus_recover_after` (3),
`vbus_cooldown` (45 s), `vbus_request_file`. The file is loaded at runtime
(`manager_up.sh` passes it with `--params-file`), so editing a value takes effect
on the next manager restart.

---

## Install on another Pi

The GoPro rig is a **self-contained add-on**: it adds two ROS packages and a
`gopro_scripts/` folder, modifies **no** existing AUV package, and needs **no
Docker image change** (the ROS code is built into the mounted workspace, not
baked into the image). So any Pi that already has the base AUV setup (the
`cosma_auv` image + the `swarm-vehicle` workspace) gets the cameras with:

```bash
git clone <repo> && cd gopro_manager_pi
./install.sh                       # detects the workspace; build + scripts + services
# non-default locations:
GOPRO_WS=/path/to/swarm-vehicle COSMA_IMAGE=myimg:tag ./install.sh
./uninstall.sh                     # remove (footage is never touched; --purge also drops packages)
```

`install.sh` is idempotent (re-run to update) and does, on the target Pi:
1. copy `ros2_pkgs/gopro_control` + `gopro_msgs` into `<ws>/ros2_ws/src/`;
2. `colcon build` them inside the `cosma_auv` container (ROS 2 Humble lives only
   in the image, not on the host);
3. install the `host/` scripts into `<ws>/gopro_scripts/`;
4. install the `uhubctl` sudoers rule (the watcher cuts Vbus via `sudo -n uhubctl`);
5. install + enable the two systemd services (paths/user injected from the
   `host/systemd/*.in` templates — nothing is hard-coded to one Pi).

The manager + menu containers use `--network host -e ROS_DOMAIN_ID=0
-e ROS_LOCALHOST_ONLY=1` plus a FastDDS UDP-only profile
(`FASTRTPS_DEFAULT_PROFILES_FILE=.../fastdds_udp_only.xml`). `--ipc host` is
deliberately NOT used: each container has a private `/dev/shm`, so shared-memory
DDS would discover topics yet deliver no data (and leak SHM on `docker rm -f`);
UDP-localhost is robust for these tiny messages. The scripts set these.

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
