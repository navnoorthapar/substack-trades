#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")"
ROOT=$PWD
LABEL=com.navnoor.substacktrades
DOMAIN="gui/$(id -u)"
SOURCE="$ROOT/launchd/$LABEL.plist"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/SubstackTrades"

if [ ! -x "$ROOT/.githooks/pre-push" ]; then
    echo "The versioned pre-push release gate is missing or not executable." >&2
    exit 1
fi
if [ ! -x "$ROOT/scheduled_refresh.sh" ]; then
    echo "The bounded scheduled-refresh supervisor is missing or not executable." >&2
    exit 1
fi
EXISTING_HOOKS_PATH=$(git config --local --get core.hooksPath || true)
if [ -n "$EXISTING_HOOKS_PATH" ] && [ "$EXISTING_HOOKS_PATH" != ".githooks" ]; then
    echo "Refusing to replace existing Git hooks path: $EXISTING_HOOKS_PATH" >&2
    echo "Review that hook setup and chain .githooks/pre-push explicitly." >&2
    exit 1
fi
git config --local core.hooksPath .githooks

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"
cp "$SOURCE" "$TARGET"

# `plutil -replace` inserts instead of replacing an array element on some macOS
# versions. Remove the template value first so the program receives one script.
plutil -remove ProgramArguments.1 "$TARGET"
plutil -insert ProgramArguments.1 -string "$ROOT/scheduled_refresh.sh" "$TARGET"
plutil -replace EnvironmentVariables.HOME -string "$HOME" "$TARGET"
plutil -replace StandardOutPath -string "$LOG_DIR/refresh.log" "$TARGET"
plutil -replace StandardErrorPath -string "$LOG_DIR/refresh-error.log" "$TARGET"
plutil -lint "$TARGET"
installed_program=$(plutil -extract ProgramArguments.0 raw "$TARGET")
installed_supervisor=$(plutil -extract ProgramArguments.1 raw "$TARGET")
if [ "$installed_program" != "/bin/bash" ] \
    || [ "$installed_supervisor" != "$ROOT/scheduled_refresh.sh" ] \
    || plutil -extract ProgramArguments.2 raw "$TARGET" >/dev/null 2>&1; then
    echo "Installed updater ProgramArguments do not match the bounded supervisor contract." >&2
    exit 1
fi
chmod 644 "$TARGET"

# Replace any stale in-memory copy with the versioned configuration.
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
if ! launchctl bootstrap "$DOMAIN" "$TARGET"; then
    echo >&2
    echo "macOS blocked the updater. Enable the 'bash' item under" >&2
    echo "System Settings -> General -> Login Items & Extensions -> Allow in Background," >&2
    echo "then run this installer again." >&2
    open 'x-apple.systempreferences:com.apple.LoginItems-Settings.extension' 2>/dev/null || true
    exit 1
fi
launchctl enable "$DOMAIN/$LABEL"

# RunAtLoad starts one bounded refresh cycle as part of bootstrap. The
# refresh-level lock and short duplicate guard still make it a cheap no-op when
# a concurrent or recently successful manual refresh already owns the work.
if ! launchctl print "$DOMAIN/$LABEL" >/dev/null; then
    echo "Updater installation could not be verified." >&2
    exit 1
fi

echo "Updater installed and loaded."
echo "Schedule: 09:00, 13:00, and 22:00 local time."
echo "Recovery: up to 3 attempts per scheduled cycle, 15 minutes apart."
echo "Logs: $LOG_DIR"
