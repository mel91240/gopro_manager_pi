#!/bin/bash
source /opt/ros/humble/setup.bash
source /home/cosma_auv/swarm-vehicle/ros2_ws/install/setup.bash
ros2 run gopro_control gopro_manager > /tmp/mgr.log 2>&1 &
MGR=$!; trap "kill $MGR 2>/dev/null" EXIT
python3 /home/cosma_auv/swarm-vehicle/test_all.py
echo ""; echo "(manager errors, if any:)"; grep -iE "error|exception|fault|unrecover" /tmp/mgr.log || echo "  none"
kill $MGR 2>/dev/null
