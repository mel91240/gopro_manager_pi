#!/bin/bash
# Stop the manager service.
#
# Do this ONLY after the take is stopped and the footage is safe. Stopping the
# manager does NOT stop a recording, but you lose remote control of the cameras
# until you bring it back up with ./manager_up.sh
set -e
echo ">>> Stopping GoPro manager..."
docker rm -f gopro_manager >/dev/null 2>&1 && echo ">>> Manager stopped." \
    || echo ">>> Manager was not running."

# The auto-revive watcher is a systemd service and is left running on purpose
# (it only ever acts on a camera confirmed off the bus). To stop it:
#   sudo systemctl stop gopro-autorevive.service
