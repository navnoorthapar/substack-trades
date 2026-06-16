#!/usr/bin/env bash
# Full pipeline: fetch → extract → filter → build → push.
#
# This is the AUTOMATED refresh, run from this Mac by the LaunchAgent
# com.navnoor.substacktrades at 9am / 1pm / 8pm. It must run here (a residential
# IP) because Substack returns HTTP 403 to datacenter/cloud IPs — so the fetch
# cannot be moved to GitHub Actions. The cloud workflow only rebuilds the site
# on demand. Safe to call multiple times per day — skips if it already ran
# successfully in the last 20h.
set -e

cd "$(dirname "$0")"

LAST_RUN_FILE="$HOME/.substack_trades_last_run"

# Skip if already ran successfully in the last 20 hours
if [ -f "$LAST_RUN_FILE" ]; then
    LAST=$(cat "$LAST_RUN_FILE")
    NOW=$(date +%s)
    DIFF=$(( NOW - LAST ))
    if [ "$DIFF" -lt 72000 ]; then
        echo "Already ran $(( DIFF / 3600 ))h ago — skipping."
        exit 0
    fi
fi

# Start from the latest main so a manual run rebases onto any cloud commits
# instead of failing to push with a non-fast-forward error.
echo "=== Syncing with origin/main ==="
git pull --rebase --autostash origin main || true

echo "=== Fetching posts from Substack ==="
python3 fetch_all_posts.py

echo ""
echo "=== Extracting trades ==="
python3 extract_trades.py

echo ""
echo "=== Filtering & deduplicating ==="
python3 filter_trades.py

# Only rebuild + push if trades_extracted.json actually changed
git add trades_extracted.json
if git diff --staged --quiet; then
    echo "No new trades since last run."
    git restore --staged trades_extracted.json 2>/dev/null || true
    # Still mark as ran so we don't hammer Substack again today
    date +%s > "$LAST_RUN_FILE"
    exit 0
fi

echo ""
echo "=== Building site ==="
python3 build_site.py

echo ""
echo "=== Committing and pushing ==="
git add trades_extracted.json docs/index.html
TRADE_COUNT=$(python3 -c "import json; print(len(json.load(open('trades_extracted.json'))))")
git commit -m "update: ${TRADE_COUNT} trades ($(date -u '+%Y-%m-%d'))"
git push origin main

date +%s > "$LAST_RUN_FILE"

echo ""
echo "Done — ${TRADE_COUNT} trades live at:"
echo "https://navnoorthapar.github.io/substack-trades/"
