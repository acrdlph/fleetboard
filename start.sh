#!/bin/bash
# orchestra launcher — restarts the server (kill by listening port: on macOS
# the process name is ".../Python -m orchestra", so pkill by name won't match)
# and opens the dashboard. Extra args are passed through to `python3 -m orchestra`.
cd "$(dirname "$0")"
PORT="${ORCHESTRA_PORT:-4242}"
OLD=$(lsof -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null)
[ -n "$OLD" ] && kill $OLD && sleep 1
nohup python3 -m orchestra "$@" > /tmp/orchestra.log 2>&1 &
sleep 1
URL="http://127.0.0.1:$PORT"
if command -v open >/dev/null 2>&1; then open "$URL"          # macOS
elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL" # linux
fi
echo "orchestra → $URL (log: /tmp/orchestra.log)"
