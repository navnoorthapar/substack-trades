#!/usr/bin/env bash
# Fetch -> extract -> validate -> commit source data -> queue atomic deployment.
#
# Substack rejects datacenter IPs, so the live feed refresh runs on this Mac.
# GitHub Actions owns the tested build and deployment. This script is safe to
# schedule several times per day and safe to rerun after an interrupted push.
set -Eeuo pipefail

cd "$(dirname "$0")"
ROOT=$PWD
LAST_RUN_FILE="$HOME/.substack_trades_last_run"
MIN_REFRESH_SECONDS=${MIN_REFRESH_SECONDS:-1800}
LOCK_DIR="${TMPDIR:-/tmp}/com.navnoor.substacktrades.lock"
LOCK_OWNED=0
WORK_DIR=""

if [ -n "${PYTHON_BIN:-}" ]; then
    PYTHON=$PYTHON_BIN
elif [ -x /usr/bin/python3 ]; then
    PYTHON=/usr/bin/python3
else
    PYTHON=$(command -v python3)
fi

if [ ! -x "$PYTHON" ]; then
    echo "No working Python 3 interpreter found." >&2
    exit 1
fi

cleanup() {
    exit_code=$1
    trap - EXIT
    if [ -n "$WORK_DIR" ] && [ -d "$WORK_DIR" ]; then
        rm -f "$WORK_DIR"/*.json
        rmdir "$WORK_DIR" 2>/dev/null || true
    fi
    if [ "$LOCK_OWNED" -eq 1 ]; then
        rm -f "$LOCK_DIR/pid"
        rmdir "$LOCK_DIR" 2>/dev/null || true
    fi
    exit "$exit_code"
}

on_error() {
    exit_code=$?
    trap - ERR
    echo "Refresh failed at line $1 (exit $exit_code). Previous published data was preserved." >&2
    exit "$exit_code"
}

trap 'cleanup $?' EXIT
trap 'on_error $LINENO' ERR

# Prevent a manual run and a scheduled run from mutating the same files.
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    running_pid=""
    if [ -f "$LOCK_DIR/pid" ]; then
        running_pid=$(sed -n '1p' "$LOCK_DIR/pid")
    fi
    if [[ "$running_pid" =~ ^[0-9]+$ ]] && kill -0 "$running_pid" 2>/dev/null; then
        echo "A refresh is already running (PID $running_pid); exiting cleanly."
        exit 0
    fi
    rm -f "$LOCK_DIR/pid"
    rmdir "$LOCK_DIR" 2>/dev/null || true
    mkdir "$LOCK_DIR"
fi
LOCK_OWNED=1
printf '%s\n' "$$" > "$LOCK_DIR/pid"
WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/substack-trades-refresh.XXXXXX")

# Avoid only accidental rapid reruns. The old 20-hour gate defeated the 9am,
# 1pm, and 10pm schedule and could hide a post for almost a day.
if [ "${FORCE_REFRESH:-0}" != "1" ] && [ -f "$LAST_RUN_FILE" ]; then
    LAST=$(sed -n '1p' "$LAST_RUN_FILE")
    if [[ "$LAST" =~ ^[0-9]+$ ]]; then
        NOW=$(date +%s)
        DIFF=$((NOW - LAST))
        if [ "$DIFF" -ge 0 ] && [ "$DIFF" -lt "$MIN_REFRESH_SECONDS" ]; then
            echo "Refresh completed $((DIFF / 60)) minutes ago; skipping duplicate run."
            exit 0
        fi
    fi
fi

echo "=== Syncing with origin/main ==="
# A failed sync is fatal: continuing could create a commit that cannot publish.
git pull --rebase --autostash origin main

echo "=== Fetching posts from Substack ==="
POSTS_OUTPUT="$WORK_DIR/substack.candidate.json" \
ARTICLES_OUTPUT="$WORK_DIR/substack-articles.candidate.json" \
PREVIOUS_POSTS="$ROOT/all_posts.json" \
    "$PYTHON" fetch_all_posts.py

echo
echo "=== Fetching complete Medium archive ==="
MEDIUM_OUTPUT="$WORK_DIR/medium.candidate.json" \
PREVIOUS_MEDIUM="$ROOT/medium_posts.json" \
    "$PYTHON" fetch_medium_posts.py

echo
echo "=== Merging sources and removing Medium cross-posts ==="
SUBSTACK_POSTS="$WORK_DIR/substack.candidate.json" \
MEDIUM_POSTS="$WORK_DIR/medium.candidate.json" \
POSTS_OUTPUT="$WORK_DIR/posts.candidate.json" \
ARTICLES_OUTPUT="$WORK_DIR/articles.candidate.json" \
DEDUPE_REPORT_OUTPUT="$WORK_DIR/dedupe-report.json" \
    "$PYTHON" merge_article_sources.py

echo
echo "=== Extracting trades into an isolated candidate ==="
POSTS_INPUT="$WORK_DIR/posts.candidate.json" \
TRADES_OUTPUT="$WORK_DIR/trades.raw.json" \
    "$PYTHON" extract_trades.py

echo
echo "=== Filtering and deduplicating ==="
TRADES_INPUT="$WORK_DIR/trades.raw.json" \
TRADES_OUTPUT="$WORK_DIR/trades.candidate.json" \
    "$PYTHON" filter_trades.py

echo
echo "=== Restoring cached directions / resolving new residuals ==="
# The local model is optional and fail-safe. The tracked cache preserves prior
# validated classifications when Ollama is not running.
DIRECTION_LLM_ENABLE=1 DIRECTION_LLM_MODEL=qwen2.5:14b \
TRADES_PATH="$WORK_DIR/trades.candidate.json" \
    "$PYTHON" llm_direction.py || echo "(direction resolver skipped/failed; regex output kept)"

echo
echo "=== Validating candidate data ==="
VALIDATE_ARGS=(
    --posts "$WORK_DIR/posts.candidate.json"
    --articles "$WORK_DIR/articles.candidate.json"
    --trades "$WORK_DIR/trades.candidate.json"
)
if [ -f "$ROOT/trades_extracted.json" ]; then
    VALIDATE_ARGS+=(--previous-trades "$ROOT/trades_extracted.json")
fi
"$PYTHON" validate_pipeline.py "${VALIDATE_ARGS[@]}"
mv "$WORK_DIR/substack.candidate.json" "$ROOT/all_posts.json"
mv "$WORK_DIR/medium.candidate.json" "$ROOT/medium_posts.json"
mv "$WORK_DIR/posts.candidate.json" "$ROOT/all_sources_posts.json"
mv "$WORK_DIR/articles.candidate.json" "$ROOT/articles_index.json"
mv "$WORK_DIR/trades.candidate.json" "$ROOT/trades_extracted.json"

echo
echo "=== Running regression suite ==="
"$PYTHON" -m unittest -q

TRACKED_OUTPUTS=(articles_index.json medium_posts.json trades_extracted.json)
if [ -f .direction_cache.json ]; then
    TRACKED_OUTPUTS+=(.direction_cache.json)
fi
git add -- "${TRACKED_OUTPUTS[@]}"

if git diff --staged --quiet -- "${TRACKED_OUTPUTS[@]}"; then
    echo "No feed changes since the last successful refresh."
else
    ARTICLE_COUNT=$("$PYTHON" -c "import json; print(len(json.load(open('articles_index.json'))))")
    TRADE_COUNT=$("$PYTHON" -c "import json; print(len(json.load(open('trades_extracted.json'))))")
    echo
    echo "=== Committing ${ARTICLE_COUNT} articles / ${TRADE_COUNT} trades ==="
    git commit --only \
        -m "update: ${ARTICLE_COUNT} articles, ${TRADE_COUNT} trades ($(date -u '+%Y-%m-%d'))" \
        -- "${TRACKED_OUTPUTS[@]}"
fi

# Always push, even when this run produced no diff. This retries a commit left
# ahead of origin by a previous network failure.
echo
echo "=== Pushing validated source snapshot ==="
git push origin main

date +%s > "$LAST_RUN_FILE"

ARTICLE_COUNT=$("$PYTHON" -c "import json; print(len(json.load(open('articles_index.json'))))")
TRADE_COUNT=$("$PYTHON" -c "import json; print(len(json.load(open('trades_extracted.json'))))")
echo
echo "Done - ${ARTICLE_COUNT} articles and ${TRADE_COUNT} trades are synchronized."
echo "Changed snapshots queue a tested, atomic GitHub Pages deployment at:"
echo "https://navnoorthapar.github.io/substack-trades/"
