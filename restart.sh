#!/bin/sh
# Restart the dashboard server so Python changes take effect.
#
#   ./restart.sh           refuse if that would interrupt live trading
#   ./restart.sh --force    restart anyway
#
# HTML/CSS/JS changes do NOT need this — just refresh the browser.

cd "$(dirname "$0")" || exit 1
PY="./.venv/bin/python"
PORT=$(sed -n 's/.*DASHBOARD_PORT=\([0-9]*\).*/\1/p' .env 2>/dev/null)
[ -z "$PORT" ] && PORT=5050
FORCE=""
[ "$1" = "--force" ] && FORCE=1

status=$(curl -s --max-time 4 "http://localhost:$PORT/api/status" 2>/dev/null)

if [ -n "$status" ]; then
    read -r running positions market <<EOF
$(printf '%s' "$status" | "$PY" -c "
import sys, json
j = json.load(sys.stdin)
print(j.get('running'), len(j.get('positions') or []), j.get('market_open'))
" 2>/dev/null)
EOF

    echo "  שרת פעיל: בוט=$running פוזיציות=$positions שוק_פתוח=$market"

    # A restart kills the bot loop. Positions stay safe (protective stops sit at
    # the broker), but the trailing stop stops advancing until it comes back up.
    if [ -z "$FORCE" ] && [ "$running" = "True" ] && [ "$positions" != "0" ]; then
        echo
        echo "  ⚠  הבוט מנהל $positions פוזיציות פתוחות."
        echo "     הפעלה מחדש עוצרת את הטריילינג לכמה שניות (הסטופ אצל הברוקר נשאר פעיל)."
        echo "     להפעיל בכל זאת:  ./restart.sh --force"
        exit 1
    fi
fi

pids=$(lsof -ti:"$PORT" 2>/dev/null)
if [ -n "$pids" ]; then
    echo "$pids" | xargs kill 2>/dev/null
    n=0
    while [ -n "$(lsof -ti:"$PORT" 2>/dev/null)" ] && [ "$n" -lt 20 ]; do
        sleep 0.5
        n=$((n + 1))
    done
    echo "  השרת הישן נעצר"
fi

nohup "$PY" server.py > server.log 2>&1 &

n=0
while [ "$n" -lt 40 ]; do
    sleep 0.5
    if curl -s --max-time 2 "http://localhost:$PORT/api/status" >/dev/null 2>&1; then
        echo "  השרת עלה מחדש"
        grep -E "מהמחשב|מהטלפון|http://" server.log | head -4
        exit 0
    fi
    n=$((n + 1))
done

echo "  ✗ השרת לא ענה תוך 20 שניות — בדוק את server.log"
tail -15 server.log
exit 1
