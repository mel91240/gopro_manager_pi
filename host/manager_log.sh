#!/bin/bash
# Follow the GoPro logs live -- the manager, the auto-revive watcher AND the
# download (offload) run, merged into one clean stream (so a power-cycle shows up
# right next to the camera events that triggered it, and a download's progress
# next to both). Ctrl+C stops WATCHING only; the services keep running. Extra args
# pass through, e.g. ./manager_log.sh --since "1h ago".
# Filter by SyslogIdentifier (-t), NOT by unit (-u): the auto-revive watcher
# shells out to `sudo uhubctl` every scan, and -u would drown our lines in
# sudo/PAM session noise. -t keeps ONLY our own output (gopro=manager,
# revive=watcher, download=download.sh).
exec journalctl -t gopro -t revive -t download -f -n 60 -o short --no-hostname "$@"
