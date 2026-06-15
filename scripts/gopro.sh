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
WATCHER=gopro-autorevive.service

manager_running() { docker ps --format '{{.Names}}' 2>/dev/null | grep -qx gopro_manager; }
ssd_mounted()     { mountpoint -q "$SSD_MNT" 2>/dev/null; }
pause()           { read -rp "  (Entrée pour revenir au menu) " _; }

mount_ssd() {
    if ssd_mounted; then return 0; fi
    echo ">>> montage du SSD ($SSD_DEV -> $SSD_MNT)..."
    sudo mount "$SSD_DEV" "$SSD_MNT" || { echo "!!! échec du montage"; return 1; }
    sudo mkdir -p "$DEST" && sudo chown "$(id -u):$(id -g)" "$DEST"
}

do_download() {
    mount_ssd || { pause; return; }
    echo ">>> Destination : $DEST"
    read -rp "  Tout copier [a] ou choisir [p] ? (a/p, défaut a) : " m
    echo ">>> (watcher en pause le temps du transfert)"
    sudo systemctl stop "$WATCHER" 2>/dev/null
    if [ "${m:-a}" = "p" ]; then
        GOPRO_DEST="$DEST" python3 "$DIR/gopro_download.py" --pick
    else
        GOPRO_DEST="$DEST" python3 "$DIR/gopro_download.py"
    fi
    sudo systemctl start "$WATCHER" 2>/dev/null
    pause
}

do_delete() {
    echo ">>> Suppression média sur les caméras (IRRÉVERSIBLE)"
    read -rp "  Tout vider [a], choisir [p], ou annuler [c] ? (a/p/c, défaut p) : " m
    case "${m:-p}" in
        a) python3 "$DIR/gopro_delete.py" --all ;;
        c) echo ">>> annulé." ;;
        *) python3 "$DIR/gopro_delete.py" --pick ;;
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
