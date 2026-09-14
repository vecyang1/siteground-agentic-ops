from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence
from urllib.parse import urlsplit

from .config import ConfigError, OpsConfig, PortalAccountConfig, load_config

# EX_TEMPFAIL. Distinct from 1 so an automated lane can tell "we could not ask"
# from "we asked and did not like the answer".
EXIT_NO_VERDICT = 75
from .novamira_backend import LocalNovamiraBackend, NovamiraPaths, RegistryUnreachable
from .novamira_update import NovamiraUpdater, SUPPORTED_CLI_VERSION
from .portal import (
    PORTAL_READS,
    PortalError,
    PortalUnknownOutcomeError,
    build_portal_adapter,
    site_tools_links,
)
from .quota import (
    DEFAULT_HOURLY_EXECUTION_LIMIT,
    DEFAULT_INODES_LIMIT,
    DEFAULT_WEB_SPACE_GB_LIMIT,
    ALLOWED_CLEANUP_TARGETS,
    PlanQuotaSnapshot,
    QuotaMetric,
    QuotaSeverity,
    QuotaStore,
    QuotaTriageEngine,
    SiteDeepDiagnosis,
    SiteQuotaShare,
    clean_sibling_staging,
    clean_site_inodes,
    domains_match,
    probe_site_deep,
)
from .quota_intake import (
    QuotaAlertRecord,
    match_alert_to_config,
    parse_siteground_alert_email,
    scan_json_messages,
    scan_mail_threads,
)
from .quota_ledger import (
    get_ledger_db_path,
    get_quota_alert_by_id,
    get_quota_alerts,
    get_remediation_records,
    get_triage_snapshots,
    record_quota_alert,
    record_remediation,
    record_triage_snapshot,
)
from .receipts import receipt
from .runner import (
    RunnerError,
    build_novamira_runner,
    build_read_runner,
    build_runner,
    probe_public_cache_headers,
    read_transport_status,
    ssh_local_readiness_issues,
)


DEFAULT_CONFIG = Path.home() / ".config" / "siteground-ops" / "sites.json"
HOSTNAME = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-)){1,10}")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="siteground-ops")
    root.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    commands = root.add_subparsers(dest="operation", required=True)
    commands.add_parser("sites")
    doctor = commands.add_parser("doctor")
    doctor.add_argument("target")
    doctor.add_argument("--transport", choices=("auto", "ssh", "novamira"), default="auto")
    inventory = commands.add_parser("inventory")
    inventory.add_argument("target")
    inventory.add_argument("--transport", choices=("auto", "ssh", "novamira"), default="auto")
    purge = commands.add_parser("cache-purge")
    purge.add_argument("target")
    purge.add_argument("--confirm-target")
    purge.add_argument("--recovery-receipt")
    purge.add_argument("--transport", choices=("auto", "ssh", "novamira"), default="auto")
    cache_status = commands.add_parser("cache-status")
    cache_status.add_argument("target")
    cache_status.add_argument("--transport", choices=("auto", "ssh", "novamira"), default="auto")
    onboard = commands.add_parser("onboard")
    onboard.add_argument("target")
    onboard.add_argument("--transport", choices=("auto", "ssh", "novamira"), default="auto")
    novamira = commands.add_parser("novamira-update")
    novamira_commands = novamira.add_subparsers(dest="update_action", required=True)
    novamira_commands.add_parser("check")
    baseline = novamira_commands.add_parser("baseline")
    baseline.add_argument("--confirm-version", required=True)
    apply = novamira_commands.add_parser("apply")
    apply.add_argument("--confirm-version", required=True)
    portal = commands.add_parser("portal")
    portal_commands = portal.add_subparsers(dest="portal_action", required=True)
    portal_commands.add_parser("accounts")
    portal_doctor = portal_commands.add_parser("doctor")
    portal_doctor.add_argument("account")
    portal_read = portal_commands.add_parser("read")
    portal_read.add_argument("account")
    portal_read.add_argument("section", choices=tuple(PORTAL_READS))
    portal_read.add_argument("--plan-id")
    portal_links = portal_commands.add_parser("links")
    portal_links.add_argument("target")
    wp_admin = commands.add_parser("wp-admin")
    wp_admin.add_argument("target")
    wp_admin.add_argument(
        "--app",
        help="Exact WordPress application id; required when the site has more than one.",
    )
    wp_admin.add_argument(
        "--account",
        help="Exact portal account id; required when more than one is configured.",
    )
    wp_admin.add_argument(
        "--foreground",
        action="store_true",
        help="Raise the browser window. Off by default so the tab opens quietly.",
    )
    quota = commands.add_parser("quota")
    quota_commands = quota.add_subparsers(dest="quota_action", required=True)

    quota_check = quota_commands.add_parser("check")
    quota_check.add_argument("--plan", dest="plan_id")
    quota_check.add_argument("--telemetry", type=Path)

    quota_diagnose = quota_commands.add_parser("diagnose")
    quota_diagnose.add_argument("target")
    quota_diagnose.add_argument("--transport", choices=("auto", "ssh", "novamira"), default="auto")

    quota_triage = quota_commands.add_parser("triage")
    quota_triage.add_argument("target")
    quota_triage.add_argument("--plan", dest="plan_id", help="Exact hosting plan id to triage against")
    quota_triage.add_argument("--telemetry", type=Path)

    quota_clean = quota_commands.add_parser("clean")
    quota_clean.add_argument("target", help="Site identifier to clean inodes on")
    target_clean_group = quota_clean.add_mutually_exclusive_group(required=True)
    target_clean_group.add_argument(
        "--target-dir",
        choices=sorted(ALLOWED_CLEANUP_TARGETS),
        help="Subdirectory inside wp-content to purge",
    )
    target_clean_group.add_argument(
        "--target-staging",
        help="Sibling staging directory name under ~/www/ to delete (e.g. staging2.example.com)",
    )
    quota_clean.add_argument("--dry-run", action="store_true", default=False, help="Inspect without deleting files")
    quota_clean.add_argument("--confirm-target", help="Must match target site id for non-dry-run mutation")
    quota_clean.add_argument("--recovery-receipt", help="Operator rationale/audit receipt required for non-dry-run mutation")

    quota_record = quota_commands.add_parser("record")
    quota_record.add_argument("--plan", dest="plan_id", required=True)
    quota_record.add_argument("--name", dest="plan_name", default="")
    quota_record.add_argument("--inodes-used", type=int)
    quota_record.add_argument("--inodes-limit", type=int, default=DEFAULT_INODES_LIMIT)
    quota_record.add_argument("--web-space-used-gb", type=float)
    quota_record.add_argument("--web-space-limit-gb", type=float, default=DEFAULT_WEB_SPACE_GB_LIMIT)
    quota_record.add_argument("--executions-peak", type=int)
    quota_record.add_argument("--executions-limit", type=int, default=DEFAULT_HOURLY_EXECUTION_LIMIT)
    quota_record.add_argument("--cpu-alert", action="store_true")
    quota_record.add_argument("--data", help="Raw JSON snapshot string")
    quota_record.add_argument("--file", type=Path, help="Path to JSON snapshot file")

    quota_intake = quota_commands.add_parser("intake")
    quota_intake.add_argument("--since-days", type=int, default=30, help="Scan emails received within last N days")
    quota_intake.add_argument("--limit", type=int, default=50, help="Maximum emails to inspect")
    quota_intake.add_argument("--mail-db", type=Path, help="Explicit path to Mail.app Envelope Index database")
    quota_intake.add_argument("--file", type=Path, help="JSON file containing raw email dictionaries to parse")
    quota_intake.add_argument("--auto-triage", action="store_true", default=False, help="Automatically run triage for detected plan or sites")
    quota_intake.add_argument("--no-record", action="store_false", dest="record", default=True, help="Do not write ingested alerts to ledger")

    quota_ledger = quota_commands.add_parser("ledger")
    quota_ledger.add_argument("ledger_action", nargs="?", default="list", choices=("list", "show", "history"))
    quota_ledger.add_argument("--id", type=int, dest="alert_id", help="Alert ID to inspect")
    quota_ledger.add_argument("--plan", dest="plan_id", help="Filter by plan ID")
    quota_ledger.add_argument("--status", help="Filter by alert status")
    quota_ledger.add_argument("--site", help="Filter remediation history by site ID")
    quota_ledger.add_argument("--limit", type=int, default=20, help="Limit number of records returned")
    return root


