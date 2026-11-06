#!/usr/bin/env bash
# Demonstration: the agent draws a small scene in Excalidraw on its own private display while the
# session is recorded. The user's pointer, keyboard and workspace are never touched.
#
# Usage: examples/excalidraw-demo.sh [OUTPUT.mp4]
set -euo pipefail

OUT=${1:-"$(xdg-user-dir VIDEOS 2>/dev/null || echo "$HOME/Videos")/adc-excalidraw-demo.mp4"}

adc start --nested --size 1600x900 --record "$OUT" >/dev/null
trap 'adc stop >/dev/null 2>&1 || true' EXIT

adc browser "https://excalidraw.com" --name demo >/dev/null
sleep 10                                                # let the page load

shape() {  # shape <tool-key> <x1> <y1> <x2> <y2> <label>
  adc key "$1" --label "$6" >/dev/null
  adc drag-xy "$2" "$3" "$4" "$5" --label "$6" >/dev/null
}

adc key Escape --label "Starting on the canvas" >/dev/null
shape r 620 470 900 720 "House walls"
shape r 730 600 800 720 "Door"
shape r 645 510 705 565 "Window"
shape l 600 470 760 340 "Roof"
shape l 760 340 920 470 "Roof"
shape o 1180 140 1300 260 "Sun"
shape o 1000 520 1110 640 "Tree"
shape r 1045 640 1065 720 "Trunk"
shape l 450 722 1350 722 "Ground"

adc key t --label "Text tool" >/dev/null
adc click-xy 470 790 --label "Signing the drawing" >/dev/null
adc write "Drawn by an agent on its own display; the user's mouse was never touched." \
  --delay 30 --label "Typing" >/dev/null
adc key Escape >/dev/null
adc move 1400 820 --label "Done" >/dev/null
sleep 2

echo "Recording: $OUT"
