#!/bin/bash
# manager_logfile.sh -- write a per-session transcript of the merged GoPro log
# stream to a dated file. Run by the gopro-logfile systemd service, which is
# bound to gopro-manager: exactly one file per manager (re)start, named with the
# session start time, holding what `manager_log.sh` shows live -- same tag filter
# (gopro=manager, revive=watcher, download=download.sh, delete=gopro_delete.py)
# and same `-o short --no-hostname` format -- but from THIS session onward and
# with no line cap (the file is the full transcript, not a 60-line tail).
#
# Files land in $GOPRO_LOG_DIR -- install.sh sets it (via the service) to
# <workspace>/log/gp; it falls back to ~/gopro_logs when the script is run
# standalone. `latest.log` always points at the current session. Only the newest
# $GOPRO_LOG_KEEP files are kept, so the SD card never fills with old transcripts.
set -u

LOG_DIR="${GOPRO_LOG_DIR:-$HOME/gopro_logs}"
KEEP="${GOPRO_LOG_KEEP:-50}"
mkdir -p "$LOG_DIR"

# prune old transcripts, keeping the newest KEEP (bounds SD-card usage)
ls -1t "$LOG_DIR"/manager_*.log 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f

file="$LOG_DIR/manager_$(date +%Y-%m-%d_%H-%M-%S).log"
ln -sf "$(basename "$file")" "$LOG_DIR/latest.log"

# --since=now: the service starts just BEFORE the manager (see gopro-logfile
# .service), so "now" precedes the manager's first line -> the whole session is
# captured and nothing from a previous session leaks in. stdbuf -oL flushes each
# line to disk immediately instead of sitting in a block buffer.
exec stdbuf -oL journalctl -t gopro -t revive -t download -t delete \
     --since=now -f -o short --no-hostname >> "$file" 2>&1
