#!/usr/bin/env bash
set -euo pipefail

LANE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
LABEL="com.vec.siteground-novamira-cli-updater"
PLIST_SOURCE="${LANE_DIR}/${LABEL}.plist"
PLIST_DEST="${HOME}/Library/LaunchAgents/${LABEL}.plist"

mkdir -p "${HOME}/Library/LaunchAgents" "${LANE_DIR}/logs"
chmod 700 "${LANE_DIR}/update-novamira-cli-stable.sh" "${LANE_DIR}/check-status.sh" "${LANE_DIR}/uninstall.sh"
plutil -lint "$PLIST_SOURCE" >/dev/null

if launchctl print "gui/$(id -u)/${LABEL}" >/dev/null 2>&1; then
    launchctl bootout "gui/$(id -u)" "$PLIST_DEST" 2>/dev/null || true
fi
/usr/bin/python3 - "$PLIST_SOURCE" "$PLIST_DEST" "$LANE_DIR" <<'PY'
import plistlib
import sys
from pathlib import Path

source, destination, lane_dir = map(Path, sys.argv[1:])
payload = plistlib.loads(source.read_bytes())
arguments = payload["ProgramArguments"]
payload["ProgramArguments"] = [arguments[0], str(lane_dir / "update-novamira-cli-stable.sh")]
payload["StandardOutPath"] = str(lane_dir / "logs/launchd.out.log")
payload["StandardErrorPath"] = str(lane_dir / "logs/launchd.err.log")
destination.write_bytes(plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False))
PY
plutil -lint "$PLIST_DEST" >/dev/null
launchctl bootstrap "gui/$(id -u)" "$PLIST_DEST"
launchctl enable "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl kickstart "gui/$(id -u)/${LABEL}"
printf 'Installed %s; daily check at 05:20 local time.\n' "$LABEL"
