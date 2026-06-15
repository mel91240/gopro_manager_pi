#!/bin/bash
# Stop the recording-stability log collectors, snapshot final state, run a
# quick anomaly scan, and bundle everything into a tar.gz for analysis.
DIR=$(cat "$HOME/rectest/.current" 2>/dev/null)
[ -d "$DIR" ] || { echo "no current log dir (run log_start.sh first)"; exit 1; }

echo ">>> stopping collectors"
while read -r p; do
  [ -n "$p" ] && { kill "$p" 2>/dev/null; kill -- -"$p" 2>/dev/null; }
done < "$DIR/pids"

date -Iseconds > "$DIR/stop_time.txt"
df -h / > "$DIR/df_final.txt" 2>&1
{ for ip in 172.24.163.51 172.26.185.51; do
    printf '%s ' "$ip"
    curl -fsS --max-time 6 "http://$ip:8080/gopro/camera/state" -o /dev/null -w 'http=%{http_code}\n' 2>/dev/null || echo unreachable
  done; } > "$DIR/cam_final.txt" 2>&1

S="$DIR/SUMMARY.txt"
{
  echo "=== RECORDING TEST SUMMARY ==="
  echo "start: $(cat "$DIR/start_time.txt" 2>/dev/null)   stop: $(cat "$DIR/stop_time.txt")"
  echo
  echo "--- USB / kernel anomalies (cam dropping off the bus = the key failure) ---"
  grep -iE 'usb [0-9].*(disconnect|reset)|new (super|high)speed|over-?current|cannot enable|device descriptor|gadget|link is down' "$DIR/kernel.log" 2>/dev/null | tail -60
  echo "(kernel anomaly count: $(grep -icE 'usb [0-9].*(disconnect|reset)|over-?current|cannot enable|device descriptor' "$DIR/kernel.log" 2>/dev/null))"
  echo
  echo "--- watcher power-cycles (a cam was confirmed off the bus) ---"
  grep -iE 'power-cycle|empty|revive|cycling|off the bus' "$DIR/watcher.log" 2>/dev/null || echo "(none)"
  echo
  echo "--- manager DEGRADED / FAULT / recovery ---"
  grep -iE 'degraded|fault|emergency|dropped|recover|not ready|compromis' "$DIR/manager.log" 2>/dev/null || echo "(none / no manager.log)"
  echo
  echo "--- camera reachability gaps (enc should stay 1 the whole time) ---"
  echo "UNREACHABLE samples: $(grep -c UNREACHABLE "$DIR/camstate.log" 2>/dev/null)"
  grep UNREACHABLE "$DIR/camstate.log" 2>/dev/null | head -20
  echo "enc=0 samples (recording stopped?): $(grep -c 'enc=0' "$DIR/camstate.log" 2>/dev/null)"
  echo
  echo "final camera state: $(tr '\n' '  ' < "$DIR/cam_final.txt")"
} | tee "$S"

TAR="$HOME/rectest/$(basename "$DIR").tar.gz"
tar -czf "$TAR" -C "$(dirname "$DIR")" "$(basename "$DIR")" 2>/dev/null
echo
echo ">>> logs bundled -> $TAR"
echo ">>> summary saved  -> $S"
