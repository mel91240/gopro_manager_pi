#!/bin/bash
# Start the GoPro manager as a PERSISTENT background service on the AUV Pi.
#
# The manager must keep running for the WHOLE mission -- while the AUV is in the
# water and even after the operator closes the menu or drops the SSH session. So
# it runs as a DETACHED, named Docker container, NOT tied to this shell.
# Run this ONCE at the start of a mission.
#
#   ./manager_up.sh     start it (arms the cameras)            <- run once
#   ./menu.sh           open the operator menu (start/stop)    <- as many times as you like
#   ./manager_log.sh    watch what the manager is doing
#   ./manager_down.sh   stop it (ONLY after the footage is safe)
set -e

IMAGE=cosma_auv:latest
WS="$HOME/dev/swarm-vehicle"
NAME=gopro_manager

if docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
    echo ">>> Manager already running (container '$NAME'). Open the menu with ./menu.sh"
    exit 0
fi
docker rm -f "$NAME" >/dev/null 2>&1 || true

echo ">>> Starting persistent GoPro manager (arming cameras)..."
docker run -d --name "$NAME" --restart unless-stopped \
    --network host --ipc host \
    -e ROS_DOMAIN_ID=0 -e ROS_LOCALHOST_ONLY=1 \
    -v "$WS":/home/cosma_auv/swarm-vehicle \
    --entrypoint bash "$IMAGE" -lc '
        source /opt/ros/humble/setup.bash &&
        source /home/cosma_auv/swarm-vehicle/ros2_ws/install/setup.bash &&
        exec ros2 run gopro_control gopro_manager'

echo ">>> Manager up (survives SSH disconnect)."
echo ">>> Watch it arm the cameras:   ./manager_log.sh"
echo ">>> When all cameras are READY:  ./menu.sh"
