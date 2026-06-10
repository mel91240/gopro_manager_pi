#!/bin/bash
# AUV GoPro mission console: arms the cameras then opens the operator menu.
set -e
source /opt/ros/humble/setup.bash
source /home/cosma_auv/swarm-vehicle/ros2_ws/install/setup.bash

echo ">>> Starting gopro_manager (arming cameras)..."
ros2 run gopro_control gopro_manager > /tmp/gopro_manager.log 2>&1 &
MGR=$!
trap "kill $MGR 2>/dev/null" EXIT
sleep 11
echo ">>> Manager startup log:"
sed 's/^/    /' /tmp/gopro_manager.log | tail -10
echo ""
echo ">>> Opening operator menu (Ctrl+C or [0] to quit; STOP recording before quitting)"
echo "-------------------------------------------------------------------"
ros2 run gopro_control pi_menu
