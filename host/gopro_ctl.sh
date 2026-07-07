#!/bin/bash
# By-hand control of the GoPro rig -- the recording-menu features as one-line
# commands, for operators who prefer the shell over the interactive menu.
#
# It talks to the RUNNING manager's ROS2 services, so the watchdog / EMERGENCY
# logic stays in charge. (Do NOT use core/cli.py for this: that one bypasses the
# manager and drives the cameras directly, which fights the watchdog.)
#
#   ./gopro_ctl.sh record                 start recording on all cameras
#   ./gopro_ctl.sh stop                   stop recording
#   ./gopro_ctl.sh status                 show state (READY/RECORDING + per-cam SD)
#   ./gopro_ctl.sh settings k=v ...        change only the fields you pass:
#        resolution fps fov hypersmooth wind_reduction camera_mode
#        e.g. ./gopro_ctl.sh settings resolution=4K fps=30 fov=Linear
#
# The manager validates the settings combination (e.g. "5.3K is limited to 60fps")
# and replies; unpassed fields are left unchanged.
set -u

CONTAINER="${GOPRO_CONTAINER:-gopro_manager}"
NODE=/gopro_manager

docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$CONTAINER" || {
    echo "!!! Manager not running (container '$CONTAINER')."
    echo "    Start it:  sudo systemctl start gopro-manager"
    exit 1; }

# Everything runs INSIDE the manager container with the manager's own DDS env
# (UDP-only profile) -- otherwise ROS2 discovery finds nothing. Payloads are
# passed via `docker exec -e` to avoid nested-quote breakage.
svc_call() {   # $1=service  $2=type  $3=payload(YAML)
    docker exec -e S="$1" -e T="$2" -e P="$3" "$CONTAINER" bash -lc '
        source /opt/ros/humble/setup.bash
        source /home/cosma_auv/swarm-vehicle/ros2_ws/install/setup.bash
        export ROS_DOMAIN_ID=0 ROS_LOCALHOST_ONLY=1
        export FASTRTPS_DEFAULT_PROFILES_FILE=/home/cosma_auv/swarm-vehicle/gopro_scripts/fastdds_udp_only.xml
        exec ros2 service call "$S" "$T" "$P"'
}

topic_once() {  # $1=topic
    docker exec -e TOPIC="$1" "$CONTAINER" bash -lc '
        source /opt/ros/humble/setup.bash
        source /home/cosma_auv/swarm-vehicle/ros2_ws/install/setup.bash
        export ROS_DOMAIN_ID=0 ROS_LOCALHOST_ONLY=1
        export FASTRTPS_DEFAULT_PROFILES_FILE=/home/cosma_auv/swarm-vehicle/gopro_scripts/fastdds_udp_only.xml
        # ~/system is latched (transient-local) -- request the same durability so we
        # get the last published state at once, even on a stable idle rig.
        exec timeout 8 ros2 topic echo --once --qos-durability transient_local "$TOPIC"'
}

usage() {
    sed -n '2,17p' "$0" | sed 's/^# \?//'
}

cmd="${1:-help}"; shift || true
case "$cmd" in
    record|start) svc_call "$NODE/record" std_srvs/srv/SetBool "{data: true}"  ;;
    stop)         svc_call "$NODE/record" std_srvs/srv/SetBool "{data: false}" ;;
    status)       topic_once "$NODE/system" ;;
    settings)
        [ $# -gt 0 ] || { echo "usage: $0 settings key=value ...  (resolution fps fov hypersmooth wind_reduction camera_mode)"; exit 2; }
        yaml=""
        for kv in "$@"; do
            k="${kv%%=*}"; v="${kv#*=}"
            case "$k" in
                resolution|fps|fov|hypersmooth|wind_reduction|camera_mode) ;;
                *) echo "!!! unknown field '$k' (allowed: resolution fps fov hypersmooth wind_reduction camera_mode)"; exit 2 ;;
            esac
            yaml="${yaml}${yaml:+, }${k}: '${v}'"
        done
        svc_call "$NODE/settings" gopro_msgs/srv/GoProSettings "{$yaml}" ;;
    help|-h|--help) usage ;;
    *) echo "unknown command '$cmd'. Try: record | stop | status | settings | help"; exit 2 ;;
esac
