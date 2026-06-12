#!/bin/bash
# download.sh -- offload recorded videos from the GoPros to the Pi.
# Thin wrapper around gopro_download.py (parallel across cameras, optional
# duration filter and interactive picker). The Python module is reusable by
# the pi_menu. Run --help for all options.
#
#   ./download.sh                  all cameras -> ~/gopro_footage  (parallel)
#   ./download.sh --pick           list the clips and choose which to copy
#   ./download.sh --minsec 10      skip clips shorter than 10 s
#   ./download.sh --all            include tiny (<2 MB) test clips too
#   GOPRO_DEST=/mnt/usb ./download.sh
exec python3 "$(cd "$(dirname "$0")" && pwd)/gopro_download.py" "$@"
