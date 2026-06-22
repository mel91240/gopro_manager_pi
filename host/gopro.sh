#!/bin/bash
# Top-level operator menu for the AUV GoPro rig (host-side).
#
# One entry point for the whole workflow. Recording control runs in the ROS
# container (delegates to menu.sh); footage transfer and deletion run natively
# here on the Pi, where the SSD and the camera USB-network discovery live -- so
# no container mount/permission gymnastics.
#
#   [1] Recording menu   -> menu.sh (record / stop / settings)
#   [2] Download -> SSD   -> gopro_download.py (live %/speed/ETA)
#   [3] Delete media      -> gopro_delete.py   (live %/freed)
#   [4] Manager up/down   -> arm / disarm the cameras
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
SSD_DEV="${SSD_DEV:-}"            # empty = auto-detect; override with SSD_DEV=/dev/sdXN
SSD_MNT="${SSD_MNT:-/mnt/ssd}"
DEST="${GOPRO_DEST:-$SSD_MNT/gopro}"

manager_running() { docker ps --format '{{.Names}}' 2>/dev/null | grep -qx gopro_manager; }
ssd_mounted()     { mountpoint -q "$SSD_MNT" 2>/dev/null; }
pause()           { read -rp "  (Enter to return to menu) " _; }

detect_ssd_dev() {
    # Auto-find the SSD: the LARGEST partition (that holds a filesystem and is not
    # already mounted) on a USB-attached disk -- i.e. the external drive, not the
    # Pi's boot card. Override with SSD_DEV=/dev/sdXN if you have several USB disks.
    local best="" bestb=0 parent tran b
    while IFS= read -r line; do
        local NAME="" FSTYPE="" TYPE="" MOUNTPOINT=""
        eval "$line"
        [ "$TYPE" = part ] && [ -n "$FSTYPE" ] && [ -z "$MOUNTPOINT" ] || continue
        parent=$(lsblk -no pkname "/dev/$NAME" 2>/dev/null)
        [ -n "$parent" ] || continue
        tran=$(lsblk -dno TRAN "/dev/$parent" 2>/dev/null)
        [ "$tran" = usb ] || continue
        b=$(lsblk -bdno SIZE "/dev/$NAME" 2>/dev/null)
        [ "${b:-0}" -gt "$bestb" ] && { bestb=$b; best="/dev/$NAME"; }
    done < <(lsblk -Pno NAME,FSTYPE,TYPE,MOUNTPOINT)
    [ -n "$best" ] && { echo "$best"; return 0; }
    return 1
}

ensure_dest() {
    # Mount the SSD if needed, then make sure DEST exists AND is writable by us.
    # The mount point is root-owned, so DEST is created+chowned once.
    if ! ssd_mounted; then
        local dev="$SSD_DEV"
        if [ -z "$dev" ]; then
            dev=$(detect_ssd_dev) || {
                echo "!!! No external USB disk with a filesystem found to mount as the SSD."
                echo "    Check it is plugged in:   lsblk"
                echo "    Mount it by hand:         sudo mkdir -p $SSD_MNT && sudo mount /dev/sdXN $SSD_MNT"
                echo "    Or tell the menu which device:  SSD_DEV=/dev/sdXN ./gopro.sh"
                return 1; }
            echo ">>> auto-detected SSD: $dev"
        fi
        sudo mkdir -p "$SSD_MNT"          # create the mount point if missing (fresh image has no /mnt/ssd)
        echo ">>> mounting SSD ($dev -> $SSD_MNT)..."
        sudo mount "$dev" "$SSD_MNT" || {
            echo "!!! SSD mount failed. Diagnose / mount by hand:"
            echo "      sudo mkdir -p $SSD_MNT && sudo mount $dev $SSD_MNT"
            echo "      (check 'lsblk' for the device, 'dmesg | tail' for the reason)"
            return 1; }
    fi
    if [ ! -w "$DEST" ]; then
        sudo mkdir -p "$DEST" && sudo chown "$(id -u):$(id -g)" "$DEST" \
            || { echo "!!! could not prepare $DEST"; return 1; }
    fi
}

