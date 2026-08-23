#!/usr/bin/env bash
set -euo pipefail

LANE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
STATUS_FILE="${LANE_DIR}/status.json"
export PYTHONPATH="${LANE_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"

stored_status="{}"
if [ -f "$STATUS_FILE" ]; then
    stored_status="$(cat "$STATUS_FILE")"
fi
live_check=""
live_rc=0
live_check="$("${LANE_DIR}/bin/siteground-ops" novamira-update check 2>&1)" || live_rc=$?

/usr/bin/python3 - "$stored_status" "$live_check" "$live_rc" <<'PY'
import json
import sys

stored_raw, live_raw, live_rc = sys.argv[1:]
try:
    stored = json.loads(stored_raw)
except json.JSONDecodeError:
    stored = {"ok": False, "diagnostics": {"code": "status_not_recorded"}}
try:
    live = json.loads(live_raw)
except json.JSONDecodeError:
    live = {"ok": False, "mutation_state": "not_applicable", "diagnostics": {"code": "invalid_check_receipt"}}
print(json.dumps({"status": stored, "live_check": live, "live_check_exit": int(live_rc)}, indent=2, sort_keys=True))
PY
exit "$live_rc"