def emit(payload: dict) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _site(config: OpsConfig, target: str, operation: str, request_id: str):
    site = config.sites.get(target)
    if site is None:
        emit(
            receipt(
                ok=False,
                operation=operation,
                target=target,
                mutation_state="refused",
                request_id=request_id,
                safe_next_action="Choose an exact site from `siteground-ops sites`.",
                diagnostics={"code": "unknown_target"},
            )
        )
    return site


def build_novamira_updater() -> NovamiraUpdater:
    paths = NovamiraPaths.from_home()
    return NovamiraUpdater(backend=LocalNovamiraBackend(paths=paths), home=paths.home)


def handle_novamira_update(args: argparse.Namespace, request_id: str) -> int:
    if args.update_action in {"apply", "baseline"} and args.confirm_version != SUPPORTED_CLI_VERSION:
        emit(
            receipt(
                ok=False,
                operation="novamira-update",
                target=None,
                mutation_state="refused",
                request_id=request_id,
                safe_next_action=(
                    "Run `siteground-ops novamira-update check`, review the candidate, "
                    f"then confirm exactly {SUPPORTED_CLI_VERSION}."
                ),
                diagnostics={
                    "code": "version_confirmation_required",
                    "expected": SUPPORTED_CLI_VERSION,
                },
            )
        )
        return 2
    try:
        updater = build_novamira_updater()
        if args.update_action == "check":
            result = updater.check()
        elif args.update_action == "baseline":
            result = updater.initialize_baseline(confirmed=True)
        else:
            result = updater.apply(confirmed=True)
    except RegistryUnreachable as exc:
        # Not reaching the registry is the absence of a verdict. Reporting it as
        # a failed check makes the daily lane cry wolf on a dropped DNS lookup.
        emit(
            receipt(
                ok=False,
                operation="novamira-update",
                target=None,
                mutation_state="not_applicable",
                request_id=request_id,
                safe_next_action=(
                    "No verdict: the npm registry was unreachable. Confirm network and DNS, "
                    "then re-run; nothing was changed."
                ),
                diagnostics={"code": "novamira_registry_unreachable", "message": str(exc)},
            )
        )
        return EXIT_NO_VERDICT
    except Exception as exc:
        read_only = args.update_action == "check"
        emit(
            receipt(
                ok=False,
                operation="novamira-update",
                target=None,
                mutation_state="not_applicable" if read_only else "refused",
                request_id=request_id,
                safe_next_action=(
                    "Retry the read-only check after confirming registry and local CLI readiness."
                    if read_only
                    else "Inspect the redacted diagnostics and resolve the preflight blocker before retrying."
                ),
                diagnostics={"code": "novamira_check_failed" if read_only else "novamira_preflight_failed", "message": str(exc)},
            )
        )
        return 1 if read_only else 2
    emit(
        receipt(
            ok=bool(result.get("ok")),
            operation="novamira-update",
            target=None,
            mutation_state=str(result.get("mutation_state", "unknown")),
            request_id=request_id,
            evidence=result,
        )
    )
    if result.get("ok") is True:
        return 0
    return 2 if result.get("mutation_state") == "refused" else 3


# Each remedy answers only the condition above it. A read that failed because
# Chrome is closed is not fixed by signing in again, and saying so sends the
# operator to the wrong place while the real fix sits one line away.
PORTAL_FAILURE_REMEDIES = {
    "portal_browser_not_connected": (
        "Open Chrome on profile {profile} with the OpenCLI extension enabled, confirm "
        "`opencli profile list` shows it connected, then re-run. SSH and Novamira reads "
        "are unaffected."
    ),
    "portal_read_timeout": (
        "The portal read exceeded its budget. Confirm the browser bridge is idle "
        "(`ps aux | grep -c '[o]pencli --profile'` returns 0) and re-run; portal reads "
        "are idempotent."
    ),
    "portal_adapter_unavailable": (
        "The OpenCLI adapter at {opencli_path} could not be started. Confirm the binary "
        "is installed and executable, then re-run."
    ),
    "portal_account_identity_mismatch": (
        "The signed-in SiteGround account does not serve this profile's expected_domains. "
        "Confirm Chrome profile {profile} is signed into the intended account."
    ),
    "portal_read_failed": (
        "Run `siteground-ops portal doctor {account}` to see which leg refused; "
        "SSH and Novamira reads remain independent."
    ),
}


def _portal_failure(exc: Exception, account: PortalAccountConfig) -> tuple[str, str]:
    """Name the condition the adapter reported, not a plausible one."""
    code = getattr(exc, "code", None) or "portal_read_failed"
    template = PORTAL_FAILURE_REMEDIES.get(code, PORTAL_FAILURE_REMEDIES["portal_read_failed"])
    return code, template.format(
        account=account.account_id,
        profile=account.opencli_profile,
        opencli_path=account.opencli_path,
    )


