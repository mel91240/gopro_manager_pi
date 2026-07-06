#!/bin/bash
# Start the GoPro manager (arms the cameras) IN THE FOREGROUND, so that whoever
# launched it OWNS the container's stdout -- that is what decides where the logs go:
#   - under systemd (gopro-manager.service): stdout is captured by journald
#       -> `journalctl -u gopro-manager -f`  (and persisted across reboots).
#   - by hand in a terminal: the logs stream live; Ctrl-C stops the manager.
# Normally started at boot by gopro-manager.service (installed by ./install.sh).
# (Previously this ran a *detached* `docker run -d`, which handed the logs to
#  Docker instead of systemd -- so `journalctl` stayed empty. That is the fix.)
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"   # .../gopro_scripts
IMAGE="${COSMA_IMAGE:-cosma_auv:latest}"
WS="${GOPRO_WS:-$(dirname "$DIR")}"     # workspace = parent of gopro_scripts (no hard-coded path)
NAME=gopro_manager

# Clear any stale/previous container so `docker run` can reuse the name. We do NOT
# early-exit if one is already running: a foreground service needs a live process
# to own, so we always (re)claim the container ourselves.
docker rm -f "$NAME" >/dev/null 2>&1 || true

echo ">>> Starting GoPro manager in the foreground (arming cameras)..." >&2
# DDS transport: force UDP-only via fastdds_udp_only.xml (no --ipc host). Across
# containers, FastDDS otherwise picks shared-memory (same host) but each
# container has a private /dev/shm, so the menu/autonomy discover the topics yet
# receive NO data -- and a hard `docker rm -f` leaks SHM segments onto the host.
# UDP localhost (ROS_LOCALHOST_ONLY=1) is robust for these tiny messages and
# never leaks. The profile path is inside the mounted workspace.
#
# No `-d` (foreground) and no `--restart` (systemd owns the lifecycle now, not
# Docker) -> the container's stdout flows to our parent, i.e. to journald.
# `exec` makes THIS script become the `docker run` process, so systemd watches
# the right PID and a SIGTERM on stop reaches the container cleanly.
exec docker run --name "$NAME" \
    --network host \
    -e ROS_DOMAIN_ID=0 -e ROS_LOCALHOST_ONLY=1 \
    -e FASTRTPS_DEFAULT_PROFILES_FILE=/home/cosma_auv/swarm-vehicle/gopro_scripts/fastdds_udp_only.xml \
    -v "$WS":/home/cosma_auv/swarm-vehicle \
    --entrypoint bash "$IMAGE" -lc '
        source /opt/ros/humble/setup.bash &&
        source /home/cosma_auv/swarm-vehicle/ros2_ws/install/setup.bash &&
        exec ros2 run gopro_control gopro_manager --ros-args --params-file \
            /home/cosma_auv/swarm-vehicle/ros2_ws/src/gopro_control/params/gopro_params.yaml'
