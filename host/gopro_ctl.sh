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
#   ./gopro_ctl.sh solo LEFT|RIGHT        keep only that camera, power the other off
#   ./gopro_ctl.sh duo                    re-enable both cameras
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
    sed -n '2,19p' "$0" | sed 's/^# \?//'
}

# Printed on any settings error so the operator sees the valid values right in
# the terminal, without looking them up. Mirrors core/settings.py -- keep in sync.
settings_help() {
    cat >&2 <<'EOF'

Valid settings (pass only the fields you want to change):
  resolution      1080p | 2.7K | 4K | 5.3K
  fps             24 | 25 | 30 | 50 | 60 | 100 | 120 | 200 | 240
  fov             Wide | SuperView | Linear | MaxSuperView | LinearLeveling | HyperView | LinearLock
  hypersmooth     Off | On | AutoBoost
  wind_reduction  Off | Auto | On
  camera_mode     Video | Photo | Timelapse
Constraints: 5.3K max 60fps | 4K max 120fps | HyperView needs 4K/5.3K | Photo has no fps/hypersmooth/wind.
Example: ./gopro_ctl.sh settings resolution=4K fps=30 fov=Linear
EOF
}

# Solo/duo are handled host-side: the CLI just drops the request in a file the
# manager consumes (it alone knows label->socket and drives the watcher). No
# docker exec needed -- gopro_ctl.sh lives in the shared handoff dir.
solo_request() {   # $1 = LEFT|RIGHT|duo
    local dir; dir="$(cd "$(dirname "$0")" && pwd)"
    printf '%s\n' "$1" > "$dir/.solo_request"
}

cmd="${1:-help}"; shift || true
case "$cmd" in
    record|start) svc_call "$NODE/record" std_srvs/srv/SetBool "{data: true}"  ;;
    stop)         svc_call "$NODE/record" std_srvs/srv/SetBool "{data: false}" ;;
    status)       topic_once "$NODE/system" ;;
    solo)
        tgt="$(printf '%s' "${1:-}" | tr '[:lower:]' '[:upper:]')"
        case "$tgt" in
            LEFT|RIGHT) ;;
            *) echo "usage: $0 solo LEFT|RIGHT   (keep only that camera, power the other off). Back to both: $0 duo"; exit 2 ;;
        esac
        solo_request "$tgt"
        echo "solo $tgt requested -- watch ./manager_log.sh: the manager confirms and powers the other camera off." ;;
    duo)
        solo_request duo
        echo "duo requested -- both cameras re-enabled; watch ./manager_log.sh." ;;
    settings)
        [ $# -gt 0 ] || { echo "usage: $0 settings key=value ...  (only the fields you want to change)"; settings_help; exit 2; }
        yaml=""
        for kv in "$@"; do
            k="${kv%%=*}"; v="${kv#*=}"
            case "$k" in
                resolution|fps|fov|hypersmooth|wind_reduction|camera_mode) ;;
                *) echo "!!! unknown field '$k'"; settings_help; exit 2 ;;
            esac
            yaml="${yaml}${yaml:+, }${k}: '${v}'"
        done
        out="$(svc_call "$NODE/settings" gopro_msgs/srv/GoProSettings "{$yaml}")"
        printf '%s\n' "$out"
        # The manager validates the values/combo (settings.py) and replies
        # success=False on a bad value -- show the valid options so it can be fixed.
        if printf '%s' "$out" | grep -qiE 'success=False|success: *false'; then
            settings_help; exit 1
        fi ;;
    help|-h|--help) usage ;;
    *) echo "unknown command '$cmd'. Try: record | stop | status | solo | duo | settings | help"; exit 2 ;;
esac
