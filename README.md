# 📸 gopro_manager_pi — Control GoPros on a Raspberry Pi 4

Drive a pair of **GoPro Hero 12** cameras from a Raspberry Pi over wired USB:
record, change settings, offload and wipe footage — all under software control,
no one touching the cameras. Built for an AUV, so the recording subsystem is
**autonomous and reboot-proof**. The cameras run **with no battery** (USB-powered)
over the **wired Open GoPro HTTP API**.

Two identical cameras are labelled by **USB socket position** — **LEFT** and
**RIGHT** — never by serial number, so a swapped-in camera in the same port just
works.

---

## 1. Setup (once, on a freshly flashed Pi)

**a. Flash** the SD card with the latest BlueOS image (verify the write).

**b. Give the Pi internet** (adapt to your WiFi):
```bash
sudo nmcli device wifi connect 'wifi_name' password 'wifi_password' ifname wlan0
ping -c2 8.8.8.8            # internet
ping -c2 github.com        # + DNS
```

**c. Set the clock (UTC).** The Pi has no RTC — the clock resets every boot, and a
wrong clock breaks TLS (`git`/`apt` fail with confusing errors):
```bash
sudo timedatectl set-ntp true                  # auto-sync, or set it by hand:
sudo date -u -s "2026-07-21 08:00:00"          # UTC = French time − 2h in summer
```

**d. Install uhubctl** (the auto-revive watcher power-cycles cameras through it):
```bash
sudo apt update && sudo apt install -y uhubctl
command -v uhubctl         # prints a path if installed
```

**e. Clone + install** (needs the base AUV setup already present: the
`cosma_auv:latest` image + the `~/dev/swarm-vehicle` workspace):
```bash
git clone https://github.com/mel91240/gopro_manager_pi.git
cd gopro_manager_pi
./install.sh               # builds the ROS packages, installs the host scripts + boot services
```

**f. ⚠️ `setup.sh` — do NOT skip it.** Plug the **SSD into a USB 3.0 port**, then:
```bash
./setup.sh                 # detects the SSD, disables UAS (usb-storage/BOT mode), prints the next step
sudo reboot                # required for the change to take effect
```
Without this, the SSD runs in UAS mode and **drops off the bus mid-offload** (the
download fails). After the reboot, re-set the clock if NTP hasn't synced.

**g. Verify** (after the reboot):
```bash
grep -o "usb-storage.quirks=[^ ]*" /proc/cmdline               # the quirk is present
lsusb -t | grep "Mass Storage"                                 # Driver=usb-storage  (= BOT, NOT uas)
systemctl is-active gopro-manager gopro-autorevive gopro-logfile   # 3× "active"
~/dev/swarm-vehicle/gopro_scripts/gopro_ctl.sh status          # READY, ready 2/2
```

**h. Prepare the GoPros — GoPro Labs firmware (once per camera).** The cameras must
run **GoPro Labs** firmware, version **02.32.70**, set to **auto power-on when USB
power is applied** (`WAKE=2`) and to **trust USB power / assume it is always
sufficient** (`TUSB=1`, so they run USB-powered, no battery). To flash: put the Labs
`update/` folder on the SD card and boot the camera, then scan the QR codes for those
settings (`WAKE=2`, `TUSB=1`) generated at <https://gopro.github.io/labs/control/custom/>.

**i. Wire the cameras.** Plug the **Mega 4 hub** into the Pi and power it (5 V), plug
the **2 GoPros** into the hub. LEFT and RIGHT are decided by **which port**, not the
camera — see the wiring photo. Cameras arm themselves on power-up.

> **Update later:** `git pull && ./install.sh`. **Remove:** `./uninstall.sh` (footage is never touched).
> Non-default paths: `GOPRO_WS=/path COSMA_IMAGE=img:tag ./install.sh`.

---

## 2. Control the GoPros

Everything runs from `~/dev/swarm-vehicle/gopro_scripts/`:
```bash
cd ~/dev/swarm-vehicle/gopro_scripts/
```
The manager runs as a boot service, so you normally never start it by hand.
**Always check `./gopro_ctl.sh status` or `./manager_log.sh` after an action.**

**Status**
```bash
./gopro_ctl.sh status      # state (INITIALIZING/READY/RECORDING/EMERGENCY), mode (duo/solo),
                           # SD time left per camera, cameras detected, date/time
```

**Record**
```bash
./gopro_ctl.sh start       # start recording on all cameras   (alias: record)
./gopro_ctl.sh stop        # stop
```

**Settings** — pass only the fields you want to change:
```bash
./gopro_ctl.sh settings resolution=4K fps=30 fov=Linear
```
`./gopro_ctl.sh settings` alone (or an invalid value) lists all valid options.
Some combos are incompatible (it tells you). **5.3K asks for a `y` confirmation** —
without a battery it can be too much for the USB power (brown-out).

