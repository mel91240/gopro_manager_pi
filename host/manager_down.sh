#!/bin/bash
# Stop the GoPro manager (remove its container). Used as the ExecStop of
# gopro-manager.service and by the menu's [4]. Stopping the manager does NOT stop
# an in-progress recording; it only drops remote control until brought back up.
# The auto-revive watcher (gopro-autorevive.service) is left running on purpose.
set -e
echo ">>> Stopping GoPro manager..."
docker rm -f gopro_manager >/dev/null 2>&1 && echo ">>> Manager stopped." \
    || echo ">>> Manager was not running."
