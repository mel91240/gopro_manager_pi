#!/bin/bash
# revive.sh -- power-cycle a GoPro that has fallen OFF the USB bus.
#
# SAFETY RULES (the rig's hard constraints):
#  1. We ONLY cut Vbus on a port whose camera is NOT visible on the USB bus
#     (truly off -> SD idle -> cannot be corrupted). A still-visible camera might
#     be recording, so we NEVER touch it.
#  2. We act only on a SUSTAINED, CONFIRMED absence (several consecutive scans),
#     never on a single check -- a camera that blinks off for ~1s and comes back
#     must NOT be power-cycled.
#
# Cameras are tracked by SERIAL (stable), not by port: a port that no longer has
# a known GoPro can never be targeted, even if a camera was once plugged there.
# Learned mapping (serial -> hub:port) is kept in .gopro_ref.
#
#   ./revive.sh            one-shot, verbose (run by hand after a reboot)
#   ./revive.sh --watch    loop forever, auto-revive (started by manager_up.sh)
set -u
REF="$(cd "$(dirname "$0")" && pwd)/.gopro_ref"     # lines: "SERIAL hub:port"
UHUBCTL=/usr/sbin/uhubctl
CONFIRM=3                # consecutive scans a camera must be missing before we act
SCAN=5                   # [s] between scans

declare -A CUR_PORT      # serial -> hub:port  (currently visible)
declare -A PORT_TAKEN    # hub:port -> 1       (a GoPro is there right now)
declare -A REF_PORT      # serial -> hub:port  (last known, persisted)

scan_now() {             # fill CUR_PORT / PORT_TAKEN from uhubctl
    CUR_PORT=(); PORT_TAKEN=()
    local STATUS hub line p serial
    STATUS=$(sudo -n "$UHUBCTL" 2>/dev/null) || return 1
    hub=
    while IFS= read -r line; do
        [[ $line =~ Current\ status\ for\ hub\ ([^ ]+) ]] && hub=${BASH_REMATCH[1]}
        if [[ $line =~ Port\ ([0-9]+): ]]; then
            p=${BASH_REMATCH[1]}
            if [[ $line == *GoPro* && $line =~ \ ([A-Za-z0-9]+)\] ]]; then
                serial=${BASH_REMATCH[1]}
                CUR_PORT[$serial]="$hub:$p"
                PORT_TAKEN["$hub:$p"]=1
            fi
        fi
    done <<< "$STATUS"
}

load_ref() { REF_PORT=(); [[ -f $REF ]] || return 0
    local s hp; while read -r s hp; do [[ -n $s ]] && REF_PORT[$s]=$hp; done < "$REF"; }
save_ref() { : > "$REF"; local s; for s in "${!REF_PORT[@]}"; do echo "$s ${REF_PORT[$s]}" >> "$REF"; done; }

# Echo the hub:port of every known camera (by serial) that is currently missing
# AND whose port is free (no GoPro there now). Learns/updates visible serials.
missing_ports() {
    scan_now || return 1
    load_ref
    local s hp
    for s in "${!CUR_PORT[@]}"; do REF_PORT[$s]=${CUR_PORT[$s]}; done   # learn / refresh
    save_ref
    for s in "${!REF_PORT[@]}"; do
        if [[ -z ${CUR_PORT[$s]:-} ]]; then                # this serial is not visible
            hp=${REF_PORT[$s]}
            [[ -z ${PORT_TAKEN[$hp]:-} ]] && echo "$hp"     # its port is free -> safe to cycle
        fi
    done
}

cycle_port() { local h=${1%:*} p=${1#*:}; sudo -n "$UHUBCTL" -l "$h" -p "$p" -a cycle -d 4 >/dev/null 2>&1; }

if [[ "${1:-}" == "--watch" ]]; then
    echo "[autorevive] watching (power-cycle ONLY a camera confirmed OFF for ~$((CONFIRM*SCAN))s; never a visible one)"
    declare -A MISS
    while true; do
        mapfile -t missing < <(missing_ports)
        declare -A seen=()
        for hp in "${missing[@]:-}"; do
            [[ -z $hp ]] && continue
            seen[$hp]=1
            MISS[$hp]=$(( ${MISS[$hp]:-0} + 1 ))
            if (( ${MISS[$hp]} >= CONFIRM )); then
                echo "[autorevive] $hp OFF for ${MISS[$hp]} scans -> power-cycle"
                cycle_port "$hp"; MISS[$hp]=0
                sleep 30                       # let it boot/enumerate before scanning again
            fi
        done
        for hp in "${!MISS[@]}"; do [[ -z ${seen[$hp]:-} ]] && MISS[$hp]=0; done   # back -> reset
        sleep "$SCAN"
    done
fi

# --- one-shot (manual): confirm twice before cutting ------------------------
mapfile -t miss1 < <(missing_ports)
echo ">>> manquantes (1er scan) : ${miss1[*]:-aucune}"
{ [[ ${#miss1[@]} -eq 0 || -z ${miss1[0]:-} ]]; } && { echo ">>> rien à réveiller."; exit 0; }
echo ">>> re-vérification dans 2s (on ne coupe que si TOUJOURS absente)..."
sleep 2
mapfile -t miss2 < <(missing_ports)
for hp in "${miss1[@]}"; do
    [[ -z $hp ]] && continue
    if printf '%s\n' "${miss2[@]}" | grep -qxF "$hp"; then
        echo "  $hp confirmée OFF -> power-cycle hub ${hp%:*} port ${hp#*:}"; cycle_port "$hp"
    else
        echo "  $hp revenue entre-temps -> on NE touche pas."
    fi
done
echo ">>> attends ~22s ; le manager ré-arme la cam tout seul (re-scan)."
