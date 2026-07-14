#!/usr/bin/env bash
set -uo pipefail

LABEL=com.navnoor.substacktrades
DOMAIN="gui/$(id -u)"
LAST_RUN_FILE="$HOME/.substack_trades_last_run"
MAX_AGE_SECONDS=${MAX_AGE_SECONDS:-129600} # 36 hours
REPOSITORY=navnoorthapar/substack-trades
ok=1
updater_issue=0
refresh_issue=0
deployment_issue=0
failed_run_id=""

if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    echo "Updater: loaded"
else
    echo "Updater: NOT LOADED"
    ok=0
    updater_issue=1
fi

if [ -f "$LAST_RUN_FILE" ]; then
    last=$(sed -n '1p' "$LAST_RUN_FILE")
    if [[ "$last" =~ ^[0-9]+$ ]]; then
        now=$(date +%s)
        age=$((now - last))
        if [ "$age" -ge 0 ] && [ "$age" -le "$MAX_AGE_SECONDS" ]; then
            echo "Last successful data refresh: $((age / 3600)) hours ago"
        else
            echo "Last successful data refresh: STALE ($((age / 3600)) hours ago)"
            ok=0
            refresh_issue=1
        fi
    else
        echo "Last successful data refresh marker is invalid"
        ok=0
        refresh_issue=1
    fi
else
    echo "No successful data refresh marker found"
    ok=0
    refresh_issue=1
fi

if command -v gh >/dev/null 2>&1; then
    pages_mode=$(gh api "repos/$REPOSITORY/pages" --jq .build_type 2>/dev/null || true)
    if [ "$pages_mode" = "workflow" ]; then
        echo "Deployment mode: GitHub Actions"
    elif [ -n "$pages_mode" ]; then
        echo "Deployment mode: unexpected ($pages_mode)"
        ok=0
        deployment_issue=1
    else
        echo "Deployment mode: unavailable (could not query GitHub)"
        ok=0
        deployment_issue=1
    fi

    latest_run=$(gh run list \
        --repo "$REPOSITORY" \
        --workflow update.yml \
        --branch main \
        --limit 1 \
        --json status,conclusion,databaseId \
        --jq '.[0] | "\(.status)|\(.conclusion // "")|\(.databaseId)"' \
        2>/dev/null || true)
    IFS='|' read -r run_status run_conclusion run_id <<< "$latest_run"
    if [ "$run_status" = "completed" ] && [ "$run_conclusion" = "success" ]; then
        echo "Latest deployment: successful (run $run_id)"
    elif [ "$run_status" = "queued" ] || [ "$run_status" = "in_progress" ]; then
        echo "Latest deployment: $run_status (run $run_id)"
    elif [ -n "$run_status" ]; then
        echo "Latest deployment: ${run_conclusion:-$run_status} (run $run_id)"
        ok=0
        deployment_issue=1
        failed_run_id=$run_id
    else
        echo "Latest deployment: unavailable (no workflow run found)"
        ok=0
        deployment_issue=1
    fi
else
    echo "Deployment status: unavailable (GitHub CLI not installed)"
    ok=0
    deployment_issue=1
fi

if [ "$ok" -eq 1 ]; then
    exit 0
fi

ROOT=$(cd "$(dirname "$0")" && pwd)
if [ "$updater_issue" -eq 1 ]; then
    echo "Repair updater with: $ROOT/install_automation.sh"
fi
if [ "$refresh_issue" -eq 1 ]; then
    echo "Run a fresh ingestion with: $ROOT/refresh.sh"
fi
if [ "$deployment_issue" -eq 1 ]; then
    if [ -n "$failed_run_id" ]; then
        echo "Inspect deployment with: gh run view --repo $REPOSITORY $failed_run_id --log-failed"
    else
        echo "Inspect deployment with: gh run list --repo $REPOSITORY --workflow update.yml --limit 5"
    fi
fi
exit 1
