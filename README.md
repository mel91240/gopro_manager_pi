# gopro_control — wired GoPro control for the AUV

Two self-contained ROS 2 packages that add GoPro recording control to the
vehicle. **No existing package is modified.** The cameras are driven over the
Open GoPro wired (USB-ethernet) API; per-camera power is switched through the
USB hub with `uhubctl`, so no GPIO or MOSFET is required.

Validated live on the vehicle Pi with 2× Hero 12 Black:
- Hero 12 runs and records **with no battery**, powered over USB.
- The powered hub really cuts Vbus on `uhubctl ... -a cycle` (camera reboots
  ~15 s) — the only reliable cure for a camera stuck in *zombie* mode
  (HTTP API answers `/state` with 200 but `/shutter/start` with 500).

## Packages

| Package | Build type | Role |
|---|---|---|
| `gopro_msgs` | `ament_cmake` | `GoProSettings.srv`, `GoProStatus.msg` |
| `gopro_control` | `ament_python` | `gopro_manager` node + ROS-free engine (`core/`) |

```
gopro_control/gopro_control/
  core/            # ROS-independent engine (importable & testable without ROS)
    camera.py      # GoPro class, discovery, uhubctl power-cycle
    settings.py    # Open GoPro setting maps + apply helper
    cli.py         # dev CLI for live testing (no ROS)
  nodes/
    gopro_manager_node.py   # thin ROS 2 wrapper
```

## Interface

Node `gopro_manager` exposes (under its private namespace):

- `~/record` — `std_srvs/SetBool` — `data: true` starts, `false` stops, on all cameras at once.
- `~/settings` — `gopro_msgs/GoProSettings` — apply resolution/fps/fov/... to all cameras.
- `~/status` — `gopro_msgs/GoProStatus` — per-camera health, published periodically.
- `~/system` — `gopro_msgs/GoProSystem` — overall state (INITIALIZING/READY/RECORDING/DEGRADED/FAULT).

## Operating on the AUV (mission flow)

The **manager** and the **menu** run as two separate processes on purpose: the
manager owns the cameras and must run for the whole mission (it survives SSH
disconnect), while the menu is a thin client you open and close at will. See
`scripts/README.md`. In short:

```bash
./scripts/manager_up.sh    # persistent manager (detached); arms the cameras
./scripts/menu.sh          # operator menu: [1] start, [2] stop — open/close any time
./scripts/manager_down.sh  # stop the manager, only after the footage is safe
```

If the manager is restarted while the cameras are recording (Pi reboot, crash),
it **detects the in-progress recording and adopts it** rather than re-arming —
so a reconnecting operator sees `RECORDING` and a second Start is refused on the
manager side (never a loud beep from shutter-on-recording).

## Watchdog & emergency signal (for the autonomy layer)

While recording, the manager watches every camera each tick and publishes the
overall state on `~/system`:

- A camera that **stops/drops out** → state `DEGRADED`; the manager keeps trying
  to recover it (re-arm out of "USB connected" + restart recording, **no power
  cycling**). A camera that merely glitched or was briefly unplugged is recovered
  as soon as it answers again, which clears the state back to `RECORDING`.
- If a camera stays unrecoverable past `fault_after` (default 30 s), or is
  reachable but its **SD is unusable**, the state becomes **`FAULT`** with a
  `MISSION COMPROMISED -- ...` message. **`FAULT` is the emergency signal**: the
  autonomy layer subscribes to `~/system` and, on `state == GoProSystem.STATE_FAULT`,
  triggers the AUV emergency surface. The manager keeps retrying even in `FAULT`,
  so the state clears itself if the camera ever comes back.

The operator menu reflects this live (it refreshes the banner the moment the
state changes, showing `DEGRADED` / `EMERGENCY` without needing a keypress).

## Deploy

1. Copy both packages into the workspace (nothing else is touched):

   ```bash
   cp -r gopro_msgs gopro_control  ~/dev/swarm-vehicle_ros2/ros2_ws/src/
   ```

2. Runtime dependency: only `uhubctl` (the Python side uses the stdlib only):

   ```bash
   sudo apt install uhubctl      # per-port USB power switching (or the .deb)
   ```

3. Build only our packages:

   ```bash
   cd ~/dev/swarm-vehicle_ros2/ros2_ws
   colcon build --packages-select gopro_msgs gopro_control
   source install/setup.bash
   ```

4. Run:

   ```bash
   ros2 launch gopro_control gopro.launch.py
   # start / stop recording:
   ros2 service call /gopro_manager/record std_srvs/srv/SetBool "{data: true}"
   ros2 service call /gopro_manager/record std_srvs/srv/SetBool "{data: false}"
   ```

## Runtime requirements (important for the container)

The node must be able to:

- **Reach the cameras** at `172.2x.y.51` and **enumerate the host's USB-ethernet
  interfaces** → run it with **host networking** (`network_mode: host`).
- **Power-cycle a stuck camera** → `uhubctl` needs host USB access and `sudo`:
  give the container `/dev/bus/usb` + `/sys` and passwordless sudo for uhubctl,
  or run the node on the host.

If power control is not available, set `enable_power_recovery: false` — recording
and settings still work, only automatic power-recovery is disabled.

## Dev CLI (no ROS)

Test the engine directly on the Pi, without ROS:

```bash
python3 -m gopro_control.core.cli discover     # list cameras + power mapping
python3 -m gopro_control.core.cli status       # battery / SD / recording
python3 -m gopro_control.core.cli record 5     # record 5 s on all cameras
python3 -m gopro_control.core.cli cycle LEFT   # Vbus power-cycle one camera
```
