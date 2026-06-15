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
pause()           { read -rp "  (Entrée pour revenir au menu) " _; }

ensure_dest() {
    # Mount the SSD if needed, then make sure DEST exists AND is writable by us.
    # Done every time (not only on first mount) -- /mnt/ssd is root-owned, so the
    # destination folder must be created+chowned once or python (as pi) can't write.
    if ! ssd_mounted; then
        echo ">>> montage du SSD ($SSD_DEV -> $SSD_MNT)..."
        sudo mount "$SSD_DEV" "$SSD_MNT" || { echo "!!! échec du montage du SSD"; return 1; }
    fi
    if [ ! -w "$DEST" ]; then
        sudo mkdir -p "$DEST" && sudo chown "$(id -u):$(id -g)" "$DEST" \
            || { echo "!!! impossible de préparer $DEST"; return 1; }
    fi
}

do_download() {
    ensure_dest || { pause; return; }
    echo ">>> Destination : $DEST"
    read -rp "  Tout copier [a], choisir [p], ou annuler [q] ? (a/p/q, défaut a) : " m
    case "${m:-a}" in
        q|c) echo ">>> annulé."; pause; return ;;
    esac
    # The manager can stay up (the robust downloader tolerates its state polls)
    # and the auto-revive watcher stays on (it only ever acts on a camera that has
    # FALLEN OFF the bus -- which doesn't happen mid-copy, and if it did a
    # power-cycle is exactly the recovery we want; the download just resumes).
    if [ "$m" = "p" ]; then
        GOPRO_DEST="$DEST" python3 "$DIR/gopro_download.py" --pick
    else
        GOPRO_DEST="$DEST" python3 "$DIR/gopro_download.py"
    fi
    pause
}

do_delete() {
    echo ">>> Suppression média sur les caméras (IRRÉVERSIBLE)"
    read -rp "  Tout vider [a], choisir [p], ou annuler [q] ? (a/p/q, défaut p) : " m
    case "${m:-p}" in
        a)   python3 "$DIR/gopro_delete.py" --all ;;
        q|c) echo ">>> annulé." ;;
        *)   python3 "$DIR/gopro_delete.py" --pick ;;
    esac
    pause
}

toggle_manager() {
    if manager_running; then "$DIR/manager_down.sh"; else "$DIR/manager_up.sh"; fi
    pause
}

while true; do
    st="DOWN"; manager_running && st="UP"
    sd="non montée"; ssd_mounted && sd="montée"
    printf '\n=== AUV GoPro ===  (manager: %s | SSD: %s)\n' "$st" "$sd"
    echo "  [1] Enregistrement (record / stop / settings)"
    echo "  [2] Copier les vidéos -> SSD"
    echo "  [3] Supprimer / vider les cartes"
    if manager_running; then echo "  [4] Arrêter le manager"; else echo "  [4] Démarrer le manager (armer)"; fi
    echo "  [0] Quitter"
    read -rp "Commande : " cmd
    case "$cmd" in
        1) if manager_running; then "$DIR/menu.sh"; else echo "  Démarre d'abord le manager [4]."; fi ;;
        2) do_download ;;
        3) do_delete ;;
        4) toggle_manager ;;
        0) break ;;
        "") : ;;
        *) echo "  Commande inconnue." ;;
    esac
done
