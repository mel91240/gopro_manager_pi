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
#  3. We ONLY revive a camera that was PRESENT then dropped (EVER_SEEN) -- NEVER a
#     camera that is merely booting or has never appeared. Cutting Vbus on a camera
#     that is still enumerating (e.g. at boot) just makes it suffer for nothing.
#  4. We BACK OFF: after MAX_CYCLES tries with no return we give up on a socket
#     (removed/dead camera) and stay quiet until it reappears and is stable -- no
#     endless power-cycle loop.
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
UHUBCTL=$(command -v uhubctl 2>/dev/null || echo /usr/sbin/uhubctl)   # resolve where uhubctl really is (must match the /etc/sudoers.d/uhubctl path install.sh grants)
EXPECTED=${GOPRO_COUNT:-2}    # number of GoPro sockets on the rig
CONFIRM=3                     # consecutive scans a port must be empty before we act
SCAN=2                        # [s] between scans  (CONFIRM*SCAN = confirmation window)
REQ="$(cd "$(dirname "$0")" && pwd)/.revive_request"   # manager writes "hub:port" here for a targeted Vbus cycle (on-bus capture-dead cam)
SOLO="$(cd "$(dirname "$0")" && pwd)/.solo"            # manager writes "hub:port LABEL" per socket to keep POWERED OFF (solo mode); empty/absent = duo
SOCKMAP="$(cd "$(dirname "$0")" && pwd)/.socket_labels" # manager writes "hub:port LABEL": lets us log [LEFT]/[RIGHT] instead of a raw "socket 2-2:2"
REQ_OFF=15                    # [s] Vbus OFF for a requested cycle (a capture-dead/black-but-lit cam needs a long cut, not the 8s of an off-bus blip)
BOOT_SETTLE=30                # [s] after ANY cycle, ignore that socket's emptiness this long (it is booting/enumerating) -> never re-cut a camera mid-boot
BOOT_SCANS=$(( (BOOT_SETTLE + SCAN - 1) / SCAN ))   # the above expressed in scan ticks
MAX_CYCLES=3                  # give up power-cycling a socket after this many tries with no return (no endless Vbus loop on a removed/dead camera)
STABLE=5                      # a returned camera must stay present this many scans before its back-off resets (a mere flicker back does NOT re-arm the loop)

