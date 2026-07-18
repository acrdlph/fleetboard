#!/bin/bash
# fleetboard launcher — restarts the server (kill by listening port: on macOS
# the process name is ".../Python fleetboard.py", so pkill by name won't match)
# and opens the dashboard. Extra args are passed through to fleetboard.py.
cd "$(dirname "$0")"
PORT="${FLEETBOARD_PORT:-4242}"
OLD=$(lsof -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null)
[ -n "$OLD" ] && kill $OLD && sleep 1
nohup python3 fleetboard.py "$@" > /tmp/fleetboard.log 2>&1 &
sleep 1
URL="http://127.0.0.1:$PORT"
command -v open >/dev/null && open "$URL" || command -v xdg-open >/dev/null && xdg-open "$URL"
echo "fleetboard → $URL (log: /tmp/fleetboard.log)"
