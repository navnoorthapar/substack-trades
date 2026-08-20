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
ACTIVE_REFRESH_PID=""

forward_signal_to_refresh() {
    signal_name=$1
    exit_code=$2
    # launchd owns this supervisor rather than refresh.sh. Keep the supervisor
    # alive until the active child has received TERM and completed its own
    # byte-exact rollback; never turn an operator stop into another retry.
    trap '' INT TERM
    if [ -n "$ACTIVE_REFRESH_PID" ]; then
        # The refresh is spawned as the leader of its own process group. Signal
        # that whole group so a foreground Python/build child exits promptly;
        # refresh.sh can then run its TERM rollback instead of deferring the
        # trap until a multi-minute child happens to finish.
        kill -TERM -- "-$ACTIVE_REFRESH_PID" 2>/dev/null || true
        if wait "$ACTIVE_REFRESH_PID" 2>/dev/null; then
            child_exit=0
        else
            child_exit=$?
        fi
        echo "Scheduled refresh received $signal_name; forwarded SIGTERM to the active refresh and waited for child exit $child_exit." >&2
    else
        echo "Scheduled refresh received $signal_name with no active refresh." >&2
    fi
    exit "$exit_code"
}

trap 'forward_signal_to_refresh SIGINT 130' INT
trap 'forward_signal_to_refresh SIGTERM 143' TERM

if [[ ! "$RETRY_DELAY_SECONDS" =~ ^[0-9]+$ ]] \
    || [ "$RETRY_DELAY_SECONDS" -gt 86400 ]; then
    echo "SCHEDULED_REFRESH_RETRY_DELAY_SECONDS must be an integer from 0 to 86400." >&2
    exit 64
fi
if [ ! -x "$ROOT/refresh.sh" ]; then
    echo "Scheduled refresh target is missing or not executable: $ROOT/refresh.sh" >&2
    exit 66
fi
# The override belongs to this supervisor only. Keep the validated local value
# for its own sleeps, but do not leak a custom delay into refresh.sh's nested
# release tests or any other child process.
unset SCHEDULED_REFRESH_RETRY_DELAY_SECONDS

attempt=1
while [ "$attempt" -le "$MAX_ATTEMPTS" ]; do
    echo "=== Scheduled refresh attempt $attempt/$MAX_ATTEMPTS ==="
    # Noninteractive Bash normally keeps background children in its own process
    # group. Briefly enable job control so this refresh becomes a process-group
    # leader that the supervisor can terminate atomically with all descendants.
    set -m
    REFRESH_BUSY_EXIT_CODE=75 "$ROOT/refresh.sh" &
    ACTIVE_REFRESH_PID=$!
    set +m
    if wait "$ACTIVE_REFRESH_PID"; then
        exit_code=0
    else
        exit_code=$?
    fi
    ACTIVE_REFRESH_PID=""

    if [ "$exit_code" -eq 0 ]; then
        if [ "$attempt" -gt 1 ]; then
            echo "Scheduled refresh recovered on attempt $attempt/$MAX_ATTEMPTS."
        fi
        exit 0
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
