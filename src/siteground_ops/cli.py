from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
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
from .receipts import receipt
from .runner import (
    RunnerError,
    build_read_runner,
    build_runner,
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
        evidence = build_runner(site).purge_cache(request_id)
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
