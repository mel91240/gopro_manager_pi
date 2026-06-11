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
# Cameras are tracked by PORT, not by serial: whatever camera is plugged into a
# known GoPro socket is "that camera". So a flooded camera swapped for a fresh
# one (different serial) in the SAME socket works with no code/config change.
# The set of expected GoPro ports is snapshotted in .gopro_ref whenever the FULL
# set ($EXPECTED cameras) is visible -- so the ref never accumulates stale ports.
#
#   ./revive.sh            one-shot, verbose (run by hand)
#   ./revive.sh --watch    loop forever, auto-revive (run as a systemd service)
set -u
REF="$(cd "$(dirname "$0")" && pwd)/.gopro_ref"     # lines: "hub:port" (expected GoPro sockets)
UHUBCTL=/usr/sbin/uhubctl
EXPECTED=${GOPRO_COUNT:-2}    # number of GoPro sockets on the rig
CONFIRM=3                     # consecutive scans a port must be empty before we act
SCAN=5                        # [s] between scans

declare -A PORT_TAKEN         # hub:port -> 1  (a GoPro is enumerated there right now)

scan_now() {
    PORT_TAKEN=()
    local STATUS hub line p
    STATUS=$(sudo -n "$UHUBCTL" 2>/dev/null) || return 1
    hub=
    while IFS= read -r line; do
        [[ $line =~ Current\ status\ for\ hub\ ([^ ]+) ]] && hub=${BASH_REMATCH[1]}
        if [[ $line =~ Port\ ([0-9]+): ]]; then
            p=${BASH_REMATCH[1]}
            [[ $line == *GoPro* ]] && PORT_TAKEN["$hub:$p"]=1
        fi
    done <<< "$STATUS"
    return 0          # don't inherit the while loop's (often non-zero) exit code
}

# Echo each expected GoPro port that is currently EMPTY (no GoPro enumerated).
# Refreshes the expected-port set whenever the full set is present.
missing_ports() {
    scan_now || return 1
    if (( ${#PORT_TAKEN[@]} == EXPECTED )); then       # full set visible -> snapshot it
        : > "$REF"; local hp; for hp in "${!PORT_TAKEN[@]}"; do echo "$hp" >> "$REF"; done
    fi
    [[ -f $REF ]] || return 0
    local hp
    while IFS= read -r hp; do
        [[ -z $hp ]] && continue
        [[ -z ${PORT_TAKEN[$hp]:-} ]] && echo "$hp"
    done < "$REF"
}

cycle_port() { local h=${1%:*} p=${1#*:}; sudo -n "$UHUBCTL" -l "$h" -p "$p" -a cycle -d 4 >/dev/null 2>&1; }

if [[ "${1:-}" == "--watch" ]]; then
    echo "[autorevive] watching (power-cycle ONLY a socket confirmed empty for ~$((CONFIRM*SCAN))s; never a visible camera)"
    declare -A MISS
    while true; do
        mapfile -t missing < <(missing_ports)
        declare -A seen=()
        for hp in "${missing[@]:-}"; do
            [[ -z $hp ]] && continue
            seen[$hp]=1
            MISS[$hp]=$(( ${MISS[$hp]:-0} + 1 ))
            if (( ${MISS[$hp]} >= CONFIRM )); then
                echo "[autorevive] socket $hp empty for ${MISS[$hp]} scans -> power-cycle"
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
echo ">>> sockets vides (1er scan) : ${miss1[*]:-aucun}"
{ [[ ${#miss1[@]} -eq 0 || -z ${miss1[0]:-} ]]; } && { echo ">>> rien à réveiller."; exit 0; }
echo ">>> re-vérification dans 2s (on ne coupe que si TOUJOURS vide)..."
sleep 2
mapfile -t miss2 < <(missing_ports)
for hp in "${miss1[@]}"; do
    [[ -z $hp ]] && continue
    if printf '%s\n' "${miss2[@]}" | grep -qxF "$hp"; then
        echo "  $hp confirmé vide -> power-cycle hub ${hp%:*} port ${hp#*:}"; cycle_port "$hp"
    else
        echo "  $hp revenu entre-temps -> on NE touche pas."
    fi
done
echo ">>> attends ~22s ; le manager ré-arme la cam tout seul (re-scan)."
