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

status_payload="$($PYTHON_BIN - "$LANE_ID" "$check_rc" "$check_output" "$STATUS_FILE" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone

lane, rc_raw, raw, previous_path = sys.argv[1:]
rc = int(rc_raw)
try:
    receipt = json.loads(raw)
except json.JSONDecodeError:
    receipt = {"ok": False, "mutation_state": "not_applicable", "diagnostics": {"code": "invalid_check_receipt"}}

# A run that could not reach the registry produced no verdict. The streak is
# what separates a dropped DNS lookup from an outage that has been hiding a
# real update for a week; a bare "check failed" says neither.
previous_streak = 0
if os.path.exists(previous_path):
    try:
        with open(previous_path, "r", encoding="utf-8") as handle:
            previous_streak = int(json.load(handle).get("no_verdict_streak", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        previous_streak = 0

print(json.dumps({
    "lane": lane,
    "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "check_exit": rc,
    "check": receipt,
    "no_verdict_streak": previous_streak + 1 if rc == 75 else 0,
}, sort_keys=True, indent=2))
PY
)"
write_status "$status_payload"

# 75 is EX_TEMPFAIL: the registry was unreachable, so this run has no verdict.
# Failing the lane on it points the daily alarm at the network instead of the
# package, and an alarm that is usually wrong stops being read.
if [ "$check_rc" -eq 75 ]; then
    streak="$($PYTHON_BIN -c 'import json,sys; print(json.load(open(sys.argv[1])).get("no_verdict_streak", 0))' "$STATUS_FILE")"
    printf '[%s] no verdict: registry unreachable (streak %s); no mutation attempted\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$streak"
    exit 0
fi

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
    reviewed = evidence.get("latest") == "1.1.0"
    print("yes" if ready and available and reviewed else "no")
except (json.JSONDecodeError, AttributeError):
    print("no")
PY
)"

if [ "$eligible" != "yes" ]; then
    if printf '%s' "$check_output" | grep -q '"update_available": false'; then
        printf '[%s] already on the reviewed release; no mutation attempted\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    else
        printf '[%s] no approved update eligible; no mutation attempted\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    fi
    exit 0
fi

apply_output=""
apply_rc=0
apply_output="$("${CLI[@]}" novamira-update apply --confirm-version 1.1.0 2>&1)" || apply_rc=$?
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