**Camera power mode**
```bash
./gopro_ctl.sh solo LEFT   # keep only LEFT on, power RIGHT off (or 'solo RIGHT')
./gopro_ctl.sh duo         # both cameras on   (alias: on)
./gopro_ctl.sh off         # both cameras off (idle, anti-overheat) until 'on'
```
`solo` is useful if one camera misbehaves (continue the survey on one), or to
reboot a single camera.

**Download footage → SSD**
```bash
./download.sh              # copy all cards to the SSD (resumable, skips clips already there / under 10 s)
./download.sh --verify     # check every clip has a same-size copy on the SSD (read-only)
```
> ⚠️ **Supervise a download over ETHERNET.** The copy is Pi-local (cameras → SSD) and
> always finishes, but the USB3 activity kills the 2.4 GHz WiFi mid-copy — you lose
> your WiFi SSH (not the download). Best offload option: detachable GoPro cables →
> plug the cameras straight into a computer (faster, more robust, no WiFi).

**Delete footage** (once the offload is confirmed safe)
```bash
./gopro_delete.py --pick   # choose clips to delete
./gopro_delete.py --all    # wipe the cards  (type "all" to confirm)
```

**Logs**
```bash
./manager_log.sh                        # live manager + auto-revive + download stream
ls ~/dev/swarm-vehicle/log/gopro/        # history: one dated file per manager session (latest.log = current)
```

**Manager control** (normally automatic)
```bash
./manager_up.sh  /  ./manager_down.sh    # manual start/stop (up delegates to the service, returns at once)
# or: sudo systemctl restart gopro-manager
```

---

## 3. A usual run

1. Turn on the drone → the cameras auto-arm. Open `./manager_log.sh`.
2. `./gopro_ctl.sh status` — check **READY 2/2**, the SD time left, and the UTC clock.
   - Not ready after a moment / a camera missing? Try `solo` then `duo` (turn cameras
     on one at a time), and read `status` + `manager_log`.
3. Set the parameters, then `./gopro_ctl.sh start`.
   - Lose a camera at start? Check the power budget and that the settings aren't too
     high for the USB rail (5.3K).
4. Put the drone in the water, close SSH — the manager keeps running. If it films but
   something goes wrong, the code auto-tries to recover the camera; if it can't, it
   raises **EMERGENCY** (state `FAULT`) so the autonomy can react. The operator
   chooses to continue on one camera or stop.
5. After the dive: `./gopro_ctl.sh stop` → `./download.sh` → `./gopro_delete.py --all`
   once the footage is safe → turn the cameras `off` or power off the drone.

---

## 4. How it works

Three pieces, deliberately separate:

| Piece | Where | Role |
|---|---|---|
| **`gopro_manager`** | Docker (`cosma_auv` image) | owns the cameras: arm, record, watchdog, auto-resume, EMERGENCY |
| **auto-revive watcher** (`revive.sh`) | host (systemd) | power-cycle a camera that fell **off the bus** (Vbus lives on the host) |
| **host scripts** (`gopro_ctl.sh` / `download.sh` / `gopro_delete.py`) | host | operator control, offload, wipe |

The system spans two worlds — **Docker** (where ROS 2 lives) and the **host**
(where `uhubctl` and the SSD live):

```
   gopro_ctl.sh                    (operator control, runs on the HOST)
        │
        │  ros2 service call  —  via `docker exec` into the container
        ▼
   gopro_manager                   (the "brain", runs in DOCKER / cosma_auv image)
        │                           arm · record · watchdog · auto-resume · EMERGENCY
        │
        │  needs a camera power-cycled?  →  writes  ".revive_request"  (just a file)
        ▼
   revive.sh                       (the watcher, runs on the HOST — has uhubctl + SSD)
        │                           reads the file → uhubctl cuts that socket's USB power
        ▼
   USB hub  ──►  GoPro LEFT   +   GoPro RIGHT
                 each = a USB-Ethernet device · wired Open GoPro HTTP API on :8080

   offload / wipe:  download.sh · gopro_delete.py  ──HTTP :8080──►  cameras  ──►  SSD
```

**Why the split:** ROS nodes need the ROS image → Docker; `uhubctl` (USB power) and
the SSD need host access → host. `gopro_ctl.sh` runs `docker exec … ros2 service
call` *into* the manager container, so the manager (and its watchdog) always stays
in charge. The manager (no uhubctl in Docker) asks for a power-cycle by writing
`hub:port` to `.revive_request`; `revive.sh` (host) reads it and cuts that socket's
Vbus — an off-bus camera is revived without giving the container USB-power rights.

### Resilience (autonomous)
- **Adopt in-progress recording** — if the manager restarts while filming, it adopts the recording (no re-arm, no beep).
- **Watchdog** — a dropped camera → `DEGRADED` while the first recovery (re-arm + restart, **no power-cut**) is in flight; recovers as soon as it answers again.
- **EMERGENCY** (`FAULT` = the AUV should hold/stop) — raised when recovery keeps failing, all cameras drop at once, a camera's SD is unusable, or one stays lost past `fault_after_seconds`. Self-clears the instant a camera films again.
- **Auto-resume after reboot** — a persistent intent flag resumes recording (a new timestamped segment) after an involuntary reboot.
- **Auto-revive** — a camera **off the USB bus** is power-cycled by the host watcher, then re-armed by the manager. A camera that stays **on the bus but HTTP-mute** is also Vbus-cycled after a longer, guarded delay.

