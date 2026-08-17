#!/bin/sh
# Shoot the fire map on a connected badge, against a statsbadge checkout.
#
#     tools/stage_shots.sh /dev/cu.usbmodem101 ../statsbadge
#
# The page, the frames and the shot script are staged into that checkout's build/, which is
# ignored there. Nothing outside build/ is touched.
set -eu

PORT="${1:-/dev/cu.usbmodem101}"
CHECKOUT="${2:-../statsbadge}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

if [ ! -d "$CHECKOUT/src/statsbadge/badge_app" ]; then
    echo "no statsbadge checkout at $CHECKOUT" >&2
    exit 1
fi
if [ ! -f "$HERE/tools/sample_frames.json" ]; then
    echo "no captured frames; run: python tools/capture_frames.py" >&2
    exit 1
fi

mkdir -p "$CHECKOUT/build/shots"
cp "$HERE/src/statsbadge_wildfires/badge/firemap.py" "$CHECKOUT/build/firemap.py"
cp "$HERE/tools/sample_frames.json" "$CHECKOUT/build/sample_frames.json"
cp "$HERE/tools/shoot_firemap.py" "$CHECKOUT/build/shoot_firemap.py"

cleanup() {
    rm -f "$CHECKOUT/build/firemap.py" "$CHECKOUT/build/sample_frames.json" \
          "$CHECKOUT/build/shoot_firemap.py"
}
trap cleanup EXIT

cd "$CHECKOUT"
mpremote connect "$PORT" mount . run build/shoot_firemap.py
python3 tools/shots.py build/shots
