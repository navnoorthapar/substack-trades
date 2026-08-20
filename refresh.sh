#!/usr/bin/env bash
# Fetch -> extract -> validate -> commit source data -> queue atomic deployment.
#
# Substack rejects datacenter IPs, so the live feed refresh runs on this Mac.
# GitHub Actions owns the tested build and deployment. This script is safe to
# schedule several times per day and safe to rerun after an interrupted push.
set -Eeuo pipefail

cd "$(dirname "$0")"
ROOT=$PWD
LOCK_REPOSITORY_ROOT=$(pwd -P)
LAST_RUN_FILE="$HOME/.substack_trades_last_run"
MIN_REFRESH_SECONDS=${MIN_REFRESH_SECONDS:-1800}
REFRESH_BUSY_EXIT_CODE_VALUE=${REFRESH_BUSY_EXIT_CODE-0}
LOCK_DIR="${TMPDIR:-/tmp}/com.navnoor.substacktrades.lock"
LOCK_OWNED=0
WORK_DIR=""
RELEASE_SITE_DIR=""
PROMOTION_ACTIVE=0
GIT_PUBLICATION_ACTIVE=0
PROMOTED_OUTPUTS=()

# Manual overlap remains a successful no-op. The bounded launchd supervisor
# opts into EX_TEMPFAIL (75), allowing it to retry if the incumbent refresh
# later fails instead of incorrectly treating lock contention as publication
# success. Reject every other override so this private scheduler contract
# cannot silently change the script's exit semantics.
if [ "$REFRESH_BUSY_EXIT_CODE_VALUE" != "0" ] \
    && [ "$REFRESH_BUSY_EXIT_CODE_VALUE" != "75" ]; then
    echo "REFRESH_BUSY_EXIT_CODE must be unset, 0, or 75." >&2
    exit 64
fi

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
    if [ -n "$RELEASE_SITE_DIR" ] && [ -d "$RELEASE_SITE_DIR" ]; then
        rm -r "$RELEASE_SITE_DIR"
    fi
    if [ -n "$WORK_DIR" ] && [ -d "$WORK_DIR" ]; then
        rm -f "$WORK_DIR"/*.json
        rm -f "$WORK_DIR"/*.tmp
        rm -f "$WORK_DIR"/*.previous-missing
        rmdir "$WORK_DIR" 2>/dev/null || true
    fi
    if [ "$LOCK_OWNED" -eq 1 ]; then
        rm -f \
            "$LOCK_DIR/pid" \
            "$LOCK_DIR/process-start" \
            "$LOCK_DIR/process-command" \
            "$LOCK_DIR/repository-root" \
            "$LOCK_DIR/ready"
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

rollback_active_promotion() {
    # Rollback is a single-entry critical section. Ignore further termination
    # signals before the first backup move so both this shell and its mv/reset
    # children finish restoring the complete snapshot byte-for-byte.
    trap - ERR
    trap '' INT TERM
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
}

on_error() {
    exit_code=$?
    trap - ERR
    rollback_active_promotion
    echo "Refresh failed at line $1 (exit $exit_code). Previous published data was preserved." >&2
    exit "$exit_code"
}

on_signal() {
    signal_name=$1
    exit_code=$2
    # A second operator signal must not interrupt the bounded byte-exact
    # rollback. EXIT remains armed so the transaction directory and owned lock
    # are removed after restoration.
    trap - ERR
    trap '' INT TERM
    rollback_active_promotion
    echo "Refresh interrupted by $signal_name (exit $exit_code). Previous published data was preserved." >&2
    exit "$exit_code"
}

trap 'cleanup $?' EXIT
trap 'on_error $LINENO' ERR
trap 'on_signal SIGINT 130' INT
trap 'on_signal SIGTERM 143' TERM

process_start_for_pid() {
    LC_ALL=C /bin/ps -ww -p "$1" -o lstart= 2>/dev/null \
        | sed 's/^[[:space:]]*//;s/[[:space:]]*$//'
}

process_command_for_pid() {
    LC_ALL=C /bin/ps -ww -p "$1" -o command= 2>/dev/null \
        | sed 's/^[[:space:]]*//;s/[[:space:]]*$//'
}

process_cwd_for_pid() {
    if [ -L "/proc/$1/cwd" ]; then
        readlink "/proc/$1/cwd" 2>/dev/null
        return
    fi
    if [ -x /usr/sbin/lsof ]; then
        /usr/sbin/lsof -a -p "$1" -d cwd -Fn 2>/dev/null \
            | sed -n 's/^n//p' \
            | sed -n '1p'
        return
    fi
    return 1
}