### Safety (SD card)
Vbus is cut **only** on a camera confirmed **off the bus** (or capture-dead) for
several scans — never a visible/recording camera, never on a 1 s blip. An off-bus
camera is idle, so its SD card cannot be corrupted.

---

## 5. ROS 2 interface

Node `gopro_manager` (private namespace):

| Interface | Type | Purpose |
|---|---|---|
| `~/record` | `std_srvs/SetBool` | `true` start / `false` stop, all cameras (barrier-synced) |
| `~/settings` | `gopro_msgs/GoProSettings` | apply capture settings |
| `~/status` | `gopro_msgs/GoProStatus` | per-camera health, each tick |
| `~/system` | `gopro_msgs/GoProSystem` | overall state: `INITIALIZING`/`READY`/`RECORDING`/`DEGRADED`/`FAULT` |

`FAULT` is the **EMERGENCY** signal for the autonomy layer (`state == GoProSystem.STATE_FAULT`).

```
   INITIALIZING ──(all armed)──► READY ──start──► RECORDING ──stop──► READY
      (booting)                                      │
                                                     │  a camera drops
                                                     ▼
                                                  DEGRADED   (recovering; AUV may slow down)

   FAULT / EMERGENCY  →  the AUV should hold. Raised when recovery keeps failing,
   all cameras drop at once, an SD is unusable, or a camera is lost too long.
   It self-clears the instant a camera films again. (A real Vbus power-cycle is
   requested only for an off-bus / brown-out / HTTP-mute camera.)
```

**Parameters** (`params/gopro_params.yaml`, loaded at manager (re)start):
`camera_labels`, `tick_period` (1 s), `fault_after_attempts` (9),
`unreliable_after` (3, drop episodes/take → EMERGENCY), `restart_cooldown` (5 s),
`fault_after_seconds` (45 s), `resume_on_restart` (true). Vbus recovery:
`vbus_recover_after` (3, brown-out), `vbus_recover_unreachable_after` (5, HTTP-mute),
`vbus_cooldown` (45 s).

---

## 6. Repo layout

```
install.sh / setup.sh / uninstall.sh   # one-command add / SSD-quirk / remove
ros2_pkgs/                  # the ROS half -> built in the cosma_auv container
  gopro_control/gopro_control/
    core/          # ROS-independent engine (urllib only, no extra deps)
      camera.py    # GoPro class: discovery, state, control, Vbus (uhubctl)
      settings.py  # Open GoPro setting maps + validation + ordered apply
      cli.py       # dev CLI for live testing without ROS
    nodes/gopro_manager_node.py   # the manager node (the "brain")
  gopro_msgs/      # GoProStatus.msg, GoProSystem.msg, GoProSettings.srv
host/                        # the HOST half -> installed into gopro_scripts/
  gopro_ctl.sh              # operator control: start/stop/status/settings/solo/duo/off/on
  download.sh + gopro_download.py   # offload footage -> SSD (robust, resumable)
  gopro_delete.py           # delete / wipe media on the cameras
  revive.sh                 # auto-revive watcher (--watch); uses host uhubctl
  manager_up.sh / manager_down.sh / manager_log.sh / manager_logfile.sh
  systemd/                  # gopro-manager / gopro-autorevive / gopro-logfile TEMPLATES
```

---

## 7. Hardware notes / lessons learned
- **USB cable / uplink matters most.** The Hero 12 is a USB 2.0 device. A marginal or USB-3 uplink to the hub forces a flaky SuperSpeed link that drops under recording load — the **#1 cause of "cameras drop mid-record"**. Use good, firmly-seated cables.
- **SSD on USB 3.0 + BOT (the `setup.sh` quirk).** With the UAS quirk applied, a 10 GB offload runs clean at ~35 MB/s. Without it (UAS active), the SSD drops off the bus and the download fails — that's exactly what `setup.sh` fixes.
- **Power budget.** Two Hero 12 at 4K draw a lot (no battery, all on USB). A marginal hub supply browns out into a zombie state. Use a solid **5 V / 4–5 A+** supply.
- **5K / 5.3K needs care.** Without a battery the USB rail may not sustain 5.3K capture → brown-out zombie (answers `/state`, refuses `/shutter`). The CLI asks for a `y` confirmation; stay at **4K** unless the cameras are on battery.
- **A wedged hub survives a Pi reboot** (it is self-powered): if all cameras drop at once and `dmesg` shows repeated `usb … error -110`, physically power-cycle the hub.
- **Reboot interrupts recording** (USB bus reset), but the GoPro recovers the in-progress file and the manager auto-resumes a new segment.
