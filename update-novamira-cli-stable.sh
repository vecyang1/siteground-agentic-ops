#!/usr/bin/env bash
set -euo pipefail

LANE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
LANE_ID="siteground-novamira-cli-stable"
STATUS_FILE="${LANE_DIR}/status.json"
LOG_DIR="${LANE_DIR}/logs"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"
CLI=("${LANE_DIR}/bin/siteground-ops")
export PYTHONPATH="${LANE_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"

mkdir -p "$LOG_DIR"
exec >>"${LOG_DIR}/update.log" 2>&1
printf '\n[%s] %s start\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$LANE_ID"

write_status() {
    local payload="$1"
    local temporary="${STATUS_FILE}.tmp.$$"
    printf '%s\n' "$payload" >"$temporary"
    mv -f "$temporary" "$STATUS_FILE"
}

check_output=""
check_rc=0
check_output="$("${CLI[@]}" novamira-update check 2>&1)" || check_rc=$?
printf '%s\n' "$check_output"

status_payload="$($PYTHON_BIN - "$LANE_ID" "$check_rc" "$check_output" <<'PY'
import json
import sys
from datetime import datetime, timezone

lane, rc_raw, raw = sys.argv[1:]
try:
    receipt = json.loads(raw)
except json.JSONDecodeError:
    receipt = {"ok": False, "mutation_state": "not_applicable", "diagnostics": {"code": "invalid_check_receipt"}}
print(json.dumps({
    "lane": lane,
    "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "check_exit": int(rc_raw),
    "check": receipt,
}, sort_keys=True, indent=2))
PY
)"
write_status "$status_payload"

if [ "$check_rc" -ne 0 ]; then
    printf '[%s] check failed; no mutation attempted\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    exit 1
fi

eligible="$($PYTHON_BIN - "$check_output" <<'PY'
import json
import sys

try:
    receipt = json.loads(sys.argv[1])
    evidence = receipt.get("evidence", {})
    ready = evidence.get("auto_apply_ready") is True
    available = evidence.get("update_available") is True
    reviewed = evidence.get("latest") == "1.0.3"
    print("yes" if ready and available and reviewed else "no")
except (json.JSONDecodeError, AttributeError):
    print("no")
PY
)"

if [ "$eligible" != "yes" ]; then
    printf '[%s] no approved update eligible; no mutation attempted\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    exit 0
fi

apply_output=""
apply_rc=0
apply_output="$("${CLI[@]}" novamira-update apply --confirm-version 1.0.3 2>&1)" || apply_rc=$?
printf '%s\n' "$apply_output"
$PYTHON_BIN - "$STATUS_FILE" "$apply_rc" "$apply_output" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone

path, rc_raw, raw = sys.argv[1:]
try:
    payload = json.loads(raw)
except json.JSONDecodeError:
    payload = {"ok": False, "mutation_state": "unknown", "diagnostics": {"code": "invalid_apply_receipt"}}
with open(path, "r", encoding="utf-8") as handle:
    status = json.load(handle)
status["apply_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
status["apply_exit"] = int(rc_raw)
status["apply"] = payload
temporary = f"{path}.tmp.{os.getpid()}"
with open(temporary, "w", encoding="utf-8") as handle:
    json.dump(status, handle, indent=2, sort_keys=True)
    handle.write("\n")
os.replace(temporary, path)
PY

if [ "$apply_rc" -ne 0 ]; then
    printf '[%s] approved update did not complete successfully\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    exit "$apply_rc"
fi
printf '[%s] approved update completed\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
