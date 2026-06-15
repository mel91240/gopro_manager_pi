#!/bin/bash
# Thin CLI wrapper around gopro_download.py: offload recorded videos from the
# GoPros to the Pi/SSD (auto parallel across cameras, resumable, live progress).
# gopro.sh [2] is the menu-driven way; this is the direct CLI. Run --help.
#
#   ./download.sh                  all cameras -> ~/gopro_footage
#   ./download.sh --pick           list the clips and choose which to copy
#   ./download.sh --minsec 10      skip clips shorter than 10 s
#   GOPRO_DEST=/mnt/ssd/gopro ./download.sh
exec python3 "$(cd "$(dirname "$0")" && pwd)/gopro_download.py" "$@"
