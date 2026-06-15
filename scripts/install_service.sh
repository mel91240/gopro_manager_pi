#!/bin/bash
# Install the GoPro systemd services so they start automatically on every boot
# (and restart if they ever die). Run once. Needs sudo.
#
#   gopro-autorevive.service  -- watcher: power-cycle a camera that fell off the bus
#   gopro-manager.service     -- manager: arm cameras, recording, watchdog/EMERGENCY
#
# After this, powering on the AUV arms the cameras by itself -- no manual
# manager_up. You can still stop/start either with systemctl, or use the menu.
#
#   ./install_service.sh
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICES="gopro-autorevive.service gopro-manager.service"

for SVC in $SERVICES; do
    echo ">>> installing $SVC ..."
    sudo cp "$DIR/$SVC" "/etc/systemd/system/$SVC"
done
sudo systemctl daemon-reload
for SVC in $SERVICES; do
    sudo systemctl enable "$SVC"
    sudo systemctl restart "$SVC"
done
sleep 2
for SVC in $SERVICES; do
    sudo systemctl --no-pager --full status "$SVC" | head -4
    echo
done
echo ">>> done. Watcher + manager now start automatically on every boot."
echo ">>>   manager logs:   ./manager_log.sh   (or journalctl -u gopro-manager.service -f)"
echo ">>>   stop manager:   sudo systemctl stop gopro-manager.service"
echo ">>>   disable boot:   sudo systemctl disable gopro-manager.service"
