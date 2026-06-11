#!/bin/bash
# Open the operator menu (transient client).
#
# Safe to quit and re-open as many times as you like: it does NOT stop the
# manager or the recording. On reconnect after a mission it shows the live state
# (e.g. RECORDING 2/2) so you can stop the take and recover the footage.
set -e

IMAGE=cosma_auv:latest
WS="$HOME/dev/swarm-vehicle"

if ! docker ps --format '{{.Names}}' | grep -qx gopro_manager; then
    echo "!!! Manager is not running. Start it first:  ./manager_up.sh"
    exit 1
fi

docker run --rm -it --network host --ipc host \
    -e ROS_DOMAIN_ID=0 -e ROS_LOCALHOST_ONLY=1 \
    -v "$WS":/home/cosma_auv/swarm-vehicle \
    --entrypoint bash "$IMAGE" -lc '
        source /opt/ros/humble/setup.bash &&
        source /home/cosma_auv/swarm-vehicle/ros2_ws/install/setup.bash &&
        exec ros2 run gopro_control pi_menu'
