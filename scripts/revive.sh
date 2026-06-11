#!/bin/bash
# revive.sh -- safely power-cycle a GoPro that has fallen OFF the USB bus.
#
# SAFETY RULE (the rig's hard constraint): we ONLY cut Vbus on a port whose
# camera is NOT visible on the USB bus -- i.e. the camera is truly off /
# disconnected, so it CANNOT be writing to its SD. A camera that is still
# visible (enumerated) but merely silent might still be recording, so we NEVER
# power-cycle it. Cutting Vbus during a write is what corrupts the card.
#
# The script learns where GoPros live (the hub ports it has seen them on) and
# stores that in .gopro_ref, so it can target the exact dead port. Run it after
# a reboot if a camera did not come back.
#
#   ./revive.sh
#   ./manager_down.sh && ./manager_up.sh   # then refresh the manager
set -u
REF="$(cd "$(dirname "$0")" && pwd)/.gopro_ref"

echo ">>> Reading USB hub state..."
STATUS=$(sudo uhubctl 2>/dev/null) || { echo "!!! uhubctl failed (need sudo / uhubctl installed)"; exit 1; }

# --- which hub:port currently have a GoPro enumerated ---------------------
declare -A HAS
hub=
while IFS= read -r line; do
    if [[ $line =~ Current\ status\ for\ hub\ ([^ ]+) ]]; then hub=${BASH_REMATCH[1]}; fi
    if [[ $line =~ Port\ ([0-9]+): ]]; then
        p=${BASH_REMATCH[1]}
        [[ $line == *GoPro* ]] && HAS["$hub:$p"]=1
    fi
done <<< "$STATUS"

# --- remember every GoPro location we currently see (builds up over time) --
touch "$REF"
for hp in "${!HAS[@]}"; do
    grep -qxF "$hp" "$REF" || { echo "$hp" >> "$REF"; echo "  learned GoPro port $hp"; }
done

# --- report present vs missing --------------------------------------------
echo ">>> Known GoPro ports: $(tr '\n' ' ' < "$REF")"
missing=()
while IFS= read -r hp; do
    [[ -z $hp ]] && continue
    if [[ -n ${HAS[$hp]:-} ]]; then
        echo "  [$hp] GoPro present  -> leave alone (never Vbus a visible camera)"
    else
        echo "  [$hp] GoPro MISSING (off the bus) -> safe to power-cycle"
        missing+=("$hp")
    fi
done < "$REF"

if [[ ${#missing[@]} -eq 0 ]]; then
    echo ">>> All known GoPros are present. Nothing to revive."
    exit 0
fi

# --- power-cycle only the missing (truly-off) ports -----------------------
echo ">>> Reviving ${#missing[@]} missing camera(s)..."
for hp in "${missing[@]}"; do
    h=${hp%:*}; p=${hp#*:}
    echo "  power-cycling hub $h port $p (Vbus off 4s -> on)"
    sudo uhubctl -l "$h" -p "$p" -a cycle -d 4 >/dev/null 2>&1
done

echo ">>> Waiting ~22s for boot + USB enumeration..."
sleep 22
echo ">>> Re-check (GoPros now visible):"
sudo uhubctl 2>/dev/null | grep -i gopro | sed 's/^/  /' \
    || echo "  (none visible yet -- give it more time, or check the cable/camera)"
echo ""
echo ">>> If a camera came back, refresh the manager:  ./manager_down.sh && ./manager_up.sh"