def handle_portal(args: argparse.Namespace, config: OpsConfig, request_id: str) -> int:
    if args.portal_action == "accounts":
        emit(
            receipt(
                ok=True,
                operation="portal-accounts",
                target=None,
                mutation_state="not_applicable",
                request_id=request_id,
                evidence={
                    "accounts": [
                        account.public_summary()
                        for account in sorted(config.portal_accounts.values(), key=lambda item: item.account_id)
                    ]
                },
            )
        )
        return 0

    if args.portal_action == "links":
        site = _site(config, args.target, "portal-links", request_id)
        if site is None:
            return 2
        try:
            links = site_tools_links(site)
        except PortalError:
            emit(
                receipt(
                    ok=False,
                    operation="portal-links",
                    target=site.site_id,
                    mutation_state="refused",
                    request_id=request_id,
                    safe_next_action="Add the exact non-secret SiteGround portal site id to this site profile.",
                    diagnostics={"code": "portal_site_mapping_required"},
                )
            )
            return 2
        emit(
            receipt(
                ok=True,
                operation="portal-links",
                target=site.site_id,
                mutation_state="not_applicable",
                request_id=request_id,
                evidence={"links": links, "portal_account": site.portal_account},
            )
        )
        return 0

    account = config.portal_accounts.get(args.account)
    operation = "portal-doctor" if args.portal_action == "doctor" else "portal-read"
    if account is None:
        emit(
            receipt(
                ok=False,
                operation=operation,
                target=args.account,
                mutation_state="refused",
                request_id=request_id,
                safe_next_action="Choose an exact account from `siteground-ops portal accounts`.",
                diagnostics={"code": "unknown_portal_account"},
            )
        )
        return 2
    section = "websites" if args.portal_action == "doctor" else args.section
    provider_plan_id = None if args.portal_action == "doctor" else args.plan_id
    try:
        evidence = build_portal_adapter(account).read(section, provider_plan_id=provider_plan_id)
    except Exception as exc:
        code, remedy = _portal_failure(exc, account)
        emit(
            receipt(
                ok=False,
                operation=operation,
                target=account.account_id,
                mutation_state="not_applicable",
                request_id=request_id,
                safe_next_action=remedy,
                diagnostics={"code": code},
            )
        )
        return 1
    emit(
        receipt(
            ok=True,
            operation=operation,
            target=account.account_id,
            mutation_state="not_applicable",
            request_id=request_id,
            evidence=evidence,
        )
    )
    return 0


def _resolve_portal_account(config: OpsConfig, site, requested: str | None):
    """Return (account, diagnostics). Exactly one of the two is None."""
    account_id = requested or (None if site is None else site.portal_account)
    if account_id is not None:
        account = config.portal_accounts.get(account_id)
        if account is None:
            return None, {"code": "unknown_portal_account", "requested": account_id}
        return account, None
    if len(config.portal_accounts) == 1:
        return next(iter(config.portal_accounts.values())), None
    return None, {
        "code": "ambiguous_portal_account",
        "configured": sorted(config.portal_accounts),
    }


def _select_wordpress_application(
    rows,
    expected_domain: str | None,
    requested_app_id: str | None,
    pinned_site_id: str | None = None,
):
    """Pick exactly one WordPress application, or explain why it cannot be done.

    The application id is provider-assigned and is not always 1 — one live site
    here is numbered 3 — so it is always read from the inventory. A site with a
    staging copy has more than one, and picking the wrong copy looks identical
    to picking the right one, so ambiguity is refused rather than guessed.

    A profile whose `public_url` is a customer-facing custom domain will not
    match the portal's own site domain; that case is served by pinning
    `portal_site_id`, which takes priority over any domain match.
    """
    if pinned_site_id:
        candidates = [row for row in rows if str(row.get("site_id", "")) == pinned_site_id]
        if not candidates:
            return None, {
                "code": "wordpress_application_not_found",
                "pinned_site_id": pinned_site_id,
                "observed_site_ids": sorted({str(row.get("site_id", "")) for row in rows}),
            }
        site_id = pinned_site_id
    else:
        matches = [
            row
            for row in rows
            if expected_domain
            in (
                str(row.get("domain", "")).strip().lower(),
                str(row.get("admin_host", "")).strip().lower(),
            )
        ]
        if not matches:
            return None, {
                "code": "wordpress_application_not_found",
                "expected_domain": expected_domain,
                "observed_domains": sorted({str(row.get("domain", "")) for row in rows}),
            }
        site_id = str(matches[0].get("site_id", ""))
        candidates = [row for row in rows if str(row.get("site_id", "")) == site_id]
    available = sorted({str(row.get("app_id", "")) for row in candidates})
    if requested_app_id is not None:
        match = next((row for row in candidates if str(row.get("app_id", "")) == requested_app_id), None)
        if match is None:
            return None, {
                "code": "unknown_wordpress_application",
                "available_app_ids": available,
                "requested_app_id": requested_app_id,
                "site_id": site_id,
            }
        return match, None
    if len(candidates) != 1:
        return None, {
            "code": "ambiguous_wordpress_application",
            "available_app_ids": available,
            "site_id": site_id,
        }
    return candidates[0], None


