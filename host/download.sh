#!/bin/bash
# download.sh -- offload GoPro footage -> USB SSD, reliably, on a Raspberry Pi 4 rig.
#
# Portable across Pi 4 units (same OS image): the SSD device node and hub ports
# are auto-detected, not hard-coded.
#
# Hard-won lessons baked in (see README / crash notes):
#  * The Pi 4 has ONE USB3 controller (VL805) shared by the SSD + both cameras.
#    Downloading both cameras in PARALLEL wedges it under load (SSD drops off the
#    bus, cameras go "No route to host", needs a reboot). Confirmed crashing at
#    parallel uncapped AND capped; the VL805 firmware is already the latest.
#    => we FORCE --sequential (one camera at a time, ~35 MB/s, rock-solid).
#  * The SSD MUST run with UAS disabled (usb-storage.quirks=<VID:PID>:u in
#    /boot/firmware/cmdline.txt) or UAS aborts wedge the same controller. The
#    script WARNS if that quirk is missing.
#  * Cameras answer HTTP, not ICMP ping. Transfers are resumable: files already
#    on the SSD are skipped, so re-running is always safe and just continues.
#  * Progress + results go to journald (SyslogIdentifier "download"), NOT to a
#    file in $HOME: watch them live -- grouped with the manager & auto-revive --
#    via ./manager_log.sh.
#
# Usage:
#   ./download.sh                 # all cameras -> $DEST (default /mnt/ssd), sequential, clips >= 10 s
#   ./download.sh --minsec 0      # also grab clips shorter than 10 s
#   ./download.sh --minsec 30     # different threshold (CLI wins over the default)
# Env overrides: GOPRO_DEST, GOPRO_SSD_DEV, GOPRO_SSD_LABEL, GOPRO_MINSEC, GOPRO_LOGTAG
set -u

DEST="${GOPRO_DEST:-/mnt/ssd}"
MINSEC="${GOPRO_MINSEC:-10}"                 # ignore clips < 10 s by default
HERE="$(cd "$(dirname "$0")" && pwd)"
MAX_PASSES=6
UHUBCTL="$(command -v uhubctl || echo /usr/sbin/uhubctl)"
TAG="${GOPRO_LOGTAG:-download}"           # journald SyslogIdentifier (grouped in ./manager_log.sh)
EXTRA=("$@")

# Every line goes to the terminal AND to journald under "$TAG", so the whole run
# is visible live via ./manager_log.sh (next to the manager & auto-revive) -- and
# no log file is dropped in $HOME any more.
log(){ echo "[$(date +%H:%M:%S)] $*"; logger -t "$TAG" -- "$*"; }
# Forward a command's output: show it on the terminal + send each line to journald.
logpipe(){ while IFS= read -r line; do printf '%s\n' "$line"; logger -t "$TAG" -- "$line"; done; }

# --- Find the USB SSD partition (don't hard-code /dev/sda1: enumeration varies) ---
detect_ssd(){
  # 1) explicit override
  if [ -n "${GOPRO_SSD_DEV:-}" ]; then echo "$GOPRO_SSD_DEV"; return 0; fi
  # 2) by filesystem label if provided
  if [ -n "${GOPRO_SSD_LABEL:-}" ]; then
    local d; d="$(blkid -L "$GOPRO_SSD_LABEL" 2>/dev/null)" && [ -n "$d" ] && { echo "$d"; return 0; }
  fi
  # 3) first exFAT partition sitting on a USB-attached disk
  local disk part
  for disk in $(lsblk -rpno NAME,TRAN,TYPE | awk '$2=="usb" && $3=="disk"{print $1}'); do
    part="$(lsblk -rpno NAME,FSTYPE,TYPE "$disk" | awk '$3=="part" && $2=="exfat"{print $1; exit}')"
    [ -n "$part" ] && { echo "$part"; return 0; }
  done
  return 1
}

