#!/usr/bin/env bash
set -uo pipefail

LABEL=com.navnoor.substacktrades
DOMAIN="gui/$(id -u)"
ROOT=$(cd "$(dirname "$0")" && pwd)
LAST_RUN_FILE="$HOME/.substack_trades_last_run"
SNAPSHOT_MANIFEST_PATH=${SNAPSHOT_MANIFEST_PATH:-"$ROOT/snapshot_manifest.json"}
MAX_AGE_SECONDS=${MAX_AGE_SECONDS:-57600} # 16 hours; the longest normal schedule gap is 11 hours
REPOSITORY=navnoorthapar/substack-trades
ok=1
updater_issue=0
updater_loaded=0
updater_exit_issue=0
updater_pending=0
refresh_issue=0
source_health_issue=0
deployment_issue=0
deployment_pending=0
watchdog_issue=0
watchdog_pending=0
failed_run_id=""
failed_watchdog_run_id=""

launchctl_output=$(launchctl print "$DOMAIN/$LABEL" 2>&1)
launchctl_status=$?
if [ "$launchctl_status" -eq 0 ]; then
    echo "Updater: loaded"
    updater_loaded=1
    updater_state=$(printf '%s\n' "$launchctl_output" | awk '
        $1 == "state" && $2 == "=" && NF >= 3 {
            for (field = 3; field <= NF; field++) {
                printf "%s%s", (field == 3 ? "" : " "), $field
            }
            print ""
            exit
        }
    ')
    if [ "$updater_state" = "running" ]; then
        echo "Updater activity: scheduled refresh/retry cycle in progress"
        ok=0
        updater_pending=1
    elif [ -n "$updater_state" ]; then
        echo "Updater activity: idle ($updater_state)"
    else
        echo "Updater activity: unavailable"
        ok=0
        updater_issue=1
    fi
    if [ "$updater_pending" -eq 1 ]; then
        echo "Updater last exit: deferred until active refresh completes"
    else
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

if [ -n "${PYTHON_BIN:-}" ]; then
    SOURCE_HEALTH_PYTHON=$PYTHON_BIN
elif [ -x /usr/bin/python3 ]; then
    SOURCE_HEALTH_PYTHON=/usr/bin/python3
else
    SOURCE_HEALTH_PYTHON=$(command -v python3 || true)
fi
if [ -z "$SOURCE_HEALTH_PYTHON" ]; then
    echo "Source health: unavailable (Python 3 not found)"
    ok=0
    source_health_issue=1
elif ! "$SOURCE_HEALTH_PYTHON" "$ROOT/source_health.py" \
    --policy status \
    < "$SNAPSHOT_MANIFEST_PATH"; then
    ok=0
    source_health_issue=1
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

    remote_main=$(gh api \
        "repos/$REPOSITORY/git/ref/heads/main" \
        --jq .object.sha \
        2>/dev/null || true)
    if [[ "$remote_main" =~ ^[0-9a-f]{40}$ ]]; then
        echo "Remote main: ${remote_main:0:12}"
    else
        echo "Remote main: unavailable (could not prove exact revision)"
        ok=0
        deployment_issue=1
    fi

    latest_run=$(gh run list \
        --repo "$REPOSITORY" \
        --workflow update.yml \
        --branch main \
        --limit 1 \
        --json status,conclusion,databaseId,headSha \
        --jq '.[0] | "\(.status)|\(.conclusion // "")|\(.databaseId)|\(.headSha)"' \
        2>/dev/null || true)
    IFS='|' read -r run_status run_conclusion run_id run_head_sha <<< "$latest_run"
    if [ "$run_status" = "completed" ] && [ "$run_conclusion" = "success" ]; then
        if [[ ! "$run_head_sha" =~ ^[0-9a-f]{40}$ ]]; then
            echo "Latest deployment: successful but revision evidence is invalid (run $run_id)"
            ok=0
            deployment_issue=1
        elif [[ ! "$remote_main" =~ ^[0-9a-f]{40}$ ]]; then
            echo "Latest deployment: successful but current main is unavailable (run $run_id)"
            ok=0
            deployment_issue=1
        elif [ "$run_head_sha" != "$remote_main" ]; then
            echo "Latest deployment: successful but stale (run $run_id at ${run_head_sha:0:12}; main ${remote_main:0:12})"
            ok=0
            deployment_issue=1
        else
            echo "Latest deployment: successful for current main (run $run_id)"
        fi
    elif [ "$run_status" = "queued" ] || [ "$run_status" = "in_progress" ]; then
        echo "Latest deployment: pending ($run_status, run $run_id)"
        ok=0
        deployment_pending=1
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

    latest_watchdog=$(gh run list \
        --repo "$REPOSITORY" \
        --workflow watchdog.yml \
        --branch main \
        --limit 1 \
        --json status,conclusion,databaseId,headSha \
        --jq '.[0] | "\(.status)|\(.conclusion // "")|\(.databaseId)|\(.headSha)"' \
        2>/dev/null || true)
    IFS='|' read -r watchdog_status watchdog_conclusion watchdog_run_id \
        watchdog_head_sha <<< "$latest_watchdog"
    if [ "$watchdog_status" = "completed" ] \
        && [ "$watchdog_conclusion" = "success" ]; then
        if [[ ! "$watchdog_head_sha" =~ ^[0-9a-f]{40}$ ]]; then
            echo "Latest watchdog: successful but revision evidence is invalid (run $watchdog_run_id)"
            ok=0
            watchdog_issue=1
        elif [[ ! "$remote_main" =~ ^[0-9a-f]{40}$ ]]; then
            echo "Latest watchdog: successful but current main is unavailable (run $watchdog_run_id)"
            ok=0
            watchdog_issue=1
        elif [ "$watchdog_head_sha" != "$remote_main" ]; then
            echo "Latest watchdog: successful but stale (run $watchdog_run_id at ${watchdog_head_sha:0:12}; main ${remote_main:0:12})"
            ok=0
            watchdog_issue=1
        else
            echo "Latest watchdog: successful for current main (run $watchdog_run_id)"
        fi
    elif [ "$watchdog_status" = "queued" ] \
        || [ "$watchdog_status" = "in_progress" ]; then
        echo "Latest watchdog: pending ($watchdog_status, run $watchdog_run_id)"
        ok=0
        watchdog_pending=1
    elif [ -n "$watchdog_status" ]; then
        echo "Latest watchdog: ${watchdog_conclusion:-$watchdog_status} (run $watchdog_run_id)"
        ok=0
        watchdog_issue=1
        failed_watchdog_run_id=$watchdog_run_id
    else
        echo "Latest watchdog: unavailable (no workflow run found)"
        ok=0
        watchdog_issue=1
    fi
else
    echo "Deployment and watchdog status: unavailable (GitHub CLI not installed)"
    ok=0
    deployment_issue=1
    watchdog_issue=1
fi

if [ "$ok" -eq 1 ]; then
    exit 0
fi

if [ "$updater_pending" -eq 1 ]; then
    echo "Wait for the active refresh to finish, then rerun: $ROOT/automation_status.sh"
fi
if [ "$updater_issue" -eq 1 ]; then
    if [ "$updater_loaded" -eq 0 ]; then
        echo "Repair updater with: $ROOT/install_automation.sh"
    elif [ "$updater_exit_issue" -eq 1 ]; then
        echo "Inspect updater errors: $HOME/Library/Logs/SubstackTrades/refresh-error.log"
        echo "The bounded automatic retry cycle was exhausted or could not start."
        echo "Start a new three-attempt cycle with: launchctl kickstart -k $DOMAIN/$LABEL"
    fi
fi
if [ "$refresh_issue" -eq 1 ] && [ "$updater_pending" -eq 0 ]; then
    echo "Run a fresh ingestion with: $ROOT/refresh.sh"
fi
if [ "$source_health_issue" -eq 1 ]; then
    echo "Inspect source modes in: $SNAPSHOT_MANIFEST_PATH"
    echo "Inspect publisher detail in: $HOME/Library/Logs/SubstackTrades/refresh-error.log"
fi
if [ "$deployment_pending" -eq 1 ]; then
    echo "Wait for the pending deployment to finish, then rerun: $ROOT/automation_status.sh"
fi
if [ "$deployment_issue" -eq 1 ]; then
    if [ -n "$failed_run_id" ]; then
        echo "Inspect deployment with: gh run view --repo $REPOSITORY $failed_run_id --log-failed"
    else
        echo "Inspect deployment with: gh run list --repo $REPOSITORY --workflow update.yml --limit 5"
    fi
fi
if [ "$watchdog_pending" -eq 1 ]; then
    echo "Wait for the pending watchdog to finish, then rerun: $ROOT/automation_status.sh"
fi
if [ "$watchdog_issue" -eq 1 ]; then
    if [ -n "$failed_watchdog_run_id" ]; then
        echo "Inspect watchdog with: gh run view --repo $REPOSITORY $failed_watchdog_run_id --log-failed"
    else
        echo "Inspect watchdog with: gh run list --repo $REPOSITORY --workflow watchdog.yml --limit 5"
    fi
fi
exit 1
