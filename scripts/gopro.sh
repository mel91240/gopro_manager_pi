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
SSD_DEV="${SSD_DEV:-/dev/sda1}"
SSD_MNT="${SSD_MNT:-/mnt/ssd}"
DEST="${GOPRO_DEST:-$SSD_MNT/gopro}"

manager_running() { docker ps --format '{{.Names}}' 2>/dev/null | grep -qx gopro_manager; }
ssd_mounted()     { mountpoint -q "$SSD_MNT" 2>/dev/null; }
pause()           { read -rp "  (Enter to return to menu) " _; }

ensure_dest() {
    # Mount the SSD if needed, then make sure DEST exists AND is writable by us.
    # Done every time (not only on first mount) -- /mnt/ssd is root-owned, so the
    # destination folder must be created+chowned once or python (as pi) can't write.
    if ! ssd_mounted; then
        echo ">>> mounting SSD ($SSD_DEV -> $SSD_MNT)..."
        sudo mount "$SSD_DEV" "$SSD_MNT" || { echo "!!! SSD mount failed"; return 1; }
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
    echo "  [0] Quit"
    read -rp "Command: " cmd
    case "$cmd" in
        1) if manager_running; then "$DIR/menu.sh"; else echo "  Start the manager first [4]."; fi ;;
        2) do_download ;;
        3) do_delete ;;
        4) toggle_manager ;;
        0) break ;;
        "") : ;;
        *) echo "  Unknown command." ;;
    esac
done