def handle_wp_admin(args: argparse.Namespace, config: OpsConfig, request_id: str) -> int:
    site = config.sites.get(args.target)
    bare_domain = None
    if site is None:
        # A domain is also an exact target: the portal inventory is authoritative
        # and a site does not need a local profile just to be opened.
        candidate = args.target.strip().lower()
        if HOSTNAME.fullmatch(candidate):
            bare_domain = candidate
        else:
            emit(
                receipt(
                    ok=False,
                    operation="wp-admin",
                    target=args.target,
                    mutation_state="refused",
                    request_id=request_id,
                    safe_next_action=(
                        "Choose an exact site from `siteground-ops sites`, or pass the exact domain."
                    ),
                    diagnostics={"code": "unknown_target"},
                )
            )
            return 2

    target_label = args.target if site is None else site.site_id
    account, diagnostics = _resolve_portal_account(config, site, args.account)
    if account is None:
        emit(
            receipt(
                ok=False,
                operation="wp-admin",
                target=target_label,
                mutation_state="refused",
                request_id=request_id,
                safe_next_action=(
                    "Name the exact portal account with --account, or pin `portal_account` in the site profile."
                ),
                diagnostics=diagnostics,
            )
        )
        return 2

    expected_domain = bare_domain or urlsplit(site.public_url).hostname
    if not expected_domain:
        emit(
            receipt(
                ok=False,
                operation="wp-admin",
                target=args.target,
                mutation_state="refused",
                request_id=request_id,
                safe_next_action="Repair the site profile so `public_url` is an exact HTTPS origin.",
                diagnostics={"code": "site_public_url_invalid"},
            )
        )
        return 2

    adapter = build_portal_adapter(account)
    try:
        rows = adapter.wordpress_apps()["rows"]
    except (PortalError, KeyError, TypeError) as exc:
        emit(
            receipt(
                ok=False,
                operation="wp-admin",
                target=target_label,
                mutation_state="not_applicable",
                request_id=request_id,
                safe_next_action=(
                    "Confirm OpenCLI doctor is green and the configured Chrome profile is signed into SiteGround."
                ),
                diagnostics={"code": "portal_read_failed", "message": str(exc)},
            )
        )
        return 1

    profile_site_id = None if site is None else site.portal_site_id
    selected, diagnostics = _select_wordpress_application(
        rows, expected_domain.lower(), args.app, pinned_site_id=profile_site_id
    )
    if selected is None:
        emit(
            receipt(
                ok=False,
                operation="wp-admin",
                target=target_label,
                mutation_state="refused",
                request_id=request_id,
                safe_next_action=(
                    "Run `siteground-ops portal read <account> wp-apps` and retry with the exact --app id."
                ),
                diagnostics=diagnostics,
            )
        )
        return 2

    resolved_site_id = str(selected.get("site_id", ""))

    # The provider labels every application with the *site* domain, so a staging
    # copy reads as `example.com` while its admin lives on `staging2.example.com`.
    # `admin_host` is the host a login actually lands on; compare against that.
    admin_host = str(selected.get("admin_host", "")).strip().lower()
    if not admin_host:
        emit(
            receipt(
                ok=False,
                operation="wp-admin",
                target=target_label,
                mutation_state="refused",
                request_id=request_id,
                safe_next_action="Re-read `wp-apps`; the portal did not report an admin host for this application.",
                diagnostics={"code": "wordpress_admin_host_unknown", "site_id": resolved_site_id},
            )
        )
        return 2
    warnings: list[str] = []
    if admin_host != expected_domain.lower():
        warnings.append(
            f"Application {selected.get('app_id')} opens {admin_host}, not the profile domain {expected_domain}."
        )

    try:
        evidence = adapter.open_wordpress_admin(
            site_id=resolved_site_id,
            app_id=args.app,
            expected_domain=admin_host,
            foreground=args.foreground,
        )
    except PortalUnknownOutcomeError as exc:
        emit(
            receipt(
                ok=False,
                operation="wp-admin",
                target=target_label,
                mutation_state="unknown",
                request_id=request_id,
                safe_next_action=(
                    "Do not repeat the command. Check the browser for an open wp-admin tab first; "
                    "an unused single-use login expires on its own."
                ),
                diagnostics={"code": "wordpress_login_outcome_unknown", "message": str(exc)},
                warnings=warnings,
            )
        )
        return 3
    except PortalError as exc:
        emit(
            receipt(
                ok=False,
                operation="wp-admin",
                target=target_label,
                mutation_state="not_applicable",
                request_id=request_id,
                safe_next_action=(
                    "Confirm OpenCLI doctor is green and the configured Chrome profile is signed into SiteGround."
                ),
                diagnostics={"code": "wordpress_login_failed", "message": str(exc)},
                warnings=warnings,
            )
        )
        return 1

    evidence["resolved_from"] = "site_profile" if profile_site_id else "portal_inventory"
    evidence["requested_target"] = args.target
    emit(
        receipt(
            ok=True,
            operation="wp-admin",
            target=target_label,
            mutation_state="not_applicable",
            request_id=request_id,
            evidence=evidence,
            warnings=warnings,
        )
    )
    return 0


