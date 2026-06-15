#!/bin/bash
# Start background logging for a GoPro recording-stability test.
#
# Captures, into a timestamped folder that SURVIVES an SSH drop (setsid):
#   kernel.log    kernel/USB events  -- THE signal when a camera drops off the bus
#   watcher.log   gopro-autorevive   -- any power-cycle of a camera
#   manager.log   gopro_manager      -- DEGRADED / FAULT / recovery (if running)
#   camstate.log  both cameras every 10 s (enc=1 means actively recording)
#
# Start this with the manager up (so its log is captured), then start recording
# from gopro.sh [1]. Stop everything later with log_stop.sh.
HERE="$(cd "$(dirname "$0")" && pwd)"
TS=$(date +%Y%m%d_%H%M%S)
DIR="$HOME/rectest/logs_$TS"
mkdir -p "$DIR"
echo "$DIR" > "$HOME/rectest/.current"
: > "$DIR/pids"
date -Iseconds > "$DIR/start_time.txt"

# 1) kernel / USB events
setsid journalctl -k -f -o short-iso </dev/null >"$DIR/kernel.log" 2>&1 &
echo $! >> "$DIR/pids"

# 2) auto-revive watcher
setsid journalctl -u gopro-autorevive.service -f -o short-iso </dev/null >"$DIR/watcher.log" 2>&1 &
echo $! >> "$DIR/pids"

# 3) manager (only if running)
if docker ps --format '{{.Names}}' | grep -qx gopro_manager; then
  setsid docker logs -f gopro_manager </dev/null >"$DIR/manager.log" 2>&1 &
  echo $! >> "$DIR/pids"
else
  echo "(manager not running -- no manager.log; start it from gopro.sh [4])"
fi

# 4) camera state every 10 s
setsid python3 "$HERE/campoll.py" </dev/null >"$DIR/camstate.log" 2>&1 &
echo $! >> "$DIR/pids"

sleep 1
echo ">>> logging started -> $DIR"
echo ">>> $(wc -l < "$DIR/pids") collectors running (survive SSH drop)"
echo ">>> now start recording:  gopro.sh [1] -> [1] -> confirm both RECORDING -> exit"
echo ">>> stop the test later with:  ~/rectest/log_stop.sh"
