#!/bin/bash
# Follow the GoPro logs live -- BOTH the manager and the auto-revive watcher,
# merged into one clean stream (so a power-cycle shows up right next to the
# camera events that triggered it). Ctrl+C stops WATCHING only; the services
# keep running. Extra args pass through, e.g. ./manager_log.sh --since "1h ago".
# Filter by SyslogIdentifier (-t), NOT by unit (-u): the auto-revive watcher
# shells out to `sudo uhubctl` every scan, and -u would drown our lines in
# sudo/PAM session noise. -t keeps ONLY the manager's and watcher's own output.
exec journalctl -t gopro -t revive -f -n 60 -o short --no-hostname "$@"
