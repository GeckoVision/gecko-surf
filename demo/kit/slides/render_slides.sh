#!/usr/bin/env bash
# Render the explainer stills to PNG. Headless Chrome, no server, no network beyond the
# webfont import — the numbers are baked into the HTML at authoring time from real output
# (see README-slides.md for where each one came from).
#
#   ./demo/kit/slides/render_slides.sh
#
# 1477x803 matches the source deck's frame, so a still can sit beside one in a video
# without a resize.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

CHROME="${CHROME:-$(command -v google-chrome || command -v chromium || true)}"
if [[ -z "$CHROME" ]]; then
  echo "no chrome/chromium on PATH — set CHROME=/path/to/chrome" >&2
  exit 1
fi

for slide in *.html; do
  name="${slide%.html}"
  "$CHROME" --headless --disable-gpu --no-sandbox --hide-scrollbars \
    --window-size=1477,803 --virtual-time-budget=4000 \
    --screenshot="$name.png" "file://$PWD/$slide" >/dev/null 2>&1
  echo "wrote $name.png"
done
