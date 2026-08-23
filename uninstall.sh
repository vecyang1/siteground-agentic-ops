#!/usr/bin/env bash
set -euo pipefail

LABEL="com.vec.siteground-novamira-cli-updater"
PLIST_DEST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
if launchctl print "gui/$(id -u)/${LABEL}" >/dev/null 2>&1; then
    launchctl bootout "gui/$(id -u)" "$PLIST_DEST" 2>/dev/null || true
fi
rm -f "$PLIST_DEST"
printf 'Removed %s.\n' "$LABEL"
