#!/bin/bash
# install.sh -- add the GoPro rig to a Pi that already has the base AUV setup.
#
# One command turns a base-image Pi (BlueOS + the cosma_auv image + the
# swarm-vehicle workspace) into a working GoPro rig: it drops the two ROS
# packages into the workspace, builds them in the cosma_auv container, installs
# the host scripts, the uhubctl sudoers rule and the two systemd services, then
# enables them so the cameras arm themselves on every boot.
#
# It is idempotent: safe to re-run to update an existing install.
#
#   ./install.sh
#   GOPRO_WS=/path/to/swarm-vehicle ./install.sh     # non-default workspace
#   COSMA_IMAGE=myimage:tag ./install.sh             # non-default ROS image
#
# Nothing here is GoPro-version-specific to one Pi: paths are derived, not
# hard-coded, so the same repo installs on any Pi.
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
RUN_USER="${SUDO_USER:-$USER}"
USER_HOME="$(eval echo "~$RUN_USER")"
GOPRO_WS="${GOPRO_WS:-$USER_HOME/dev/swarm-vehicle}"
IMAGE="${COSMA_IMAGE:-cosma_auv:latest}"

WS_SRC="$GOPRO_WS/ros2_ws/src"
SCRIPTS_DST="$GOPRO_WS/gopro_scripts"
GP_LOG_DIR="$GOPRO_WS/log/gopro"   # per-session manager transcripts land here
PKGS="gopro_msgs gopro_control"

say() { echo ">>> $*"; }
die() { echo "!!! $*" >&2; exit 1; }

say "GoPro install"
echo "    workspace : $GOPRO_WS"
echo "    ROS image : $IMAGE"
echo "    user      : $RUN_USER"
[ -d "$GOPRO_WS/ros2_ws" ] || die "no ros2_ws under $GOPRO_WS -- is this the AUV workspace? (set GOPRO_WS=)"
command -v docker >/dev/null || die "docker not found"
docker image inspect "$IMAGE" >/dev/null 2>&1 || die "docker image '$IMAGE' not found (set COSMA_IMAGE=)"
if ! command -v uhubctl >/dev/null && [ ! -x /usr/sbin/uhubctl ]; then
    echo "!!! WARNING: uhubctl not found -- the auto-revive watcher cannot power-cycle"
    echo "    a camera that falls off the USB bus until it is installed"
    echo "    (sudo apt install uhubctl). The manager + recording still work without it;"
    echo "    only off-bus camera recovery needs it. Continuing..."
fi

# 1. ROS packages -> workspace src (replace cleanly so no stale files linger)
say "copying ROS packages into $WS_SRC"
mkdir -p "$WS_SRC"
for p in gopro_control gopro_msgs; do
    rm -rf "${WS_SRC:?}/$p"
    cp -r "$REPO/ros2_pkgs/$p" "$WS_SRC/$p"
done

# 2. build them inside the cosma_auv container (ROS 2 lives only in the image)
say "building $PKGS in the $IMAGE container (colcon)"
docker run --rm -v "$GOPRO_WS":/home/cosma_auv/swarm-vehicle --entrypoint bash "$IMAGE" -lc \
    "source /opt/ros/humble/setup.bash && cd /home/cosma_auv/swarm-vehicle/ros2_ws && \
     colcon build --packages-select $PKGS" \
    || die "colcon build failed"

# 3. host scripts -> gopro_scripts/
say "installing host scripts into $SCRIPTS_DST"
mkdir -p "$SCRIPTS_DST"
cp "$REPO"/host/*.sh "$REPO"/host/*.py "$REPO"/host/*.xml "$SCRIPTS_DST/"
chmod +x "$SCRIPTS_DST"/*.sh "$SCRIPTS_DST"/*.py 2>/dev/null || true

# 4. uhubctl sudoers (the watcher cuts Vbus via `sudo -n uhubctl`)
UHUBCTL="$(command -v uhubctl || echo /usr/sbin/uhubctl)"
say "installing /etc/sudoers.d/uhubctl ($RUN_USER NOPASSWD: $UHUBCTL)"
echo "$RUN_USER ALL=(root) NOPASSWD: $UHUBCTL" | sudo tee /etc/sudoers.d/uhubctl >/dev/null
sudo chmod 0440 /etc/sudoers.d/uhubctl
sudo visudo -cf /etc/sudoers.d/uhubctl >/dev/null || { sudo rm -f /etc/sudoers.d/uhubctl; die "bad sudoers"; }

# 5. systemd services (paths/user injected from the templates -- no hard-coding)
say "installing systemd services"
for svc in gopro-manager gopro-autorevive gopro-logfile; do
    sed -e "s#__GOPRO_SCRIPTS__#$SCRIPTS_DST#g" \
        -e "s#__USER__#$RUN_USER#g" \
        -e "s#__HOME__#$USER_HOME#g" \
        -e "s#__LOG_DIR__#$GP_LOG_DIR#g" \
        "$REPO/host/systemd/$svc.service.in" | sudo tee "/etc/systemd/system/$svc.service" >/dev/null
done
sudo systemctl daemon-reload
# gopro-logfile is WantedBy gopro-manager: enabling it links it into the manager so
# it starts/stops/restarts with each manager session (one dated transcript each).
sudo systemctl enable gopro-manager.service gopro-autorevive.service gopro-logfile.service
# restart (not just --now): on a re-install the manager container is already up,
# and manager_up.sh is a no-op when it sees a running container -- so without a
# restart the freshly-built code would not be picked up until the next boot.
say "restarting services to load the build"
sudo systemctl restart gopro-autorevive.service
if [ -f "$SCRIPTS_DST/.recording_intent" ]; then
    echo "!!! a recording is IN PROGRESS -- NOT restarting the manager (would cut the"
    echo "    take). The new build loads on the next manager restart: when the take is"
    echo "    stopped, run: sudo systemctl restart gopro-manager.service"
else
    sudo systemctl restart gopro-manager.service
fi

say "done. The cameras now arm on every boot."
echo "    control (cli) : $SCRIPTS_DST/gopro_ctl.sh  record | stop | status | settings k=v"
echo "    SSD offload   : $SCRIPTS_DST/download.sh  (run '$REPO/setup.sh' once first to disable UAS on the SSD, then reboot)"
echo "    manager logs  : $SCRIPTS_DST/manager_log.sh  (live)  |  transcripts: $GP_LOG_DIR/ (one dated file per manager start, latest.log -> current)"
echo "    remove        : $REPO/uninstall.sh"
