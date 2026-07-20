#!/bin/bash
# Start the GoPro manager (arms the cameras). This is the gopro-manager.service
# ExecStart hook, run IN THE FOREGROUND so systemd owns the container's stdout and
# captures it into journald -> `journalctl -u gopro-manager -f`, persisted across
# reboots and shown by ./manager_log.sh.
#
# Run BY HAND in a terminal it does NOT stream that firehose at you: it hands off
# to the systemd service (manager in the background, logs still in journald, prompt
# back immediately). Set GOPRO_MANAGER_FOREGROUND=1 to force the raw foreground run.
# Normally started at boot by gopro-manager.service (installed by ./install.sh).
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"   # .../gopro_scripts
IMAGE="${COSMA_IMAGE:-cosma_auv:latest}"
WS="${GOPRO_WS:-$(dirname "$DIR")}"     # workspace = parent of gopro_scripts (no hard-coded path)
NAME=gopro_manager

# Interactive (a human at a terminal) vs systemd (ExecStart, no TTY). When YOU run
# ./manager_up.sh by hand, don't hijack the terminal with the container's live log
# stream (and the awkward multi-Ctrl-C to stop it): hand off to the service. The
# manager runs in the BACKGROUND, its stdout still flows to journald -- so
# ./manager_log.sh keeps showing everything -- and your prompt returns at once.
if [ -t 1 ] && [ -z "${GOPRO_MANAGER_FOREGROUND:-}" ] \
   && command -v systemctl >/dev/null 2>&1 \
   && systemctl cat gopro-manager.service >/dev/null 2>&1; then
    sudo systemctl restart gopro-manager.service
    echo "manager started (background service) -- logs: $DIR/manager_log.sh"
    exit 0
fi

# --- Foreground path: systemd's ExecStart (no TTY), or GOPRO_MANAGER_FOREGROUND=1. ---
# Clear any stale/previous container so `docker run` can reuse the name. We do NOT
# early-exit if one is already running: a foreground service needs a live process
# to own, so we always (re)claim the container ourselves.
docker rm -f "$NAME" >/dev/null 2>&1 || true

echo "manager started" >&2
# DDS transport: force UDP-only via fastdds_udp_only.xml (no --ipc host). Across
# containers, FastDDS otherwise picks shared-memory (same host) but each
# container has a private /dev/shm, so the menu/autonomy discover the topics yet
# receive NO data -- and a hard `docker rm -f` leaks SHM segments onto the host.
# UDP localhost (ROS_LOCALHOST_ONLY=1) is robust for these tiny messages and
# never leaks. The profile path is inside the mounted workspace.
#
# No `-d` (foreground) and no `--restart` (systemd owns the lifecycle now, not
# Docker) -> the container's stdout flows to our parent, i.e. to journald.
# `exec` makes THIS script become the `docker run` process, so systemd watches
# the right PID and a SIGTERM on stop reaches the container cleanly.
exec docker run --name "$NAME" \
    --network host \
    -e ROS_DOMAIN_ID=0 -e ROS_LOCALHOST_ONLY=1 \
    -e FASTRTPS_DEFAULT_PROFILES_FILE=/home/cosma_auv/swarm-vehicle/gopro_scripts/fastdds_udp_only.xml \
    -e RCUTILS_CONSOLE_OUTPUT_FORMAT='{message}' -e RCUTILS_COLORIZED_OUTPUT=0 \
    -v "$WS":/home/cosma_auv/swarm-vehicle \
    --entrypoint bash "$IMAGE" -lc '
        source /opt/ros/humble/setup.bash &&
        source /home/cosma_auv/swarm-vehicle/ros2_ws/install/setup.bash &&
        exec ros2 run gopro_control gopro_manager --ros-args --params-file \
            /home/cosma_auv/swarm-vehicle/ros2_ws/src/gopro_control/params/gopro_params.yaml'