command_is_refresh_shell() {
    # launchd uses /bin/bash today, but an operator may legitimately invoke
    # this script with another Bash installation. Authenticate the executable
    # by its basename and allow only the three exact sole script forms operators
    # use from the already-verified repo cwd. A command that merely contains
    # "refresh.sh" (or adds shell options/code) is not an owner.
    process_command=$1
    shell_executable=${process_command%% *}
    [ "$shell_executable" != "$process_command" ] || return 1
    [ "${shell_executable##*/}" = "bash" ] || return 1
    shell_arguments=${process_command#"$shell_executable "}
    case "$shell_arguments" in
        "$LOCK_REPOSITORY_ROOT/refresh.sh"|./refresh.sh|refresh.sh) return 0 ;;
        *) return 1 ;;
    esac
}

lock_matches_live_refresh() {
    [ -f "$LOCK_DIR/ready" ] \
        && [ -f "$LOCK_DIR/pid" ] \
        && [ -f "$LOCK_DIR/process-start" ] \
        && [ -f "$LOCK_DIR/process-command" ] \
        && [ -f "$LOCK_DIR/repository-root" ] || return 1

    locked_pid=$(sed -n '1p' "$LOCK_DIR/pid")
    locked_start=$(sed -n '1p' "$LOCK_DIR/process-start")
    locked_command=$(sed -n '1p' "$LOCK_DIR/process-command")
    locked_root=$(sed -n '1p' "$LOCK_DIR/repository-root")
    [[ "$locked_pid" =~ ^[0-9]+$ ]] || return 1
    [ "$locked_root" = "$LOCK_REPOSITORY_ROOT" ] || return 1
    kill -0 "$locked_pid" 2>/dev/null || return 1

    live_start=$(process_start_for_pid "$locked_pid")
    live_command=$(process_command_for_pid "$locked_pid")
    live_root=$(process_cwd_for_pid "$locked_pid")
    [ -n "$live_start" ] && [ "$live_start" = "$locked_start" ] || return 1
    [ -n "$live_command" ] && [ "$live_command" = "$locked_command" ] || return 1
    [ "$live_root" = "$LOCK_REPOSITORY_ROOT" ] || return 1
    command_is_refresh_shell "$live_command"
}

legacy_lock_matches_live_refresh() {
    # During a rolling upgrade, an already-running pre-identity refresh may
    # have written only `pid`. Never overlap that exact repo-rooted process;
    # unlike the old implementation, an unrelated reused PID still fails the
    # command and physical-working-directory checks.
    [ -f "$LOCK_DIR/pid" ] || return 1
    locked_pid=$(sed -n '1p' "$LOCK_DIR/pid")
    [[ "$locked_pid" =~ ^[0-9]+$ ]] || return 1
    kill -0 "$locked_pid" 2>/dev/null || return 1
    live_command=$(process_command_for_pid "$locked_pid")
    live_root=$(process_cwd_for_pid "$locked_pid")
    [ "$live_root" = "$LOCK_REPOSITORY_ROOT" ] || return 1
    command_is_refresh_shell "$live_command"
}

initialize_owned_lock() {
    LOCK_OWNED=1
    current_start=$(process_start_for_pid "$$")
    current_command=$(process_command_for_pid "$$")
    if [ -z "$current_start" ] || [ -z "$current_command" ]; then
        echo "Could not establish the refresh process identity for its lock." >&2
        return 1
    fi
    printf '%s\n' "$$" > "$LOCK_DIR/pid"
    printf '%s\n' "$current_start" > "$LOCK_DIR/process-start"
    printf '%s\n' "$current_command" > "$LOCK_DIR/process-command"
    printf '%s\n' "$LOCK_REPOSITORY_ROOT" > "$LOCK_DIR/repository-root"
    printf 'ready\n' > "$LOCK_DIR/ready"
}

