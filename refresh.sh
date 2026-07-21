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
PROMOTION_ACTIVE=0
GIT_PUBLICATION_ACTIVE=0
PROMOTED_OUTPUTS=()

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
        rm -f "$WORK_DIR"/*.tmp
        rm -f "$WORK_DIR"/*.previous-missing
        rmdir "$WORK_DIR" 2>/dev/null || true
    fi
    if [ "$LOCK_OWNED" -eq 1 ]; then
        rm -f "$LOCK_DIR/pid"
        rmdir "$LOCK_DIR" 2>/dev/null || true
    fi
    exit "$exit_code"
}

restore_promoted_outputs() {
    if [ "$PROMOTION_ACTIVE" -ne 1 ]; then
        return 0
    fi

    rollback_failed=0
    for index in "${!PROMOTED_OUTPUTS[@]}"; do
        output=${PROMOTED_OUTPUTS[$index]}
        previous="$WORK_DIR/promoted-$index.previous.json"
        missing="$WORK_DIR/promoted-$index.previous-missing"
        if [ -f "$previous" ]; then
            if ! mv "$previous" "$ROOT/$output"; then
                echo "Could not restore $output from the refresh transaction backup." >&2
                rollback_failed=1
            fi
        elif [ -f "$missing" ]; then
            if ! rm -f "$ROOT/$output"; then
                echo "Could not remove newly promoted $output during rollback." >&2
                rollback_failed=1
            fi
        else
            echo "Refresh transaction backup is missing for $output." >&2
            rollback_failed=1
        fi
    done
    PROMOTION_ACTIVE=0
    return "$rollback_failed"
}

on_error() {
    exit_code=$?
    trap - ERR
    if [ "$PROMOTION_ACTIVE" -eq 1 ]; then
        echo "Refresh failed before the validated snapshot was committed; restoring the previous local snapshot." >&2
        if ! restore_promoted_outputs; then
            echo "Refresh rollback was incomplete; manual recovery is required before another run." >&2
        fi
        if [ "$GIT_PUBLICATION_ACTIVE" -eq 1 ]; then
            if ! git reset --quiet HEAD -- \
                articles_index.json medium_posts.json patreon_registry.json \
                trades_extracted.json \
                snapshot_manifest.json .direction_cache.json; then
                echo "Could not clear the failed publication staging state; manual recovery is required." >&2
            fi
        fi
    fi
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
# Never let the scheduled production writer run from a feature branch or a
# detached checkout. Its commit and push targets must describe the same branch.
CURRENT_BRANCH=$(git branch --show-current)
if [ "$CURRENT_BRANCH" != "main" ]; then
    echo "Production refresh must run from the checked-out main branch (current: ${CURRENT_BRANCH:-detached HEAD})." >&2
    exit 1
fi
# Production ingestion must run from reviewable, committed code. Ignored local
# caches and previews are harmless, but any staged, unstaged, or untracked
# source file makes the run fail closed instead of autostashing development.
WORKTREE_STATUS=$(git status --porcelain --untracked-files=normal)
if [ -n "$WORKTREE_STATUS" ]; then
    echo "Production refresh requires a clean worktree. Commit or remove these changes first:" >&2
    printf '%s\n' "$WORKTREE_STATUS" >&2
    exit 1
fi
# A failed or non-fast-forward sync is fatal: continuing could publish data
# produced by code that does not match main.
git pull --ff-only origin main

echo "=== Fetching posts from Substack ==="
POSTS_OUTPUT="$WORK_DIR/substack.candidate.json" \
ARTICLES_OUTPUT="$WORK_DIR/substack-articles.candidate.json" \
PREVIOUS_POSTS="$ROOT/all_posts.json" \
FETCH_STATUS_OUTPUT="$WORK_DIR/substack-status.json" \
    "$PYTHON" fetch_all_posts.py

echo
echo "=== Fetching complete Medium archive ==="
MEDIUM_OUTPUT="$WORK_DIR/medium.candidate.json" \
PREVIOUS_MEDIUM="$ROOT/medium_posts.json" \
FETCH_STATUS_OUTPUT="$WORK_DIR/medium-status.json" \
    "$PYTHON" fetch_medium_posts.py

echo
echo "=== Fetching sparse public Patreon catalogue metadata ==="
PATREON_OUTPUT="$WORK_DIR/patreon.candidate.json" \
PREVIOUS_PATREON="$ROOT/patreon_registry.json" \
PATREON_STATUS_OUTPUT="$WORK_DIR/patreon-status.json" \
    "$PYTHON" fetch_patreon_posts.py

echo
echo "=== Merging sources, registries, and reviewed cross-posts ==="
SUBSTACK_POSTS="$WORK_DIR/substack.candidate.json" \
MEDIUM_POSTS="$WORK_DIR/medium.candidate.json" \
PATREON_REGISTRY="$WORK_DIR/patreon.candidate.json" \
FXEMPIRE_REGISTRY="$ROOT/fxempire_registry.json" \
REGISTRY_OVERRIDES="$ROOT/registry_crosslink_overrides.json" \
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
# validated classifications when Ollama is not running. Work against a private
# candidate so an invalid snapshot can never dirty the scheduled writer's
# tracked cache or block the next run.
DIRECTION_CACHE_CANDIDATE="$WORK_DIR/direction-cache.candidate.json"
if [ -f "$ROOT/.direction_cache.json" ]; then
    cp -p "$ROOT/.direction_cache.json" "$DIRECTION_CACHE_CANDIDATE"
else
    printf '{}\n' > "$DIRECTION_CACHE_CANDIDATE"
fi
DIRECTION_LLM_ENABLE=1 DIRECTION_LLM_MODEL=qwen2.5:14b \
TRADES_PATH="$WORK_DIR/trades.candidate.json" \
DIRECTION_CACHE_PATH="$DIRECTION_CACHE_CANDIDATE" \
    "$PYTHON" llm_direction.py || echo "(direction resolver skipped/failed; regex output kept)"

echo
echo "=== Creating verifiable snapshot manifest ==="
"$PYTHON" write_snapshot_manifest.py \
    --articles "$WORK_DIR/articles.candidate.json" \
    --trades "$WORK_DIR/trades.candidate.json" \
    --substack-status "$WORK_DIR/substack-status.json" \
    --medium-status "$WORK_DIR/medium-status.json" \
    --patreon-status "$WORK_DIR/patreon-status.json" \
    --output "$WORK_DIR/snapshot_manifest.candidate.json"

echo
echo "=== Validating candidate data ==="
VALIDATE_ARGS=(
    --posts "$WORK_DIR/posts.candidate.json"
    --articles "$WORK_DIR/articles.candidate.json"
    --trades "$WORK_DIR/trades.candidate.json"
    --manifest "$WORK_DIR/snapshot_manifest.candidate.json"
)
if [ -f "$ROOT/articles_index.json" ]; then
    VALIDATE_ARGS+=(--previous-articles "$ROOT/articles_index.json")
fi
if [ -f "$ROOT/trades_extracted.json" ]; then
    VALIDATE_ARGS+=(--previous-trades "$ROOT/trades_extracted.json")
fi
if [ -f "$ROOT/snapshot_manifest.json" ]; then
    VALIDATE_ARGS+=(--previous-manifest "$ROOT/snapshot_manifest.json")
fi
"$PYTHON" validate_pipeline.py "${VALIDATE_ARGS[@]}"

# Keep a transaction-local copy so a regression failure after candidate
# promotion restores the exact previous workspace state. The live site is
# already protected by the deployment quality gate; this also keeps the next
# scheduled local run clean and repeatable.
PROMOTED_OUTPUTS=(
    all_posts.json
    medium_posts.json
    patreon_registry.json
    all_sources_posts.json
    articles_index.json
    trades_extracted.json
    snapshot_manifest.json
    .direction_cache.json
)
PROMOTION_CANDIDATES=(
    "$WORK_DIR/substack.candidate.json"
    "$WORK_DIR/medium.candidate.json"
    "$WORK_DIR/patreon.candidate.json"
    "$WORK_DIR/posts.candidate.json"
    "$WORK_DIR/articles.candidate.json"
    "$WORK_DIR/trades.candidate.json"
    "$WORK_DIR/snapshot_manifest.candidate.json"
    "$DIRECTION_CACHE_CANDIDATE"
)
for index in "${!PROMOTED_OUTPUTS[@]}"; do
    output=${PROMOTED_OUTPUTS[$index]}
    if [ -f "$ROOT/$output" ]; then
        cp -p "$ROOT/$output" "$WORK_DIR/promoted-$index.previous.json"
    else
        : > "$WORK_DIR/promoted-$index.previous-missing"
    fi
done

PROMOTION_ACTIVE=1
for index in "${!PROMOTED_OUTPUTS[@]}"; do
    mv "${PROMOTION_CANDIDATES[$index]}" "$ROOT/${PROMOTED_OUTPUTS[$index]}"
done

echo
echo "=== Running regression suite ==="
if ! "$PYTHON" -m unittest -q; then
    echo "Regression suite failed; restoring the previous local snapshot." >&2
    if ! restore_promoted_outputs; then
        echo "Refresh rollback was incomplete; manual recovery is required before another run." >&2
    fi
    exit 1
fi
TRACKED_OUTPUTS=(
    articles_index.json
    medium_posts.json
    patreon_registry.json
    trades_extracted.json
    snapshot_manifest.json
)
if [ -f .direction_cache.json ]; then
    TRACKED_OUTPUTS+=(.direction_cache.json)
fi
# Keep rollback armed through staging and the local commit. If either operation
# fails, the error trap restores and unstages the old snapshot so the next
# scheduled run starts clean. Once a commit exists, the worktree is clean and a
# push failure is safely retried by the next run.
GIT_PUBLICATION_ACTIVE=1
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
GIT_PUBLICATION_ACTIVE=0
PROMOTION_ACTIVE=0

# Always push, even when this run produced no diff. This retries a commit left
# ahead of origin by a previous network failure.
echo
echo "=== Pushing validated source snapshot ==="
push_succeeded=0
for attempt in 1 2 3; do
    if git push origin main; then
        push_succeeded=1
        break
    fi
    if [ "$attempt" -lt 3 ]; then
        retry_delay=$((attempt * 20))
        echo "Push attempt $attempt failed; retrying in ${retry_delay}s." >&2
        sleep "$retry_delay"
    fi
done
if [ "$push_succeeded" -ne 1 ]; then
    echo "Validated snapshot could not be pushed after three attempts." >&2
    exit 1
fi

date +%s > "$LAST_RUN_FILE"

ARTICLE_COUNT=$("$PYTHON" -c "import json; print(len(json.load(open('articles_index.json'))))")
TRADE_COUNT=$("$PYTHON" -c "import json; print(len(json.load(open('trades_extracted.json'))))")
echo
echo "Done - ${ARTICLE_COUNT} articles and ${TRADE_COUNT} trades are synchronized."
echo "Changed snapshots queue a tested, atomic GitHub Pages deployment at:"
echo "https://navnoorthapar.github.io/substack-trades/"
