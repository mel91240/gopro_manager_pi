#!/bin/bash
# Automated ROS-interface test: spawn a manager, run test_all.py against it.
# Runs inside the cosma_auv dev container (no persistent/systemd manager).
DIR="$(cd "$(dirname "$0")" && pwd)"
source /opt/ros/humble/setup.bash
source /home/cosma_auv/swarm-vehicle/ros2_ws/install/setup.bash
ros2 run gopro_control gopro_manager > /tmp/mgr.log 2>&1 &
MGR=$!; trap "kill $MGR 2>/dev/null" EXIT
python3 "$DIR/test_all.py"
echo ""; echo "(manager errors, if any:)"; grep -iE "error|exception|fault|unrecover" /tmp/mgr.log || echo "  none"
kill $MGR 2>/dev/null
