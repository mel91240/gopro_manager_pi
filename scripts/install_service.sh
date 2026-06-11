#!/bin/bash
# Install the auto-revive watcher as a systemd service so it starts on boot
# (and restarts if it ever dies). Run once. Needs sudo.
#
#   ./install_service.sh
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
SVC=gopro-autorevive.service

echo ">>> installing $SVC ..."
sudo cp "$DIR/$SVC" "/etc/systemd/system/$SVC"
sudo systemctl daemon-reload
sudo systemctl enable "$SVC"
sudo systemctl restart "$SVC"
sleep 1
sudo systemctl --no-pager --full status "$SVC" | head -6
echo ">>> done. The auto-revive watcher now starts automatically on every boot."
echo ">>>   logs:  journalctl -u $SVC -f"
echo ">>>   stop:  sudo systemctl stop $SVC     (disable: sudo systemctl disable $SVC)"
