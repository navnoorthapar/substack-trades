#!/usr/bin/env bash
# Run one scheduled refresh with a small, bounded recovery window.
#
# launchd's KeepAlive/SuccessfulExit=false policy has no retry-count limit: a
# persistent fail-closed condition would relaunch forever. Keep retries in this
# foreground supervisor instead so launchd records a final nonzero exit after
# exactly three failed attempts and never creates a background retry storm.
set -Eeuo pipefail

cd "$(dirname "$0")"
ROOT=$PWD
MAX_ATTEMPTS=3
RETRY_DELAY_SECONDS=${SCHEDULED_REFRESH_RETRY_DELAY_SECONDS:-900}

if [[ ! "$RETRY_DELAY_SECONDS" =~ ^[0-9]+$ ]] \
    || [ "$RETRY_DELAY_SECONDS" -gt 86400 ]; then
    echo "SCHEDULED_REFRESH_RETRY_DELAY_SECONDS must be an integer from 0 to 86400." >&2
    exit 64
fi
if [ ! -x "$ROOT/refresh.sh" ]; then
    echo "Scheduled refresh target is missing or not executable: $ROOT/refresh.sh" >&2
    exit 66
fi

attempt=1
while [ "$attempt" -le "$MAX_ATTEMPTS" ]; do
    echo "=== Scheduled refresh attempt $attempt/$MAX_ATTEMPTS ==="
    if REFRESH_BUSY_EXIT_CODE=75 "$ROOT/refresh.sh"; then
        if [ "$attempt" -gt 1 ]; then
            echo "Scheduled refresh recovered on attempt $attempt/$MAX_ATTEMPTS."
        fi
        exit 0
    else
        exit_code=$?
    fi

    if [ "$attempt" -eq "$MAX_ATTEMPTS" ]; then
        echo "Scheduled refresh failed after $MAX_ATTEMPTS attempts; preserving final exit code $exit_code for launchd and automation status." >&2
        exit "$exit_code"
    fi

    echo "Scheduled refresh attempt $attempt/$MAX_ATTEMPTS failed with exit code $exit_code; retrying in $RETRY_DELAY_SECONDS seconds." >&2
    sleep "$RETRY_DELAY_SECONDS"
    attempt=$((attempt + 1))
done

# The loop bounds above make this unreachable; retain a fail-closed guard in
# case a future edit accidentally changes its control flow.
echo "Scheduled refresh retry supervisor reached an invalid state." >&2
exit 70
