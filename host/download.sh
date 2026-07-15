#!/bin/bash
# download.sh — offload GoPro footage -> USB SSD, reliably, on a Raspberry Pi 4 rig.
#
# Portable across Pi 4 units (same OS image): the SSD device node, log path and
# hub ports are auto-detected, not hard-coded.
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
#
# Usage:
#   ./download.sh                 # all cameras -> $DEST (default /mnt/ssd), sequential, clips >= 10 s
#   ./download.sh --minsec 0      # also grab clips shorter than 10 s
#   ./download.sh --minsec 30     # different threshold (CLI wins over the default)
# Env overrides: GOPRO_DEST, GOPRO_SSD_DEV, GOPRO_SSD_LABEL, GOPRO_MINSEC, GOPRO_LOG_DIR
set -u

DEST="${GOPRO_DEST:-/mnt/ssd}"
MINSEC="${GOPRO_MINSEC:-10}"                 # ignore clips < 10 s by default
HERE="$(cd "$(dirname "$0")" && pwd)"
LOG="${GOPRO_LOG_DIR:-$HOME}/gopro_download_$(date +%Y%m%d_%H%M%S).log"
MAX_PASSES=6
UHUBCTL="$(command -v uhubctl || echo /usr/sbin/uhubctl)"
EXTRA=("$@")

log(){ echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG" ; }

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
    log "ATTENTION: aucun quirk 'usb-storage.quirks=<VID:PID>:u' dans /proc/cmdline."
    log "  -> l'UAS n'est peut-etre pas desactive pour le SSD; sans ca le transfert peut planter le VL805."
    log "  -> Fix: ajouter usb-storage.quirks=<VID:PID>:u dans /boot/firmware/cmdline.txt puis reboot (voir README)."
  fi
}

# --- Ensure the SSD is mounted (fsck it first if it was left dirty by a crash) ---
ensure_mount(){
  if mountpoint -q "$DEST"; then return 0; fi
  local dev; dev="$(detect_ssd)" || { log "ERREUR: aucun SSD USB exFAT trouve (branche-le, ou GOPRO_SSD_DEV=...)."; exit 1; }
  log "SSD non monte -> fsck + montage de $dev sur $DEST"
  sudo fsck.exfat -y "$dev" >>"$LOG" 2>&1 || true
  sudo mkdir -p "$DEST"
  sudo mount -o uid="$(id -u)",gid="$(id -g)",umask=022 "$dev" "$DEST" \
    && log "monte: $dev -> $DEST" || { log "ECHEC montage $dev sur $DEST"; exit 1; }
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
  log "revive: power-cycle Vbus des ports GoPro"
  sudo "$UHUBCTL" 2>/dev/null | awk '
      /Current status for hub/ { hub=$5 }
      /Port [0-9]+:.*GoPro/    { p=$2; sub(":","",p); print hub, p }' \
  | while read -r hub port; do
      [ -n "$hub" ] && sudo "$UHUBCTL" -l "$hub" -p "$port" -a cycle -d 15 >>"$LOG" 2>&1
    done
  log "revive: attente re-arm des cameras (~30s)"
  sleep 30
}

count(){ find "$DEST" -type f -name '*.MP4' 2>/dev/null | wc -l ; }

# --- main -------------------------------------------------------------------
check_uas
ensure_mount
cd "$HERE"; export PYTHONPATH="$HERE"
log "cible: $DEST | mode: SEQUENTIEL (parallele interdit sur ce Pi 4) | minsec: $MINSEC"

prev=-1
for pass in $(seq 1 "$MAX_PASSES"); do
  log "=== passe $pass / $MAX_PASSES ==="
  # --minsec BEFORE EXTRA so a user-supplied --minsec on the CLI wins (argparse: last one applies)
  python3 "$HERE/gopro_download.py" --sequential --minsec "$MINSEC" --dest "$DEST" "${EXTRA[@]}" 2>&1 | tee -a "$LOG"
  now=$(count)
  log "passe $pass: $now fichiers MP4 sur le SSD"

  if [ "$now" -gt "$prev" ]; then
    prev="$now"; continue                 # progress -> keep going
  fi
  if cameras_ok; then
    log "aucun nouveau fichier et les 2 cameras repondent -> TERMINE"
    break
  fi
  log "aucun progres ET une camera ne repond plus -> revive puis nouvelle passe"
  revive_cameras
  prev="$now"
done

log "=== BILAN: $(count) fichiers MP4, $(du -sh "$DEST" 2>/dev/null | cut -f1) sur $DEST ==="
log "log complet: $LOG"
