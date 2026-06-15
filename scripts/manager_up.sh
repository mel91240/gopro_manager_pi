#!/bin/bash
# Start the GoPro manager (arms the cameras) as a detached, named Docker
# container that survives SSH disconnects. Normally started automatically at boot
# by gopro-manager.service (install with ./install_service.sh); run by hand only
# as a fallback, or on a host where the service isn't installed.
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
IMAGE=cosma_auv:latest
WS="$HOME/dev/swarm-vehicle"
NAME=gopro_manager

if docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
    echo ">>> Manager already running (container '$NAME')."
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

# The auto-revive watcher runs as a systemd service (starts on boot). Install it
# once with ./install_service.sh. We just report its state here.
if systemctl is-active --quiet gopro-autorevive.service 2>/dev/null; then
    echo ">>> auto-revive watcher: active (systemd)"
else
    echo ">>> auto-revive watcher: NOT running -- install it once: ./install_service.sh"
fi

echo ">>> Logs: ./manager_log.sh   |   operator menu: ./gopro.sh"
