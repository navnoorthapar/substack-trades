#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")"
ROOT=$PWD
LABEL=com.navnoor.substacktrades
DOMAIN="gui/$(id -u)"
SOURCE="$ROOT/launchd/$LABEL.plist"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/SubstackTrades"

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"
cp "$SOURCE" "$TARGET"

plutil -replace ProgramArguments.1 -string "$ROOT/refresh.sh" "$TARGET"
plutil -replace EnvironmentVariables.HOME -string "$HOME" "$TARGET"
plutil -replace StandardOutPath -string "$LOG_DIR/refresh.log" "$TARGET"
plutil -replace StandardErrorPath -string "$LOG_DIR/refresh-error.log" "$TARGET"
plutil -lint "$TARGET"
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

# Run once now. The short duplicate-run guard makes this a cheap no-op when a
# successful manual refresh just completed.
launchctl kickstart -k "$DOMAIN/$LABEL"

if ! launchctl print "$DOMAIN/$LABEL" >/dev/null; then
    echo "Updater installation could not be verified." >&2
    exit 1
fi

echo "Updater installed and loaded."
echo "Schedule: 09:00, 13:00, and 20:00 local time."
echo "Logs: $LOG_DIR"