def handle_quota(args: argparse.Namespace, config: OpsConfig, request_id: str) -> int:
    store = QuotaStore()
    action = args.quota_action

    if action == "record":
        plan_id = args.plan_id
        if args.file:
            try:
                raw_data = json.loads(args.file.read_text(encoding="utf-8"))
                snapshot = PlanQuotaSnapshot.from_dict(raw_data)
            except Exception as exc:
                emit(
                    receipt(
                        ok=False,
                        operation="quota-record",
                        target=plan_id,
                        mutation_state="refused",
                        request_id=request_id,
                        safe_next_action="Provide a valid JSON snapshot file.",
                        diagnostics={"code": "invalid_snapshot_file", "message": str(exc)},
                    )
                )
                return 2
        elif args.data:
            try:
                raw_data = json.loads(args.data)
                snapshot = PlanQuotaSnapshot.from_dict(raw_data)
            except Exception as exc:
                emit(
                    receipt(
                        ok=False,
                        operation="quota-record",
                        target=plan_id,
                        mutation_state="refused",
                        request_id=request_id,
                        safe_next_action="Provide valid JSON in --data.",
                        diagnostics={"code": "invalid_snapshot_json", "message": str(exc)},
                    )
                )
                return 2
        else:
            observed_at = (
                datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            )
            snapshot = PlanQuotaSnapshot(
                plan_id=plan_id,
                plan_name=args.plan_name or f"Plan {plan_id}",
                observed_at=observed_at,
                web_space_limit_gb=float(args.web_space_limit_gb or DEFAULT_WEB_SPACE_GB_LIMIT),
                web_space_used_gb=float(args.web_space_used_gb or 0.0),
                inodes_limit=int(args.inodes_limit or DEFAULT_INODES_LIMIT),
                inodes_used=int(args.inodes_used or 0),
                hourly_execution_limit=int(args.executions_limit or DEFAULT_HOURLY_EXECUTION_LIMIT),
                hourly_execution_peak=int(args.executions_peak or 0),
                cpu_seconds_alert=bool(args.cpu_alert),
                source="cli_record",
            )

        saved_path = store.save_snapshot(snapshot)
        emit(
            receipt(
                ok=True,
                operation="quota-record",
                target=plan_id,
                mutation_state="applied",
                request_id=request_id,
                evidence={
                    "plan_id": plan_id,
                    "saved_path": str(saved_path),
                    "snapshot": snapshot.to_dict(),
                },
            )
        )
        return 0

    if action == "check":
        snapshot = None
        plan_id = getattr(args, "plan_id", None)
        if args.telemetry:
            try:
                raw = json.loads(args.telemetry.read_text(encoding="utf-8"))
                snapshot = PlanQuotaSnapshot.from_dict(raw)
            except Exception as exc:
                emit(
                    receipt(
                        ok=False,
                        operation="quota-check",
                        target=plan_id,
                        mutation_state="not_applicable",
                        request_id=request_id,
                        safe_next_action="Provide a valid telemetry file.",
                        diagnostics={"code": "telemetry_load_failed", "message": str(exc)},
                    )
                )
                return 2
        elif plan_id:
            snapshot = store.load_snapshot(plan_id)

        if snapshot is None and not plan_id:
            for s in config.sites.values():
                if s.portal_plan_id:
                    plan_id = s.portal_plan_id
                    snapshot = store.load_snapshot(plan_id)
                    if snapshot:
                        break

        if snapshot is None:
            if plan_id:
                snapshot = PlanQuotaSnapshot(
                    plan_id=plan_id,
                    plan_name=f"Plan {plan_id}",
                    observed_at=(
                        datetime.now(timezone.utc)
                        .replace(microsecond=0)
                        .isoformat()
                        .replace("+00:00", "Z")
                    ),
                )
            else:
                emit(
                    receipt(
                        ok=False,
                        operation="quota-check",
                        target=None,
                        mutation_state="not_applicable",
                        request_id=request_id,
                        safe_next_action="Specify an exact --plan or record a telemetry snapshot first.",
                        diagnostics={"code": "plan_unresolved"},
                    )
                )
                return 2

        emit(
            receipt(
                ok=True,
                operation="quota-check",
                target=snapshot.plan_id,
                mutation_state="not_applicable",
                request_id=request_id,
                evidence={
                    "plan_id": snapshot.plan_id,
                    "plan_name": snapshot.plan_name,
                    "observed_at": snapshot.observed_at,
                    "severity": snapshot.overall_severity,
                    "metrics": {k: m.to_dict() for k, m in snapshot.metrics.items()},
                    "top_sites_by_inodes": [s.to_dict() for s in snapshot.top_sites_by_inodes()],
                },
            )
        )
        return 0

    if action == "diagnose":
        target = args.target
        site = _site(config, target, "quota-diagnose", request_id)
        if site is None:
            return 2

        try:
            diagnosis = probe_site_deep(site)
        except Exception as exc:
            emit(
                receipt(
                    ok=False,
                    operation="quota-diagnose",
                    target=site.site_id,
                    mutation_state="not_applicable",
                    request_id=request_id,
                    safe_next_action="Check site transport connectivity and retry.",
                    diagnostics={"code": "probe_failed", "message": str(exc)},
                )
            )
            return 2

        emit(
            receipt(
                ok=True,
                operation="quota-diagnose",
                target=site.site_id,
                mutation_state="not_applicable",
                request_id=request_id,
                evidence=diagnosis.to_dict(),
            )
        )
        return 0

    if action == "triage":
        target = args.target
        plan_id = getattr(args, "plan_id", None)
        sites_to_probe: list[SiteConfig] = []

        if target in config.sites:
            target_site = config.sites[target]
            if not plan_id and target_site.portal_plan_id:
                plan_id = target_site.portal_plan_id
            sites_to_probe.append(target_site)

        # If plan_id is not yet resolved, attempt lookup via store for target site domain
        if not plan_id and sites_to_probe:
            site_domain = sites_to_probe[0].public_url.replace("https://", "").replace("http://", "").rstrip("/")
            plan_id = store.find_plan_for_domain(site_domain)

        # If still unresolved, default to target
        if not plan_id:
            plan_id = target

        # 1. Match sites explicitly pinned with portal_plan_id
        for s in config.sites.values():
            if s.portal_plan_id == plan_id and s not in sites_to_probe:
                sites_to_probe.append(s)

        # 2. Load snapshot from telemetry
        snapshot = store.load_snapshot(plan_id)
        if args.telemetry:
            try:
                raw = json.loads(args.telemetry.read_text(encoding="utf-8"))
                snapshot = PlanQuotaSnapshot.from_dict(raw)
            except Exception:
                pass

        # 3. Match any configured site whose domain is in snapshot.site_shares
        if snapshot:
            for domain in snapshot.site_shares:
                for s in config.sites.values():
                    if domains_match(domain, s.public_url) and s not in sites_to_probe:
                        sites_to_probe.append(s)

        if not sites_to_probe and target in config.sites:
            sites_to_probe = [config.sites[target]]

        if snapshot is None:
            snapshot = PlanQuotaSnapshot(
                plan_id=plan_id,
                plan_name=f"Plan {plan_id}",
                observed_at=(
                    datetime.now(timezone.utc)
                    .replace(microsecond=0)
                    .isoformat()
                    .replace("+00:00", "Z")
                ),
            )

        diagnoses: list[SiteDeepDiagnosis] = []
        warnings: list[str] = []
        for s in sites_to_probe:
            try:
                diag = probe_site_deep(s)
                diagnoses.append(diag)
            except Exception as exc:
                warnings.append(f"Site {s.site_id} deep probe skipped: {exc}")

        engine = QuotaTriageEngine()
        report = engine.triage(snapshot, diagnoses)

        # Record to SSOT ledger
        try:
            record_triage_snapshot(
                plan_id=plan_id,
                overall_severity=report.overall_severity,
                total_inodes_used=snapshot.inodes_used,
                total_inodes_limit=snapshot.inodes_limit,
                total_web_space_gb=snapshot.web_space_used_gb,
                reclaimable_inodes=sum(a.estimated_inode_savings or 0 for a in report.remediation_actions),
                immediate_action_needed=report.immediate_action_needed,
                culprits=[c.to_dict() for c in report.top_culprits],
                actions=[a.to_dict() for a in report.remediation_actions],
                raw_report=report.to_dict(),
            )
        except Exception:
            pass

        emit(
            receipt(
                ok=True,
                operation="quota-triage",
                target=plan_id,
                mutation_state="not_applicable",
                request_id=request_id,
                warnings=warnings,
                evidence=report.to_dict(),
            )
        )
        return 0

    if action == "clean":
        target = args.target
        site = _site(config, target, "quota-clean", request_id)
        if site is None:
            return 2

        is_dry_run = getattr(args, "dry_run", False)
        if not is_dry_run:
            if args.confirm_target != site.site_id or not args.recovery_receipt:
                emit(
                    receipt(
                        ok=False,
                        operation="quota-clean",
                        target=site.site_id,
                        mutation_state="refused",
                        request_id=request_id,
                        safe_next_action=(
                            f"Retry with --confirm-target {site.site_id} and --recovery-receipt <receipt>."
                        ),
                        diagnostics={"code": "mutation_confirmation_required"},
                    )
                )
                return 2

        target_label = args.target_dir if args.target_dir else f"staging:{args.target_staging}"
        try:
            if args.target_staging:
                res = clean_sibling_staging(site, args.target_staging, dry_run=is_dry_run)
            else:
                res = clean_site_inodes(site, args.target_dir, dry_run=is_dry_run)
        except ValueError as exc:
            emit(
                receipt(
                    ok=False,
                    operation="quota-clean",
                    target=site.site_id,
                    mutation_state="refused",
                    request_id=request_id,
                    safe_next_action=(
                        f"Select an allowed cleanup target: {sorted(ALLOWED_CLEANUP_TARGETS)} "
                        f"or specify a valid --target-staging name."
                    ),
                    diagnostics={"code": "forbidden_cleanup_target", "message": str(exc)},
                )
            )
            return 2
        except Exception as exc:
            emit(
                receipt(
                    ok=False,
                    operation="quota-clean",
                    target=site.site_id,
                    mutation_state="unknown" if not is_dry_run else "not_applicable",
                    request_id=request_id,
                    safe_next_action="Inspect site filesystem and read back status before retrying.",
                    diagnostics={"code": "cleanup_failed", "message": str(exc)},
                )
            )
            return 1 if is_dry_run else 3

        evidence = dict(res)
        if not is_dry_run:
            evidence["recovery_receipt"] = args.recovery_receipt

        # Record remediation to SSOT ledger
        try:
            record_remediation(
                site_id=site.site_id,
                target_dir=target_label,
                dry_run=is_dry_run,
                inodes_reclaimed=res.get("inodes_reclaimed", 0),
                disk_reclaimed_mb=res.get("disk_reclaimed_mb", 0.0),
                status="dry_run" if is_dry_run else "completed",
                recovery_receipt=getattr(args, "recovery_receipt", "") or "",
                command_executed=res.get("command_executed", ""),
                output_json=res,
            )
        except Exception:
            pass

        emit(
            receipt(
                ok=True,
                operation="quota-clean",
                target=site.site_id,
                mutation_state="not_applicable" if is_dry_run else "applied",
                request_id=request_id,
                evidence=evidence,
            )
        )
        return 0

    if action == "intake":
        messages: list[dict[str, Any]] = []
        if getattr(args, "file", None):
            with open(args.file, "r") as f:
                messages = json.load(f)
            alerts = scan_json_messages(messages)
        else:
            alerts = scan_mail_threads(
                since_days=args.since_days,
                limit=args.limit,
                db_path=getattr(args, "mail_db", None),
            )

        recorded_count = 0
        if getattr(args, "record", True):
            for a in alerts:
                try:
                    record_quota_alert(a)
                    recorded_count += 1
                except Exception:
                    pass

        matched_plans: dict[str, list[str]] = {}
        for a in alerts:
            p_id, matched_sites = match_alert_to_config(a, config)
            if p_id:
                matched_plans.setdefault(p_id, [])
                for s_id in matched_sites:
                    if s_id not in matched_plans[p_id]:
                        matched_plans[p_id].append(s_id)

        triage_results: dict[str, Any] = {}
        if getattr(args, "auto_triage", False) and matched_plans:
            engine = QuotaTriageEngine()
            store = QuotaStore()
            for p_id, s_ids in matched_plans.items():
                snap = store.load_snapshot(p_id)
                if not snap:
                    snap = PlanQuotaSnapshot(
                        plan_id=p_id,
                        plan_name=f"Plan {p_id}",
                        observed_at=datetime.now(timezone.utc).isoformat(),
                    )
                diags: list[SiteDeepDiagnosis] = []
                for s_id in s_ids:
                    s_cfg = config.sites.get(s_id)
                    if s_cfg:
                        try:
                            diags.append(probe_site_deep(s_cfg))
                        except Exception:
                            pass
                rep = engine.triage(snap, diags)
                triage_results[p_id] = rep.to_dict()
                try:
                    record_triage_snapshot(
                        plan_id=p_id,
                        overall_severity=rep.overall_severity,
                        total_inodes_used=snap.inodes_used,
                        total_inodes_limit=snap.inodes_limit,
                        total_web_space_gb=snap.web_space_used_gb,
                        reclaimable_inodes=sum(act.estimated_inode_savings or 0 for act in rep.remediation_actions),
                        immediate_action_needed=rep.immediate_action_needed,
                        culprits=[c.to_dict() for c in rep.top_culprits],
                        actions=[act.to_dict() for act in rep.remediation_actions],
                        raw_report=rep.to_dict(),
                    )
                except Exception:
                    pass

        evidence = {
            "alerts_found": len(alerts),
            "alerts_recorded": recorded_count,
            "alerts": [a.to_dict() for a in alerts],
            "matched_plans": matched_plans,
            "auto_triage_executed": bool(triage_results),
            "triage_results": triage_results,
        }
        emit(
            receipt(
                ok=True,
                operation="quota-intake",
                target="mail_threads",
                mutation_state="applied" if recorded_count > 0 else "not_applicable",
                request_id=request_id,
                evidence=evidence,
            )
        )
        return 0

    if action == "ledger":
        ledger_subaction = getattr(args, "ledger_action", "list") or "list"
        if ledger_subaction == "show":
            alert_id = getattr(args, "alert_id", None)
            if not alert_id:
                emit(
                    receipt(
                        ok=False,
                        operation="quota-ledger",
                        target=None,
                        mutation_state="not_applicable",
                        request_id=request_id,
                        safe_next_action="Provide --id <alert_id> to inspect a specific alert.",
                        diagnostics={"code": "missing_alert_id"},
                    )
                )
                return 2
            alert = get_quota_alert_by_id(alert_id)
            if not alert:
                emit(
                    receipt(
                        ok=False,
                        operation="quota-ledger",
                        target=str(alert_id),
                        mutation_state="not_applicable",
                        request_id=request_id,
                        safe_next_action="Verify alert ID using 'siteground-ops quota ledger list'.",
                        diagnostics={"code": "alert_not_found"},
                    )
                )
                return 1
            emit(
                receipt(
                    ok=True,
                    operation="quota-ledger",
                    target=str(alert_id),
                    mutation_state="not_applicable",
                    request_id=request_id,
                    evidence=alert.to_dict(),
                )
            )
            return 0

        if ledger_subaction == "history":
            site_filter = getattr(args, "site", None)
            limit = getattr(args, "limit", 20)
            records = get_remediation_records(site_id=site_filter, limit=limit)
            emit(
                receipt(
                    ok=True,
                    operation="quota-ledger-history",
                    target=site_filter or "all_sites",
                    mutation_state="not_applicable",
                    request_id=request_id,
                    evidence={"count": len(records), "records": records},
                )
            )
            return 0

        # Default: list alerts and triage snapshots
        limit = getattr(args, "limit", 20)
        status_filter = getattr(args, "status", None)
        plan_filter = getattr(args, "plan_id", None)
        alerts = get_quota_alerts(limit=limit, status=status_filter, plan_id=plan_filter)
        snapshots = get_triage_snapshots(plan_id=plan_filter, limit=5)
        emit(
            receipt(
                ok=True,
                operation="quota-ledger",
                target=plan_filter or "all_plans",
                mutation_state="not_applicable",
                request_id=request_id,
                evidence={
                    "alerts_count": len(alerts),
                    "alerts": [a.to_dict() for a in alerts],
                    "recent_triage_snapshots": snapshots,
                },
            )
        )
        return 0

    emit(
        receipt(
            ok=False,
            operation="quota",
            target=None,
            mutation_state="not_applicable",
            request_id=request_id,
            safe_next_action="Specify a valid quota action: check, diagnose, triage, clean, record, intake, ledger.",
            diagnostics={"code": "invalid_quota_action"},
        )
    )
    return 2


