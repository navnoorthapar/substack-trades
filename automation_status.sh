#!/usr/bin/env bash
set -uo pipefail

LABEL=com.navnoor.substacktrades
DOMAIN="gui/$(id -u)"
LAST_RUN_FILE="$HOME/.substack_trades_last_run"
MAX_AGE_SECONDS=${MAX_AGE_SECONDS:-57600} # 16 hours; the longest normal schedule gap is 11 hours
REPOSITORY=navnoorthapar/substack-trades
ok=1
updater_issue=0
updater_loaded=0
updater_exit_issue=0
refresh_issue=0
deployment_issue=0
failed_run_id=""

launchctl_output=$(launchctl print "$DOMAIN/$LABEL" 2>&1)
launchctl_status=$?
if [ "$launchctl_status" -eq 0 ]; then
    echo "Updater: loaded"
    updater_loaded=1
    last_exit_code=$(printf '%s\n' "$launchctl_output" | awk '
        $1 == "last" && $2 == "exit" && $3 == "code" && $4 == "=" &&
        $5 ~ /^-?[0-9]+$/ { print $5; exit }
    ')
    if [ "$last_exit_code" = "0" ]; then
        echo "Updater last exit: successful"
    elif [[ "$last_exit_code" =~ ^-?[0-9]+$ ]]; then
        echo "Updater last exit: FAILED (code $last_exit_code)"
        ok=0
        updater_issue=1
        updater_exit_issue=1
    else
        echo "Updater last exit: unavailable (no completed run recorded)"
        ok=0
        updater_issue=1
        updater_exit_issue=1
    fi
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
    if [ "$updater_loaded" -eq 0 ]; then
        echo "Repair updater with: $ROOT/install_automation.sh"
    elif [ "$updater_exit_issue" -eq 1 ]; then
        echo "Inspect updater errors: $HOME/Library/Logs/SubstackTrades/refresh-error.log"
        echo "Run updater now with: launchctl kickstart -k $DOMAIN/$LABEL"
    fi
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
