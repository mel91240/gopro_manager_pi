#!/bin/bash
# download.sh -- offload recorded videos from the GoPros to the Pi.
#
# Each clip is saved as  <dest>/<CAMERA>/<UTC-timestamp>_<name>.MP4
# The camera clock is synced (tzone=0), so the timestamp is real UTC -- segments
# of one mission line up across both cameras (and across a reboot). Re-runnable:
# a file already present with the right size is skipped, so an interrupted
# download just resumes.
#
#   ./download.sh                  both cameras -> ~/gopro_footage
#   ./download.sh --all            include tiny (<2 MB) test clips too
#   GOPRO_DEST=/mnt/usb ./download.sh
#
# Tip: for a clean, fast transfer, stop the manager first (./manager_down.sh) so
# it isn't polling the cameras at the same time.
set -u
DEST="${GOPRO_DEST:-$HOME/gopro_footage}"
MINSIZE=2000000                       # skip clips smaller than this (0 s test clips)
[[ "${1:-}" == "--all" ]] && MINSIZE=0

# Discover camera IPs from the host's USB-ethernet interfaces (Open GoPro = .51),
# labelled in the same order as the manager (sorted by IP).
mapfile -t IPS < <(ip -4 -o addr show 2>/dev/null \
    | grep -oE '172\.2[0-9]\.[0-9]+\.[0-9]+' | sed -E 's/\.[0-9]+$/.51/' | sort -u)
LABELS=(LEFT RIGHT CAM2 CAM3)
[[ ${#IPS[@]} -eq 0 ]] && { echo "No GoPro found on the USB bus."; exit 1; }

if pgrep -f gopro_manager >/dev/null 2>&1 || docker ps --format '{{.Names}}' 2>/dev/null | grep -qx gopro_manager; then
    echo "(note: manager is running -- ./manager_down.sh first if a download stalls)"
fi

total=0; bytes=0; fail=0
for i in "${!IPS[@]}"; do
    ip=${IPS[$i]}; label=${LABELS[$i]:-CAM$i}
    echo ">>> $label ($ip)"
    list=$(curl -s --max-time 15 "http://$ip:8080/gopro/media/list") \
        || { echo "  unreachable -- skipped"; continue; }
    while IFS=$'\t' read -r dir name size cre; do
        [[ -z ${name:-} ]] && continue
        (( size < MINSIZE )) && continue
        ts=$(date -u -d "@$cre" +%Y%m%d_%H%M%S 2>/dev/null || echo nodate)
        mkdir -p "$DEST/$label"
        out="$DEST/$label/${ts}_${name}"
        if [[ -f "$out" ]] && (( $(stat -c%s "$out" 2>/dev/null || echo 0) == size )); then
            echo "  have  ${ts}_${name}"; continue
        fi
        printf "  get   %s_%s (%d MB)... " "$ts" "$name" "$(( size/1000000 ))"
        if curl -s --max-time 1800 "http://$ip:8080/videos/DCIM/$dir/$name" -o "$out" \
           && (( $(stat -c%s "$out" 2>/dev/null || echo 0) == size )); then
            echo "OK"; total=$((total+1)); bytes=$((bytes+size))
        else
            echo "FAIL"; fail=$((fail+1)); rm -f "$out"
        fi
    done < <(echo "$list" | python3 -c "
import sys,json
d=json.load(sys.stdin)
for m in d.get('media',[]):
    for f in m.get('fs',[]):
        print('\t'.join([m['d'], f['n'], str(f.get('s',0)), str(f.get('cre', f.get('mod','0')))]))
")
done
echo ">>> done: $total file(s), $(( bytes/1000000 )) MB -> $DEST   (failures: $fail)"