declare -A PORT_TAKEN         # hub:port -> 1  (a GoPro is enumerated there right now)
CYCLED_PORT=                  # set by handle_request to the socket it just cycled (so the watch loop can grant it a boot grace)

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
# Echo each EXPECTED GoPro socket currently EMPTY. Reads the GLOBAL PORT_TAKEN,
# so a scan_now must have run in the SAME shell first (the watch loop relies on
# PORT_TAKEN staying set in the parent, which a `< <(missing_ports)` subshell would
# hide). Expected = the full-set snapshot (.gopro_ref) UNION the manager's known
# sockets (.socket_labels) -- the union revives a camera seen once but never with
# the other simultaneously (REF alone, needing the full set, would miss it).
expected_missing() {
    if (( ${#PORT_TAKEN[@]} == EXPECTED )); then       # full set visible -> snapshot it
        : > "$REF"; local hp; for hp in "${!PORT_TAKEN[@]}"; do echo "$hp" >> "$REF"; done
    fi
    local hp; declare -A EXP=()
    [[ -f $REF ]]     && while IFS= read -r hp;   do [[ -n $hp ]] && EXP[$hp]=1; done < "$REF"
    [[ -f $SOCKMAP ]] && while IFS= read -r hp _; do [[ -n $hp ]] && EXP[$hp]=1; done < "$SOCKMAP"
    for hp in "${!EXP[@]}"; do
        [[ -z ${PORT_TAKEN[$hp]:-} ]] && echo "$hp"
    done
}
# One-shot helper (scan + compute) for the manual mode at the bottom.
missing_ports() { scan_now || return 1; expected_missing; }

cycle_port() { local h=${1%:*} p=${1#*:}; sudo -n "$UHUBCTL" -l "$h" -p "$p" -a cycle -d 8 >/dev/null 2>&1; }
cycle_port_long() { local h=${1%:*} p=${1#*:}; sudo -n "$UHUBCTL" -l "$h" -p "$p" -a cycle -d "$REQ_OFF" >/dev/null 2>&1; }
off_port() { local h=${1%:*} p=${1#*:}; sudo -n "$UHUBCTL" -l "$h" -p "$p" -a off >/dev/null 2>&1; }
on_port()  { local h=${1%:*} p=${1#*:}; sudo -n "$UHUBCTL" -l "$h" -p "$p" -a on  >/dev/null 2>&1; }
# Sockets the manager marked as deliberately-off (solo mode): first field of each .solo line.
solo_ports() { [[ -f $SOLO ]] && awk 'NF{print $1}' "$SOLO"; return 0; }
# "hub:port" -> "[LEFT]"/"[RIGHT]" via the manager's map, else "[autorevive]" (socket unknown yet).
label_of() {
    local hp=$1 l=
    [[ -f $SOCKMAP ]] && l=$(awk -v s="$hp" '$1==s{print $2; exit}' "$SOCKMAP")
    [[ -n $l ]] && echo "[$l]" || echo "[autorevive]"
}

# The manager (no uhubctl in its container) drops a "hub:port" into $REQ to ask us to
# Vbus-cycle a camera that is ON the bus but capture-dead (brown-out: /state 200 but
# /shutter 500). We cut ONLY that socket, with a LONG cut (a short blip won't reset
# it); a camera filming on another port is never touched. revive's normal off-bus
# logic does not catch this case (the dead camera is still enumerated).
handle_request() {
    [[ -s $REQ ]] || return 0
    local line; line=$(head -n1 "$REQ")
    : > "$REQ"                                   # consume immediately: one cycle per request
    local target label
    target=$(awk '{print $1}' <<<"$line")        # "hub:port"
    label=$(awk '{print $2}' <<<"$line")         # optional "LEFT"/"RIGHT" (the manager tags it)
    [[ -n $label ]] && label="[$label]" || label="[autorevive]"
    if [[ ! $target =~ ^[^:]+:[0-9]+$ ]]; then
        echo "$label bad Vbus request '$line' -- ignored"; return 0
    fi
    echo "$label cutting Vbus ${REQ_OFF}s (power-cycle)"
    cycle_port_long "$target"
    echo "$label power-cycle done -- re-arming"
    CYCLED_PORT=$target          # tell the watch loop to let this socket boot (don't re-cut it)
}

if [[ "${1:-}" == "--watch" ]]; then
    echo "[autorevive] watching (power-cycle ONLY a camera that was PRESENT then dropped -- never a booting/never-seen one; give up after $MAX_CYCLES tries)"
    declare -A MISS GRACE SOLO_OFF CYCLES GIVEN_UP PRESENT_RUN EVER_SEEN
    while true; do
        CYCLED_PORT=
        handle_request
        if [[ -n $CYCLED_PORT ]]; then         # a requested cycle just happened
            GRACE[$CYCLED_PORT]=$BOOT_SCANS     # protect it while it cold-boots
            MISS[$CYCLED_PORT]=0
        fi
        # Solo mode: keep the manager-designated socket(s) powered OFF and out of
        # the revive logic. Re-assert OFF every scan (idempotent) so a camera can
        # never creep back on; power it ON again the instant it leaves .solo (duo).
        declare -A DISABLED=()
        while IFS= read -r hp; do [[ -n $hp ]] && DISABLED[$hp]=1; done < <(solo_ports)
        for hp in "${!DISABLED[@]}"; do
            off_port "$hp"; MISS[$hp]=0; GRACE[$hp]=0
            if [[ -z ${SOLO_OFF[$hp]:-} ]]; then
                echo "$(label_of "$hp") power off"; SOLO_OFF[$hp]=1
            fi
        done
        for hp in "${!SOLO_OFF[@]}"; do
            if [[ -z ${DISABLED[$hp]:-} ]]; then
                echo "$(label_of "$hp") power on"; on_port "$hp"; unset 'SOLO_OFF[$hp]'
            fi
        done
        scan_now || { sleep "$SCAN"; continue; }  # populate PORT_TAKEN in THIS shell (parent)
        mapfile -t missing < <(expected_missing)  # subshell only READS PORT_TAKEN; parent keeps it
        for hp in "${missing[@]:-}"; do
            [[ -z $hp ]] && continue
            PRESENT_RUN[$hp]=0
            [[ -n ${DISABLED[$hp]:-} ]] && continue   # solo: deliberately off -> never revive
            [[ -n ${GIVEN_UP[$hp]:-} ]] && continue   # gave up on this one -> leave it until it returns
            # NEVER power-cycle a socket we have not yet seen PRESENT since this watcher
            # started: at boot the cameras take time to enumerate, and cutting their Vbus
            # then just makes them suffer for nothing. We only recover a camera that WAS
            # present and then dropped -- a real fall-off-the-bus, not a slow boot.
            [[ -z ${EVER_SEEN[$hp]:-} ]] && continue
            if (( ${GRACE[$hp]:-0} > 0 )); then    # just cycled -> still booting, do NOT re-cut
                GRACE[$hp]=$(( GRACE[$hp] - 1 )); MISS[$hp]=0; continue
            fi
            MISS[$hp]=$(( ${MISS[$hp]:-0} + 1 ))
            if (( ${MISS[$hp]} >= CONFIRM )); then
                MISS[$hp]=0
                CYCLES[$hp]=$(( ${CYCLES[$hp]:-0} + 1 ))
                if (( ${CYCLES[$hp]} > MAX_CYCLES )); then
                    # Back-off: stop Vbus-cycling a socket that never comes back (removed
                    # or dead camera, or a manager that isn't there to re-arm it). This
                    # is what kills the endless power-cycle loop. Auto-resets when the
                    # camera reappears and stays STABLE.
                    echo "$(label_of "$hp") giving up (still gone after $MAX_CYCLES power-cycles)"
                    GIVEN_UP[$hp]=1; continue
                fi
                echo "$(label_of "$hp") power-cycle"
                cycle_port "$hp"; GRACE[$hp]=$BOOT_SCANS
            fi
        done
        # A present + STABLE camera clears its trouble state (so a FUTURE drop gets a
        # fresh set of cycles). Requiring several stable scans -- not one -- means a
        # camera that only flickers back for a moment does NOT reset the back-off.
        for hp in "${!PORT_TAKEN[@]}"; do
            EVER_SEEN[$hp]=1                       # this socket has now been seen present -> a later drop is recoverable
            MISS[$hp]=0; GRACE[$hp]=0
            PRESENT_RUN[$hp]=$(( ${PRESENT_RUN[$hp]:-0} + 1 ))
            if (( ${PRESENT_RUN[$hp]} >= STABLE )); then
                [[ -n ${GIVEN_UP[$hp]:-} ]] && echo "$(label_of "$hp") back"
                unset "GIVEN_UP[$hp]"; CYCLES[$hp]=0
            fi
        done
        sleep "$SCAN"
    done
fi

# --- one-shot (manual): confirm twice before cutting ------------------------
mapfile -t miss1 < <(missing_ports)
echo ">>> empty sockets (1st scan): ${miss1[*]:-none}"
{ [[ ${#miss1[@]} -eq 0 || -z ${miss1[0]:-} ]]; } && { echo ">>> nothing to revive."; exit 0; }
echo ">>> re-checking in 2s (only cut if STILL empty)..."
sleep 2
mapfile -t miss2 < <(missing_ports)
for hp in "${miss1[@]}"; do
    [[ -z $hp ]] && continue
    if printf '%s\n' "${miss2[@]}" | grep -qxF "$hp"; then
        echo "  $hp confirmed empty -> power-cycle hub ${hp%:*} port ${hp#*:}"; cycle_port "$hp"
    else
        echo "  $hp came back meanwhile -> NOT touching it."
    fi
done
echo ">>> wait ~10s; the manager re-arms the camera on its own (re-scan)."