def handle_cache_status(args: argparse.Namespace, config: OpsConfig, request_id: str) -> int:
    site = _site(config, args.target, "cache-status", request_id)
    if site is None:
        return 2

    public_headers = probe_public_cache_headers(site.public_url)
    app_status = None
    warnings: list[str] = []
    try:
        selected_transport, runner = build_read_runner(site, getattr(args, "transport", "auto"))
        if hasattr(runner, "cache_status"):
            app_status = runner.cache_status()
            app_status["transport"] = selected_transport
    except Exception as exc:
        warnings.append(f"Could not read WordPress in-app cache configuration: {exc}")

    cacher_link = (
        f"https://tools.siteground.com/cacher?siteId={site.portal_site_id}"
        if site.portal_site_id
        else None
    )
    edge_enabled = public_headers.get("x_cache_enabled") == "True"
    if not edge_enabled and cacher_link:
        warnings.append(
            f"SiteGround Nginx edge cache reports x-cache-enabled={public_headers.get('x_cache_enabled')!r}. "
            f"Toggle Dynamic Cache ON in Site Tools: {cacher_link}"
        )

    evidence = {
        "public_url": site.public_url,
        "portal_site_id": site.portal_site_id,
        "cacher_link": cacher_link,
        "edge_headers": public_headers,
        "wordpress_app_status": app_status,
        "edge_cache_active": edge_enabled,
    }
    emit(
        receipt(
            ok=True,
            operation="cache-status",
            target=site.site_id,
            mutation_state="not_applicable",
            request_id=request_id,
            evidence=evidence,
            warnings=warnings,
        )
    )
    return 0


