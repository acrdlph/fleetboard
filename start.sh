#!/bin/bash
# orchestr launcher — restarts the server (kill by listening port: on macOS
# the process name is ".../Python orchestr.py", so pkill by name won't match)
# and opens the dashboard. Extra args are passed through to orchestr.py.
cd "$(dirname "$0")"
PORT="${ORCHESTR_PORT:-4242}"
OLD=$(lsof -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null)
[ -n "$OLD" ] && kill $OLD && sleep 1
nohup python3 orchestr.py "$@" > /tmp/orchestr.log 2>&1 &
sleep 1
URL="http://127.0.0.1:$PORT"
command -v open >/dev/null && open "$URL" || command -v xdg-open >/dev/null && xdg-open "$URL"
echo "orchestr → $URL (log: /tmp/orchestr.log)"
