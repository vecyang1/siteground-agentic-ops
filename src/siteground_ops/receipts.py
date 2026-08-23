from __future__ import annotations

import re
from typing import Any


REDACTIONS = (
    re.compile(r"(?i)(SSH_CONFIRMED_PASS\s*=\s*)([^\s]+)"),
    re.compile(r"(?i)(Authorization\s*:\s*(?:Basic|Bearer)\s+)([^\s]+)"),
    re.compile(r"(?i)(https?://)([^/@\s:]+):([^/@\s]+)@"),
    re.compile(
        r"(?i)((?:ssh[_-])?(?:password|passphrase|passwd|secret|token|api[_-]?key|private[_-]?key)\s*[:=]\s*[\"']?)([^\"'\s,;}]+)"
    ),
)

SENSITIVE_KEY = re.compile(
    r"(?i)^(?:ssh[_-])?(?:confirmed[_-]?pass|confirmed[_-]?(?:password|passphrase|passwd|secret|token|api[_-]?key|private[_-]?key)|password|passphrase|passwd|secret|token|api[_-]?key|private[_-]?key|authorization)$"
)


def redact(value: str) -> str:
    cleaned = value
    cleaned = REDACTIONS[0].sub(r"\1[REDACTED]", cleaned)
    cleaned = REDACTIONS[1].sub(r"\1[REDACTED]", cleaned)
    cleaned = REDACTIONS[2].sub(r"\1[REDACTED]@", cleaned)
    cleaned = REDACTIONS[3].sub(r"\1[REDACTED]", cleaned)
    return cleaned


def sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if SENSITIVE_KEY.fullmatch(str(key)) else sanitize(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    return value


def receipt(
    *,
    ok: bool,
    operation: str,
    target: str | None,
    mutation_state: str,
    request_id: str,
    evidence: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    safe_next_action: str | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return sanitize(
        {
            "ok": ok,
            "operation": operation,
            "target": target,
            "mutation_state": mutation_state,
            "request_id": request_id,
            "evidence": evidence or {},
            "warnings": warnings or [],
            "safe_next_action": safe_next_action,
            "diagnostics": diagnostics or {},
        }
    )
