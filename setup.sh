#!/bin/bash
# setup.sh -- one-time host prep for the SSD offload on a Raspberry Pi 4.
#
# Disables UAS for the USB SSD by adding `usb-storage.quirks=<VID:PID>:u` to the
# kernel command line, so the drive runs in BOT (Bulk-Only) mode. WHY: the Pi 4's
# single VL805 USB3 controller is shared by the SSD and the cameras; with UAS
# enabled, heavy SSD writes during an offload wedge the whole controller (SSD drops
# off the bus, cameras go unreachable, needs a reboot). Forcing BOT fixes it.
#
# Safe by construction: auto-detects the SSD's VID:PID (override with
# GOPRO_SSD_VIDPID=<vid:pid>), backs up cmdline.txt, is idempotent, and MERGES into
# an existing usb-storage.quirks= list instead of adding a second one (a second
# would silently override the first). Takes effect after a reboot.
#
#   ./setup.sh            # with the SSD plugged in
#   GOPRO_SSD_VIDPID=0781:55ae ./setup.sh    # skip auto-detection
set -euo pipefail

CMDLINE="${CMDLINE:-/boot/firmware/cmdline.txt}"

say() { echo ">>> $*"; }
die() { echo "!!! $*" >&2; exit 1; }

[ -f "$CMDLINE" ] || die "no $CMDLINE (Raspberry Pi OS boot cmdline). Set CMDLINE= if it lives elsewhere (e.g. /boot/cmdline.txt)."

# --- 1. find the USB SSD's VID:PID -------------------------------------------
VIDPID="${GOPRO_SSD_VIDPID:-}"
if [ -z "$VIDPID" ]; then
    disk="$(lsblk -rpno NAME,TRAN,TYPE | awk '$2=="usb" && $3=="disk"{print $1; exit}')"
    [ -n "$disk" ] || die "no USB disk found (plug the SSD in, or set GOPRO_SSD_VIDPID=<vid:pid>)."
    props="$(udevadm info -q property -n "$disk")"
    vid="$(sed -n 's/^ID_VENDOR_ID=//p' <<<"$props")"
    pid="$(sed -n 's/^ID_MODEL_ID=//p'  <<<"$props")"
    [ -n "$vid" ] && [ -n "$pid" ] || die "could not read the USB VID:PID of $disk (set GOPRO_SSD_VIDPID=<vid:pid>)."
    VIDPID="$vid:$pid"
    say "USB SSD detected: $disk  (VID:PID $VIDPID)"
fi

# --- 2. already done? --------------------------------------------------------
if grep -qE "usb-storage\.quirks=[^ ]*${VIDPID}:u" "$CMDLINE"; then
    say "already set: usb-storage.quirks for ${VIDPID}:u is in $CMDLINE. Nothing to do."
    exit 0
fi

# --- 3. back up, then add/merge the quirk (cmdline.txt is ONE line) ----------
bak="$CMDLINE.bak-$(date +%Y%m%d_%H%M%S)"
sudo cp "$CMDLINE" "$bak"
say "backed up $CMDLINE -> $bak"

if grep -qE "usb-storage\.quirks=" "$CMDLINE"; then
    # merge into the existing comma-separated list rather than adding a 2nd param
    sudo sed -i "1 s|\(usb-storage\.quirks=[^ ]*\)|\1,${VIDPID}:u|" "$CMDLINE"
    say "merged ${VIDPID}:u into the existing usb-storage.quirks list"
else
    sudo sed -i "1 s|\$| usb-storage.quirks=${VIDPID}:u|" "$CMDLINE"
    say "added usb-storage.quirks=${VIDPID}:u"
fi

say "DONE. Reboot for it to take effect:   sudo reboot"
say "After reboot, verify:   dmesg | grep -i 'UAS is ignored'   (the SSD now runs in BOT mode)"
say "To undo:   sudo cp '$bak' '$CMDLINE'   then reboot"