do_download() {
    ensure_dest || { pause; return; }
    echo ">>> Destination: $DEST"
    # Soft guard: copying during an active recording adds USB-bus load that can
    # brown-out a no-battery camera mid-record (auto-revive would then power-cycle
    # it). The downloader itself is safe to run while the manager is up, but doing
    # it DURING a mission recording is the operator's call -- ask first.
    if [ -f "$DIR/.recording_intent" ]; then
        echo "!!! A recording is IN PROGRESS (mission intent set)."
        read -rp "  Copy anyway while recording? (y/N): " yn
        [ "${yn,,}" = y ] || { echo ">>> cancelled (recording in progress)."; pause; return; }
    fi
    # No default action: an empty/unknown answer copies NOTHING. Copying every
    # clip is the slow, full-bus operation -- it must be asked for explicitly
    # (lowercase or uppercase), never triggered by a stray key or a bare Enter.
    # The manager can stay up (the robust downloader tolerates its state polls)
    # and the auto-revive watcher stays on (it only ever acts on a camera that has
    # FALLEN OFF the bus -- which doesn't happen mid-copy, and if it did a
    # power-cycle is exactly the recovery we want; the download just resumes).
    # Run the copy at low CPU priority and slightly lowered IO priority so it
    # yields to sshd, WITHOUT capping throughput (the copy is IO-bound, not CPU-
    # bound, so 'nice' costs it almost nothing; 'ionice -c2 -n7' is best-effort-
    # low, not idle, so it doesn't starve the transfer). The real anti-freeze is
    # the periodic fsync inside the downloader.
    # [a] copies all real footage; clips <2 MB (0 s test clips) are skipped by
    # design -- to include them too, use the CLI: ./download.sh --all
    read -rp "  Pick clips [p], copy all footage [a], or cancel [q]? (p/a/q): " m
    local LOW="nice -n 19 ionice -c2 -n7"
    case "${m,,}" in
        p)      GOPRO_DEST="$DEST" $LOW python3 "$DIR/gopro_download.py" --pick ;;
        a)      GOPRO_DEST="$DEST" $LOW python3 "$DIR/gopro_download.py" ;;
        q|c|"") echo ">>> cancelled (nothing copied)." ;;
        *)      echo ">>> '$m' not understood -- nothing copied. Use p / a / q." ;;
    esac
    pause
}

do_delete() {
    echo ">>> Deleting media on the cameras (IRREVERSIBLE)"
    read -rp "  Wipe all [a], pick [p], or cancel [q]? (a/p/q, default p): " m
    case "${m:-p}" in
        a)   python3 "$DIR/gopro_delete.py" --all ;;
        q|c) echo ">>> cancelled." ;;
        *)   python3 "$DIR/gopro_delete.py" --pick ;;
    esac
    pause
}

do_inspect() {
    # Show what is actually on the SSD and, optionally, verify against the cameras
    # that every clip copied in full (size-for-size) -- a manual completeness check.
    ensure_dest || { pause; return; }
    echo ">>> SSD content: $DEST"
    df -h "$SSD_MNT" 2>/dev/null | awk 'NR==1 || NR==2'
    echo
    local any=0
    if [ -d "$DEST" ]; then
        for d in "$DEST"/*/; do
            [ -d "$d" ] || continue
            any=1
            local n sz
            n=$(find "$d" -maxdepth 1 -iname '*.MP4' | wc -l)
            sz=$(du -sh "$d" 2>/dev/null | cut -f1)
            echo "  $(basename "$d") : $n clip(s), $sz"
        done
    fi
    if [ "$any" = 1 ]; then
        echo "  TOTAL footage: $(du -sh "$DEST" 2>/dev/null | cut -f1)"
    else
        echo "  (no footage copied yet)"
    fi
    echo
    # Compare every camera clip to its SSD copy (by size) -> proves nothing is missing.
    read -rp "  Verify everything is fully copied from the cameras? (y/N): " v
    [ "${v,,}" = y ] && GOPRO_DEST="$DEST" python3 "$DIR/gopro_download.py" --verify
    pause
}

toggle_manager() {
    if manager_running; then "$DIR/manager_down.sh"; else "$DIR/manager_up.sh"; fi
    pause
}

while true; do
    sd="not mounted"; ssd_mounted && sd="mounted"
    printf '\n=== AUV GoPro ===  (SSD: %s)\n' "$sd"
    echo "  [1] Recording (record / stop / settings)"
    echo "  [2] Copy videos -> SSD"
    echo "  [3] Delete / wipe cards"
    if manager_running; then echo "  [4] Stop manager"; else echo "  [4] Start manager (arm)"; fi
    echo "  [5] Inspect / verify SSD (what's copied + completeness check)"
    echo "  [0] Quit"
    read -rp "Command: " cmd
    case "$cmd" in
        1) if manager_running; then "$DIR/menu.sh"; else echo "  Start the manager first [4]."; fi ;;
        2) do_download ;;
        3) do_delete ;;
        4) toggle_manager ;;
        5) do_inspect ;;
        0) break ;;
        "") : ;;
        *) echo "  Unknown command." ;;
    esac
done
