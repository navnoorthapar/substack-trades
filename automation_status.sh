#!/usr/bin/env bash
set -uo pipefail

LABEL=com.navnoor.substacktrades
DOMAIN="gui/$(id -u)"
LAST_RUN_FILE="$HOME/.substack_trades_last_run"
MAX_AGE_SECONDS=${MAX_AGE_SECONDS:-129600} # 36 hours
ok=1

if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    echo "Updater: loaded"
else
    echo "Updater: NOT LOADED"
    ok=0
fi

if [ -f "$LAST_RUN_FILE" ]; then
    last=$(sed -n '1p' "$LAST_RUN_FILE")
    if [[ "$last" =~ ^[0-9]+$ ]]; then
        now=$(date +%s)
        age=$((now - last))
        if [ "$age" -ge 0 ] && [ "$age" -le "$MAX_AGE_SECONDS" ]; then
            echo "Last successful publish: $((age / 3600)) hours ago"
        else
            echo "Last successful publish: STALE ($((age / 3600)) hours ago)"
            ok=0
        fi
    else
        echo "Last successful publish marker is invalid"
        ok=0
    fi
else
    echo "No successful publish marker found"
    ok=0
fi

if [ "$ok" -eq 1 ]; then
    exit 0
fi

echo "Repair with: $(cd "$(dirname "$0")" && pwd)/install_automation.sh"
exit 1