# Prevent a manual run and a scheduled run from mutating the same files.
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    # An owner writes `ready` last. Give a just-created lock one second to
    # finish its identity record before deciding that it is stale or foreign.
    if [ ! -f "$LOCK_DIR/ready" ]; then
        sleep 1
    fi
    lock_is_live=0
    if lock_matches_live_refresh; then
        lock_is_live=1
    elif [ ! -f "$LOCK_DIR/ready" ] && legacy_lock_matches_live_refresh; then
        lock_is_live=1
    fi
    if [ "$lock_is_live" -eq 1 ]; then
        running_pid=$locked_pid
        if [ "$REFRESH_BUSY_EXIT_CODE_VALUE" -eq 0 ]; then
            echo "A refresh is already running (PID $running_pid); exiting cleanly."
        else
            echo "A refresh is already running (PID $running_pid); deferring this scheduled attempt for retry." >&2
        fi
        exit "$REFRESH_BUSY_EXIT_CODE_VALUE"
    fi
    rm -f \
        "$LOCK_DIR/pid" \
        "$LOCK_DIR/process-start" \
        "$LOCK_DIR/process-command" \
        "$LOCK_DIR/repository-root" \
        "$LOCK_DIR/ready"
    if ! rmdir "$LOCK_DIR" 2>/dev/null; then
        echo "Refresh lock is foreign or malformed and could not be cleared safely." >&2
        exit 74
    fi
    if ! mkdir "$LOCK_DIR" 2>/dev/null; then
        echo "Refresh lock ownership changed while stale state was being cleared; retry later." >&2
        exit 74
    fi
fi
initialize_owned_lock
# The scheduler-only contention signal has served its purpose once this process
# owns the lock. Do not leak it into the release gate's child processes: nested
# refresh fixtures must retain the ordinary manual-run contract unless they
# explicitly opt into the scheduler code themselves.
unset REFRESH_BUSY_EXIT_CODE
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
echo "=== Refreshing the official Treasury par yield curve ==="
# The curve is a published series, not something this pipeline produces, so a
# feed outage must not stop a research refresh. Merging keeps every earlier
# trading day, and falling back to the tracked series keeps the rate context
# that already shipped rather than dropping it.
TREASURY_CANDIDATE="$WORK_DIR/treasury_curve.candidate.json"
if ! "$PYTHON" fetch_treasury_curve.py > "$TREASURY_CANDIDATE"; then
    if [ -f "$ROOT/treasury_curve.json" ]; then
        echo "Treasury curve refresh failed; keeping the tracked curve." >&2
        cp -p "$ROOT/treasury_curve.json" "$TREASURY_CANDIDATE"
    else
        echo "Treasury curve refresh failed and no tracked curve exists to fall back to." >&2
        exit 1
    fi
fi

echo
echo "=== Creating verifiable snapshot manifest ==="
MANIFEST_ARGS=(
    --articles "$WORK_DIR/articles.candidate.json"
    --trades "$WORK_DIR/trades.candidate.json"
    --substack-status "$WORK_DIR/substack-status.json"
    --medium-status "$WORK_DIR/medium-status.json"
    --patreon-status "$WORK_DIR/patreon-status.json"
    --output "$WORK_DIR/snapshot_manifest.candidate.json"
)
if [ -f "$ROOT/snapshot_manifest.json" ]; then
    MANIFEST_ARGS+=(--previous-manifest "$ROOT/snapshot_manifest.json")
fi
"$PYTHON" write_snapshot_manifest.py "${MANIFEST_ARGS[@]}"

# A validated cached fallback remains safer than deleting research during a
# temporary publisher outage.  Emit that state prominently here; local status
# is nonzero for any degradation and the independent watchdog escalates a
# continuous 48-hour streak.
"$PYTHON" source_health.py \
    --policy publish \
    < "$WORK_DIR/snapshot_manifest.candidate.json"

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
    treasury_curve.json
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
    "$TREASURY_CANDIDATE"
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
    rollback_active_promotion
    exit 1
fi

echo
echo "=== Building and validating the exact release candidate ==="
RELEASE_SITE_DIR="$WORK_DIR/release-site"
SITE_OUTPUT_DIR="$RELEASE_SITE_DIR" SITE_REVISION=scheduled-refresh-candidate \
    "$PYTHON" build_site.py
"$PYTHON" validate_release.py \
    --site "$RELEASE_SITE_DIR" \
    --articles articles_index.json \
    --trades trades_extracted.json \
    --manifest snapshot_manifest.json \
    --expected-revision scheduled-refresh-candidate
rm -r "$RELEASE_SITE_DIR"
RELEASE_SITE_DIR=""

TRACKED_OUTPUTS=(
    articles_index.json
    medium_posts.json
    patreon_registry.json
    trades_extracted.json
    snapshot_manifest.json
    treasury_curve.json
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