# --- Warn if UAS is not disabled (the #1 stability requirement on Pi 4) ---
check_uas(){
  if ! grep -qE 'usb-storage\.quirks=[^ ]*:u' /proc/cmdline; then
    log "WARNING: no 'usb-storage.quirks=<VID:PID>:u' quirk in /proc/cmdline."
    log "  -> UAS may not be disabled for the SSD; without it the transfer can wedge the VL805."
    log "  -> Fix: add usb-storage.quirks=<VID:PID>:u to /boot/firmware/cmdline.txt then reboot (see README)."
  fi
}

# --- Ensure the SSD is mounted (fsck it first if it was left dirty by a crash) ---
ensure_mount(){
  if mountpoint -q "$DEST"; then return 0; fi
  local dev; dev="$(detect_ssd)" || { log "ERROR: no USB exFAT SSD found (plug it in, or set GOPRO_SSD_DEV=...)."; exit 1; }
  log "SSD not mounted -> fsck + mount $dev on $DEST"
  sudo fsck.exfat -y "$dev" 2>&1 | logpipe
  sudo mkdir -p "$DEST"
  sudo mount -o uid="$(id -u)",gid="$(id -g)",umask=022 "$dev" "$DEST" \
    && log "mounted: $dev -> $DEST" || { log "FAILED to mount $dev on $DEST"; exit 1; }
}

# --- Both cameras reachable over HTTP right now? (ping is useless for GoPro) ---
cameras_ok(){
  PYTHONPATH="$HERE" python3 - <<'PY' 2>/dev/null
import sys, gopro_download as g
try:
    cams = g.discover()
    if not cams: sys.exit(1)
    for lab, ip, _ in cams:
        g.media_list(ip)          # raises if that camera is wedged / unreachable
    sys.exit(0)
except Exception:
    sys.exit(1)
PY
}

# --- Vbus power-cycle every GoPro port (recovers a camera whose NCM link is dead) ---
revive_cameras(){
  log "revive: power-cycle Vbus of the GoPro ports"
  sudo "$UHUBCTL" 2>/dev/null | awk '
      /Current status for hub/ { hub=$5 }
      /Port [0-9]+:.*GoPro/    { p=$2; sub(":","",p); print hub, p }' \
  | while read -r hub port; do
      [ -n "$hub" ] && sudo "$UHUBCTL" -l "$hub" -p "$port" -a cycle -d 15 2>&1 | logpipe
    done
  log "revive: waiting for cameras to re-arm (~30s)"
  sleep 30
}

count(){ find "$DEST" -type f -name '*.MP4' 2>/dev/null | wc -l ; }

# --- main -------------------------------------------------------------------
check_uas
ensure_mount
cd "$HERE"; export PYTHONPATH="$HERE"
log "target: $DEST | mode: SEQUENTIAL (parallel forbidden on this Pi 4) | minsec: $MINSEC"

prev=-1
for pass in $(seq 1 "$MAX_PASSES"); do
  log "=== pass $pass / $MAX_PASSES ==="
  # --minsec BEFORE EXTRA so a user-supplied --minsec on the CLI wins (argparse: last one applies)
  python3 -u "$HERE/gopro_download.py" --sequential --minsec "$MINSEC" --dest "$DEST" "${EXTRA[@]}" 2>&1 | logpipe
  now=$(count)
  log "pass $pass: $now MP4 files on the SSD"

  if [ "$now" -gt "$prev" ]; then
    prev="$now"; continue                 # progress -> keep going
  fi
  if cameras_ok; then
    log "no new file and both cameras respond -> DONE"
    break
  fi
  log "no progress AND a camera stopped responding -> revive then new pass"
  revive_cameras
  prev="$now"
done

log "=== SUMMARY: $(count) MP4 files, $(du -sh "$DEST" 2>/dev/null | cut -f1) on $DEST ==="
log "done -- full log grouped in ./manager_log.sh (identifier 'download')."
