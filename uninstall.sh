#!/bin/bash
# uninstall.sh -- remove the GoPro add-on from a Pi. Footage on the SSD is never
# touched. By default it leaves the built packages and host scripts in place
# (only stops/removes the services + sudoers); pass --purge to also remove the
# packages, the gopro_scripts/ folder and the workspace build artifacts.
#
#   ./uninstall.sh            # stop+disable services, remove service files + sudoers
#   ./uninstall.sh --purge    # the above + remove the GoPro packages and scripts
set -euo pipefail

RUN_USER="${SUDO_USER:-$USER}"
USER_HOME="$(eval echo "~$RUN_USER")"
GOPRO_WS="${GOPRO_WS:-$USER_HOME/dev/swarm-vehicle}"
SCRIPTS_DST="$GOPRO_WS/gopro_scripts"
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

say() { echo ">>> $*"; }

say "stopping + disabling services"
for svc in gopro-manager gopro-autorevive gopro-logfile; do
    sudo systemctl disable --now "$svc.service" 2>/dev/null || true
    sudo rm -f "/etc/systemd/system/$svc.service"
done
sudo systemctl daemon-reload
docker rm -f gopro_manager >/dev/null 2>&1 || true

say "removing /etc/sudoers.d/uhubctl"
sudo rm -f /etc/sudoers.d/uhubctl

if [ "$PURGE" = 1 ]; then
    say "purging packages + scripts (footage on the SSD is left untouched)"
    rm -rf "${GOPRO_WS:?}/ros2_ws/src/gopro_control" "${GOPRO_WS:?}/ros2_ws/src/gopro_msgs"
    rm -rf "${GOPRO_WS:?}/ros2_ws/build/gopro_control" "${GOPRO_WS:?}/ros2_ws/build/gopro_msgs" \
           "${GOPRO_WS:?}/ros2_ws/install/gopro_control" "${GOPRO_WS:?}/ros2_ws/install/gopro_msgs"
    rm -rf "$SCRIPTS_DST"
    echo "    (the .gopro_ref / .recording_intent / .revive_request runtime files went with it)"
else
    say "left the packages + $SCRIPTS_DST in place (use --purge to remove them)"
fi
say "done."
