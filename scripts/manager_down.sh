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

# stop the host-side auto-revive watcher too
if pkill -f 'revive.sh --watch' 2>/dev/null; then
    echo ">>> auto-revive watcher stopped"
fi
