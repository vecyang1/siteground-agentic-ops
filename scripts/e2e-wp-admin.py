#!/usr/bin/env python3
"""Live end-to-end check for `siteground-ops wp-admin`.

Why this exists as a separate, gated script rather than a unit test: the flow it
exercises mints a real single-use administrator credential on a live hosting
account. That cannot run in CI and must not run by accident, so it refuses to
start without an explicit opt-in and an explicit target.

What it guards, in the order the failures actually happened:

  1. The inventory read the login depends on still returns rows.
  2. A real login completes and lands in wp-admin.
  3. **The login finishes with margin under the configured browser budget.** This
     is the regression that shipped: OpenCLI's 60s default sat just above a
     30-40s login, so the command aborted mid-flight and reported a clean
     failure. A budget is only safe while the work stays well inside it, and
     nothing else in the suite can measure that -- it needs the real browser.
  4. Ambiguity is still refused rather than guessed. This doubles as the
     negative control: a run where nothing can fail is not evidence.
  5. The minted credential never appears in the output.

Usage:
    SITEGROUND_E2E=1 scripts/e2e-wp-admin.py --target <domain-or-profile> --app <id>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "bin" / "siteground-ops"
OPT_IN = "SITEGROUND_E2E"

# A credential-shaped string must never reach stdout, a log, or this report.
AUTOLOGIN_MARKER = re.compile(r"wp_auto_login_[a-f0-9]+", re.IGNORECASE)
# Below this share of the browser budget the margin is comfortable; above it the
# next slow site is the one that trips the cap, which is how this broke before.
BUDGET_WARN_RATIO = 0.5


def run(args: list[str], *, timeout: float) -> tuple[int, str, float]:
    started = time.monotonic()
    try:
        result = subprocess.run(
            [str(CLI), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, "", time.monotonic() - started
    return result.returncode, (result.stdout or "") + (result.stderr or ""), time.monotonic() - started


def parse(output: str) -> dict:
    try:
        return json.loads(output[output.index("{"): output.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, help="Exact site profile id or hostname to sign into.")
    parser.add_argument("--app", help="Exact WordPress application id; required for a multi-app site.")
    parser.add_argument(
        "--ambiguous-target",
        help="A site with more than one application, to prove a refusal still refuses.",
    )
    args = parser.parse_args(argv)

    if os.environ.get(OPT_IN) != "1":
        print(
            f"refusing to run: this mints a real administrator credential on a live account.\n"
            f"Set {OPT_IN}=1 to opt in.",
            file=sys.stderr,
        )
        return 2

    sys.path.insert(0, str(ROOT / "src"))
    from siteground_ops import portal  # noqa: PLC0415

    budget = portal.WORDPRESS_LOGIN_BROWSER_SECONDS
    ceiling = portal.OPENCLI_BROWSER_ACTION_CEILING_SECONDS
    # bool | None: None means "no verdict" -- the check could not be evaluated.
    # Reporting an unmeasured margin as FAIL next to a healthy-looking percentage
    # sends the reader after the wrong subsystem.
    checks: list[tuple[str, bool | None, str]] = []
    outputs: list[str] = []

    # 1. The inventory read the login depends on.
    code, out, seconds = run(["portal", "read", "primary-siteground", "wp-apps"], timeout=300)
    outputs.append(out)
    rows = parse(out).get("evidence", {}).get("rows") or []
    checks.append(("wp-apps returns inventory", code == 0 and len(rows) > 0, f"exit {code}, {len(rows)} rows, {seconds:.0f}s"))

    # 2 + 3. A real login, and the margin under the budget that keeps it safe.
    login_args = ["wp-admin", args.target] + (["--app", args.app] if args.app else [])
    code, out, seconds = run(login_args, timeout=budget + 120)
    outputs.append(out)
    evidence = parse(out).get("evidence", {})
    checks.append((
        "wp-admin signs in",
        code == 0 and evidence.get("logged_in") is True,
        f"exit {code}, logged_in={evidence.get('logged_in')}, {seconds:.0f}s",
    ))
    checks.append((
        f"login margin under the {budget}s browser budget",
        (seconds < budget * BUDGET_WARN_RATIO) if code == 0 else None,
        f"{seconds:.0f}s used of {budget}s "
        f"({seconds / budget:.0%}; a single browser action is capped at {ceiling}s regardless)"
        if code == 0
        else "not measured: the login did not complete",
    ))

    # 4. The negative control: a refusal that must still refuse.
    if args.ambiguous_target:
        code, out, _ = run(["wp-admin", args.ambiguous_target], timeout=300)
        outputs.append(out)
        diagnostics = parse(out).get("diagnostics", {})
        checks.append((
            "multi-app site is refused, not guessed",
            code == 2 and diagnostics.get("code") == "ambiguous_wordpress_application",
            f"exit {code}, code={diagnostics.get('code')}",
        ))

    # 5. The credential never reaches the operator's terminal or logs.
    leaked = sum(len(AUTOLOGIN_MARKER.findall(text)) for text in outputs)
    checks.append(("no autologin credential in any output", leaked == 0, f"{leaked} matches"))

    width = max(len(name) for name, _ok, _detail in checks)
    for name, ok, detail in checks:
        print(f"{'SKIP' if ok is None else 'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
    failed = [name for name, ok, _detail in checks if ok is False]
    skipped = [name for name, ok, _detail in checks if ok is None]
    graded = len(checks) - len(skipped)
    print(
        f"\ngraded {graded} of {len(checks)} checks against {args.target}: "
        f"{graded - len(failed)} passed, {len(failed)} failed, {len(skipped)} without a verdict"
    )
    sys.stdout.flush()
    if failed:
        print("failed: " + ", ".join(failed), file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