def handle_onboard(args: argparse.Namespace, config: OpsConfig, request_id: str) -> int:
    site = _site(config, args.target, "onboard", request_id)
    if site is None:
        return 2

    parsed_url = urlsplit(site.public_url)
    hostname = parsed_url.hostname or ""
    is_temporary_domain = "sg-host.com" in hostname.lower()

    links = (
        site_tools_links(site)
        if site.portal_site_id
        else {}
    )

    transport_status = read_transport_status(site)
    public_headers = probe_public_cache_headers(site.public_url)

    stack_inventory = None
    warnings: list[str] = []
    try:
        selected_transport, runner = build_read_runner(site, getattr(args, "transport", "auto"))
        doc = runner.doctor()
        inv = runner.inventory()
        stack_inventory = {
            "transport": selected_transport,
            "wordpress_version": doc.get("wordpress_version"),
            "optimizer_active": doc.get("siteground_optimizer_active"),
            "plugins": {p["id"]: p.get("version") for p in inv.get("plugins", [])},
        }
    except Exception as exc:
        warnings.append(f"In-app stack audit could not complete: {exc}")

    plugins_dict = stack_inventory.get("plugins", {}) if stack_inventory else {}
    checklist = {
        "site_profile_registered": True,
        "temporary_domain": is_temporary_domain,
        "portal_mapped": bool(site.portal_site_id),
        "transport_ready": bool(transport_status.get("available")),
        "wordpress_reachable": stack_inventory is not None,
        "speed_optimizer_active": bool(stack_inventory and stack_inventory.get("optimizer_active")),
        "surecart_installed": "surecart" in plugins_dict,
        "turnstile_installed": "simple-cloudflare-turnstile" in plugins_dict,
        "fluent_forms_installed": "fluentform" in plugins_dict,
        "edge_cache_enabled": public_headers.get("x_cache_enabled") == "True",
    }

    next_steps = []
    if not checklist["edge_cache_enabled"] and links.get("cache"):
        next_steps.append(
            f"1. Enable SiteGround Nginx SuperCacher in Site Tools: {links['cache']}"
        )
    if not checklist["turnstile_installed"]:
        next_steps.append("2. Install & activate simple-cloudflare-turnstile plugin.")
    else:
        next_steps.append("2. Verify Turnstile keys in WP Admin -> Settings -> Cloudflare Turnstile.")
    if checklist["surecart_installed"]:
        next_steps.append("3. Connect SureCart API Token & Stripe payment gateway in WP Admin -> SureCart.")
    if is_temporary_domain:
        next_steps.append(
            "4. Phase 2 Cutover: When ready for custom domain, use cloudflare-dns-manager to point DNS, "
            "change primary domain in Site Tools, and run search-replace on database."
        )

    evidence = {
        "site_id": site.site_id,
        "label": site.label,
        "environment": site.environment,
        "public_url": site.public_url,
        "domain_type": "temporary_sg_host" if is_temporary_domain else "custom",
        "portal_account": site.portal_account,
        "portal_site_id": site.portal_site_id,
        "links": links,
        "transports": transport_status,
        "public_edge": public_headers,
        "stack": stack_inventory,
        "checklist": checklist,
        "next_steps": next_steps,
    }

    emit(
        receipt(
            ok=True,
            operation="onboard",
            target=site.site_id,
            mutation_state="not_applicable",
            request_id=request_id,
            evidence=evidence,
            warnings=warnings,
        )
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    request_id = str(uuid.uuid4())
    if args.operation == "novamira-update":
        return handle_novamira_update(args, request_id)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        emit(
            receipt(
                ok=False,
                operation=args.operation,
                target=getattr(args, "target", None),
                mutation_state="refused",
                request_id=request_id,
                safe_next_action="Repair the non-secret site configuration and retry.",
                diagnostics={"code": "config_invalid", "message": str(exc)},
            )
        )
        return 2

    if args.operation == "portal":
        return handle_portal(args, config, request_id)

    if args.operation == "wp-admin":
        return handle_wp_admin(args, config, request_id)

    if args.operation == "quota":
        return handle_quota(args, config, request_id)

    if args.operation == "sites":
        summaries = []
        for site in config.sites.values():
            status = read_transport_status(site)
            summaries.append(
                site.public_summary(
                    read_transports=status["available"],
                    read_missing=status["missing"],
                )
            )
        emit(
            receipt(
                ok=True,
                operation="sites",
                target=None,
                mutation_state="not_applicable",
                request_id=request_id,
                evidence={"sites": summaries},
            )
        )
        return 0

    site = _site(config, args.target, args.operation, request_id)
    if site is None:
        return 2

    if args.operation == "cache-status":
        return handle_cache_status(args, config, request_id)

    if args.operation == "onboard":
        return handle_onboard(args, config, request_id)

    if args.operation == "doctor":
        try:
            selected_transport, runner = build_read_runner(site, args.transport)
        except RunnerError as exc:
            emit(
                receipt(
                    ok=False,
                    operation="doctor",
                    target=site.site_id,
                    mutation_state="refused",
                    request_id=request_id,
                    safe_next_action="Choose a configured read transport or repair its non-secret owner pointer.",
                    diagnostics={"code": "read_transport_unavailable", "message": str(exc)},
                )
            )
            return 2
        try:
            evidence = runner.doctor()
        except Exception as exc:
            emit(
                receipt(
                    ok=False,
                    operation="doctor",
                    target=site.site_id,
                    mutation_state="not_applicable",
                    request_id=request_id,
                    safe_next_action="Check the selected transport, exact target identity, and its credential owner.",
                    diagnostics={"code": "doctor_failed", "message": str(exc)},
                )
            )
            return 1
        evidence["transport"] = selected_transport
        emit(
            receipt(
                ok=True,
                operation="doctor",
                target=site.site_id,
                mutation_state="not_applicable",
                request_id=request_id,
                evidence=evidence,
            )
        )
        return 0

    if args.operation == "inventory":
        try:
            selected_transport, runner = build_read_runner(site, args.transport)
        except RunnerError as exc:
            emit(
                receipt(
                    ok=False,
                    operation="inventory",
                    target=site.site_id,
                    mutation_state="refused",
                    request_id=request_id,
                    safe_next_action="Choose a configured read transport or repair its non-secret owner pointer.",
                    diagnostics={"code": "read_transport_unavailable", "message": str(exc)},
                )
            )
            return 2
        try:
            evidence = runner.inventory()
        except Exception as exc:
            emit(
                receipt(
                    ok=False,
                    operation="inventory",
                    target=site.site_id,
                    mutation_state="not_applicable",
                    request_id=request_id,
                    safe_next_action="Check the selected transport, exact target identity, and its credential owner.",
                    diagnostics={"code": "inventory_failed", "message": str(exc)},
                )
            )
            return 1
        evidence["transport"] = selected_transport
        emit(
            receipt(
                ok=True,
                operation="inventory",
                target=site.site_id,
                mutation_state="not_applicable",
                request_id=request_id,
                evidence=evidence,
            )
        )
        return 0

    transport = getattr(args, "transport", "auto")
    if transport in {"auto", "ssh"}:
        if ssh_local_readiness_issues(site):
            emit(
                receipt(
                    ok=False,
                    operation="cache-purge",
                    target=site.site_id,
                    mutation_state="refused",
                    request_id=request_id,
                    safe_next_action="Configure and verify the SSH/WP-CLI transport before any cache mutation.",
                    diagnostics={"code": "ssh_mutation_transport_required"},
                )
            )
            return 2
        runner = build_runner(site)
        resolved_transport = "ssh"
    elif transport == "novamira":
        if not site.novamira_server:
            emit(
                receipt(
                    ok=False,
                    operation="cache-purge",
                    target=site.site_id,
                    mutation_state="refused",
                    request_id=request_id,
                    safe_next_action="Site has no configured novamira_server.",
                    diagnostics={"code": "novamira_server_missing"},
                )
            )
            return 2
        try:
            runner = build_novamira_runner(site)
        except RunnerError as exc:
            emit(
                receipt(
                    ok=False,
                    operation="cache-purge",
                    target=site.site_id,
                    mutation_state="refused",
                    request_id=request_id,
                    safe_next_action="Configure and verify the Novamira MCP transport before any cache mutation.",
                    diagnostics={"code": "novamira_transport_unavailable", "message": str(exc)},
                )
            )
            return 2
        resolved_transport = "novamira"
    else:
        emit(
            receipt(
                ok=False,
                operation="cache-purge",
                target=site.site_id,
                mutation_state="refused",
                request_id=request_id,
                safe_next_action="Choose a valid transport: auto, ssh, or novamira.",
                diagnostics={"code": "invalid_transport"},
            )
        )
        return 2

    if args.confirm_target != site.site_id or not args.recovery_receipt:
        emit(
            receipt(
                ok=False,
                operation="cache-purge",
                target=site.site_id,
                mutation_state="refused",
                request_id=request_id,
                safe_next_action=(
                    f"Retry with --confirm-target {site.site_id} and --recovery-receipt <receipt>."
                ),
                diagnostics={"code": "mutation_confirmation_required"},
            )
        )
        return 2

    try:
        evidence = runner.purge_cache(request_id)
        evidence["transport"] = resolved_transport
    except TimeoutError as exc:
        emit(
            receipt(
                ok=False,
                operation="cache-purge",
                target=site.site_id,
                mutation_state="unknown",
                request_id=request_id,
                safe_next_action="Do not retry. Independently read back cache and public state first.",
                diagnostics={"code": "mutation_outcome_unknown", "message": str(exc)},
            )
        )
        return 3
    except (RunnerError, OSError) as exc:
        emit(
            receipt(
                ok=False,
                operation="cache-purge",
                target=site.site_id,
                mutation_state="unknown",
                request_id=request_id,
                safe_next_action="Do not retry until public state and the request outcome are read back.",
                diagnostics={"code": "mutation_failed", "message": str(exc)},
            )
        )
        return 3

    evidence["recovery_receipt"] = args.recovery_receipt
    emit(
        receipt(
            ok=True,
            operation="cache-purge",
            target=site.site_id,
            mutation_state="applied",
            request_id=request_id,
            evidence=evidence,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
