#!/bin/bash
# By-hand control of the GoPro rig -- the recording-menu features as one-line
# commands, for operators who prefer the shell over the interactive menu.
#
# It talks to the RUNNING manager's ROS2 services, so the watchdog / EMERGENCY
# logic stays in charge. (Do NOT use core/cli.py for this: that one bypasses the
# manager and drives the cameras directly, which fights the watchdog.)
#
#   ./gopro_ctl.sh record  (or start)     start recording on all cameras (start = alias of record)
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

read_status() {   # print the manager's latest snapshot from the .status file it writes each tick
    local f; f="$(cd "$(dirname "$0")" && pwd)/.status"
    if [ ! -f "$f" ]; then
        echo "no status yet -- is the manager running?  (sudo systemctl status gopro-manager)"
        return 1
    fi
    cat "$f"
    local age=$(( $(date +%s) - $(stat -c %Y "$f") ))
    [ "$age" -gt 5 ] && echo "!!! WARNING: this status is ${age}s old -- the manager may be stopped."
    return 0
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
#
# The request is async, but we still give the SENDING terminal a verdict: capture
# a journald cursor, drop the file, then poll the manager's own log (tag gopro)
# for its outcome line ([X] disabled / [X] solo / solo refused (recording) /
# [X] enabled). So the operator sees applied-or-refused here, not just "sent".
solo_request() {   # $1 = LEFT|RIGHT|duo
    local dir; dir="$(cd "$(dirname "$0")" && pwd)"
    local since; since="$(date '+%Y-%m-%d %H:%M:%S')"
    printf '%s\n' "$1" > "$dir/.solo_request"
    local i line
    for i in $(seq 1 8); do          # ~4 s: the manager consumes it within one 1 s tick
        sleep 0.5
        line="$(journalctl -t gopro --since "$since" --no-pager -o cat 2>/dev/null \
                | grep -iE 'solo|disabled|enabled|refused' | tail -4)"
        [ -n "$line" ] && { printf '%s\n' "$line"; return 0; }
    done
    echo "(no verdict in the log yet -- the manager may have had nothing to change; watch ./manager_log.sh)"
    return 1
}

cmd="${1:-help}"; shift || true
case "$cmd" in
    record|start) svc_call "$NODE/record" std_srvs/srv/SetBool "{data: true}"  ;;
    stop)         svc_call "$NODE/record" std_srvs/srv/SetBool "{data: false}" ;;
    status)       read_status ;;
    solo)
        tgt="$(printf '%s' "${1:-}" | tr '[:lower:]' '[:upper:]')"
        case "$tgt" in
            LEFT|RIGHT) ;;
            *) echo "usage: $0 solo LEFT|RIGHT   (keep only that camera, power the other off). Back to both: $0 duo"; exit 2 ;;
        esac
        echo "solo $tgt sent -- manager verdict:"
        solo_request "$tgt" ;;
    duo)
        echo "duo sent -- manager verdict:"
        solo_request duo ;;
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
    *) echo "unknown command '$cmd'. Try: record (or start) | stop | status | solo | duo | settings | help"; exit 2 ;;
esac
