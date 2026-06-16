#!/bin/bash
# Open the recording menu (record / stop / settings) -- a transient ROS client.
# Reached from gopro.sh [1]. Safe to quit and re-open: it touches neither the
# manager nor an in-progress recording, and on reconnect shows the live state
# (e.g. RECORDING 2/2) so a take can be stopped and the footage recovered.
set -e

IMAGE=cosma_auv:latest
WS="$HOME/dev/swarm-vehicle"

if ! docker ps --format '{{.Names}}' | grep -qx gopro_manager; then
    echo "!!! Manager is not running (it normally auto-starts at boot)."
    echo "    Start it from gopro.sh [4], or ./manager_up.sh."
    exit 1
fi

# DDS over UDP localhost (matches manager_up.sh): the UDP-only profile is what
# actually lets a fresh menu container receive the manager's /system (cross-
# container shared-memory delivers nothing); no --ipc host so nothing leaks.
docker run --rm -it --network host \
    -e ROS_DOMAIN_ID=0 -e ROS_LOCALHOST_ONLY=1 \
    -e FASTRTPS_DEFAULT_PROFILES_FILE=/home/cosma_auv/swarm-vehicle/gopro_scripts/fastdds_udp_only.xml \
    -v "$WS":/home/cosma_auv/swarm-vehicle \
    --entrypoint bash "$IMAGE" -lc '
        source /opt/ros/humble/setup.bash &&
        source /home/cosma_auv/swarm-vehicle/ros2_ws/install/setup.bash &&
        exec ros2 run gopro_control pi_menu'
