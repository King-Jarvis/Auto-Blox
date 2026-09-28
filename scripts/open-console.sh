#!/usr/bin/env bash
# Open the console in a browser with the current token filled in.
# Used by the desktop launcher and handy from a terminal: ./open-console.sh
set -euo pipefail

PORT="${ZERO2W_PORT:-8787}"
TOKEN_FILE="$HOME/.config/auto-blox/token"
# Before the console's first start since the rename, it is still here.
[[ -r "$TOKEN_FILE" ]] || TOKEN_FILE="$HOME/.config/zero2w-console/token"
URL="http://localhost:${PORT}/"

if [[ -r "$TOKEN_FILE" ]]; then
  URL="${URL}?t=$(tr -d '\n' < "$TOKEN_FILE")"
fi

# Wait briefly for the service, in case the launcher fired during boot.
for _ in {1..20}; do
  if curl -sf -o /dev/null "http://localhost:${PORT}/healthz"; then break; fi
  sleep 0.5
done

# --app gives a chromeless window, so it reads as a desktop app rather than a tab.
if command -v chromium >/dev/null 2>&1; then
  exec chromium --app="$URL" --window-size=1600,1000
elif command -v x-www-browser >/dev/null 2>&1; then
  exec x-www-browser "$URL"
else
  exec xdg-open "$URL"
fi
