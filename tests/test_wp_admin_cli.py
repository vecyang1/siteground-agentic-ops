from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from siteground_ops import cli
from siteground_ops.portal import PortalError, PortalUnknownOutcomeError


SITE_ID = "EXAMPLESITEID003"
STAGING_SITE_ID = "EXAMPLESITEID002"


def write_config(tmp_path: Path, *, accounts: dict[str, Any] | None = None) -> Path:
    opencli = tmp_path / "opencli"
    opencli.write_text("#!/bin/sh\n", encoding="utf-8")
    opencli.chmod(0o755)
    payload = {
        "schema_version": 2,
        "sites": {
            "example-shop-siteground": {
                "label": "ExampleShop",
                "environment": "production",
                "adapter": "portal_only",
                "credential_pointer": "OpenCLI Browser Bridge profile",
                "recovery_pointer": "SiteGround provider backup",
                "public_url": "https://example-shop.com",
                "portal_account": "primary",
                "portal_site_id": SITE_ID,
            },
            "worldinspire-production": {
                "label": "World Inspire",
                "environment": "production",
                "adapter": "novamira_mcp",
                "credential_pointer": "novamira-ops exact server",
                "recovery_pointer": "SiteGround provider backup",
                "public_url": "https://example-main.com",
                "novamira_server": "novamira-example-main-com",
            },
        },
        "portal_accounts": accounts
        if accounts is not None
        else {
            "primary": {
                "label": "Primary",
                "adapter": "opencli",
                "opencli_path": str(opencli),
                "opencli_profile": "profile-alias",
                "credential_pointer": "OpenCLI Browser Bridge profile",
                "expected_domains": ["example-shop.com"],
            }
        },
    }
    config = tmp_path / "sites.json"
    config.write_text(json.dumps(payload), encoding="utf-8")
    return config


class StubAdapter:
    def __init__(self, *, apps: list[dict[str, Any]] | None = None, login: Any = None) -> None:
        # Mirrors the live provider shape: every application of a site carries the
        # site's domain, so only `admin_host` separates production from staging.
        self.apps = apps if apps is not None else [
            {"domain": "example-shop.com", "admin_host": "example-shop.com", "site_id": SITE_ID,
             "app_id": "1", "cms": "woocommerce",
             "admin_url": "https://example-shop.com/wp-admin", "status": "active"},
            {"domain": "example-main.com", "admin_host": "example-main.com", "site_id": STAGING_SITE_ID,
             "app_id": "1", "cms": "wordpress",
             "admin_url": "http://example-main.com/wp-admin", "status": "active"},
            {"domain": "example-main.com", "admin_host": "staging2.example-main.com",
             "site_id": STAGING_SITE_ID, "app_id": "2", "cms": "wordpress",
             "admin_url": "http://staging2.example-main.com/wp-admin", "status": "active"},
        ]
        self.login = login
        self.calls: list[dict[str, Any]] = []

    def wordpress_apps(self) -> dict[str, Any]:
        return {"rows": self.apps}

    def open_wordpress_admin(
        self, *, site_id: str, app_id: str | None, expected_domain: str, foreground: bool = False
    ) -> dict[str, Any]:
        self.calls.append({
            "site_id": site_id, "app_id": app_id, "expected_domain": expected_domain, "foreground": foreground,
        })
        if isinstance(self.login, Exception):
            raise self.login
        return self.login or {
            "account_id": "primary",
            "admin_host": expected_domain,
            "app_id": app_id or "1",
            "credential_disclosed": False,
            "domain": expected_domain,
            "logged_in": True,
            "observed_at": "2026-08-23T00:00:00Z",
            "page_title": "Dashboard ‹ ExampleShop — WordPress",
            "site_id": site_id,
            "transport": "opencli",
            "wp_admin_url": f"https://{expected_domain}/wp-admin/",
        }


def run(monkeypatch, capsys, argv: list[str], adapter: StubAdapter) -> tuple[int, dict[str, Any]]:
    monkeypatch.setattr(cli, "build_portal_adapter", lambda account: adapter)
    code = cli.main(argv)
    return code, json.loads(capsys.readouterr().out)


def test_wp_admin_opens_the_pinned_site_and_never_returns_a_credential(tmp_path, monkeypatch, capsys):
    adapter = StubAdapter()
    code, payload = run(
        monkeypatch, capsys,
        ["--config", str(write_config(tmp_path)), "wp-admin", "example-shop-siteground"],
        adapter,
    )

    assert code == 0
    assert payload["ok"] is True
    assert payload["operation"] == "wp-admin"
    assert payload["target"] == "example-shop-siteground"
    assert payload["evidence"]["wp_admin_url"] == "https://example-shop.com/wp-admin/"
    assert payload["evidence"]["credential_disclosed"] is False
    assert adapter.calls == [{"site_id": SITE_ID, "app_id": None, "expected_domain": "example-shop.com", "foreground": False}]
    assert "wp_auto_login" not in json.dumps(payload)
    assert "autologin_url" not in json.dumps(payload)


def test_wp_admin_resolves_a_site_id_from_the_live_inventory_by_domain(tmp_path, monkeypatch, capsys):
    adapter = StubAdapter()
    code, payload = run(
        monkeypatch, capsys,
        ["--config", str(write_config(tmp_path)), "wp-admin", "worldinspire-production", "--app", "1"],
        adapter,
    )

    assert code == 0
    assert adapter.calls == [
        {"site_id": STAGING_SITE_ID, "app_id": "1", "expected_domain": "example-main.com", "foreground": False}
    ]
    assert payload["evidence"]["resolved_from"] == "portal_inventory"


def test_wp_admin_refuses_a_site_with_more_than_one_application_until_app_is_named(tmp_path, monkeypatch, capsys):
    adapter = StubAdapter()
    code, payload = run(
        monkeypatch, capsys,
        ["--config", str(write_config(tmp_path)), "wp-admin", "worldinspire-production"],
        adapter,
    )

    assert code == 2
    assert payload["ok"] is False
    assert payload["mutation_state"] == "refused"
    assert payload["diagnostics"]["code"] == "ambiguous_wordpress_application"
    assert payload["diagnostics"]["available_app_ids"] == ["1", "2"]
    assert adapter.calls == []


def test_wp_admin_refuses_an_unknown_target(tmp_path, monkeypatch, capsys):
    adapter = StubAdapter()
    code, payload = run(
        monkeypatch, capsys,
        ["--config", str(write_config(tmp_path)), "wp-admin", "not-a-site"],
        adapter,
    )

    assert code == 2
    assert payload["diagnostics"]["code"] == "unknown_target"
    assert adapter.calls == []


def test_wp_admin_refuses_a_domain_the_portal_does_not_serve(tmp_path, monkeypatch, capsys):
    adapter = StubAdapter(apps=[
        {"domain": "example-other.com", "admin_host": "example-other.com", "site_id": SITE_ID,
         "app_id": "1", "cms": "wordpress",
         "admin_url": "https://example-other.com/wp-admin", "status": "active"},
    ])
    config = write_config(tmp_path)
    code, payload = run(
        monkeypatch, capsys,
        ["--config", str(config), "wp-admin", "worldinspire-production"],
        adapter,
    )

    assert code == 2
    assert payload["diagnostics"]["code"] == "wordpress_application_not_found"
    assert adapter.calls == []


def test_wp_admin_refuses_when_more_than_one_portal_account_is_configured(tmp_path, monkeypatch, capsys):
    opencli = tmp_path / "opencli"
    accounts = {
        name: {
            "label": name,
            "adapter": "opencli",
            "opencli_path": str(opencli),
            "opencli_profile": f"profile-{name}",
            "credential_pointer": "OpenCLI Browser Bridge profile",
            "expected_domains": ["example-shop.com"],
        }
        for name in ("primary", "secondary")
    }
    adapter = StubAdapter()
    code, payload = run(
        monkeypatch, capsys,
        ["--config", str(write_config(tmp_path, accounts=accounts)), "wp-admin", "worldinspire-production"],
        adapter,
    )

    assert code == 2
    assert payload["diagnostics"]["code"] == "ambiguous_portal_account"
    assert adapter.calls == []


def test_wp_admin_reports_an_unknown_outcome_without_suggesting_a_retry(tmp_path, monkeypatch, capsys):
    adapter = StubAdapter(login=PortalUnknownOutcomeError("timed out"))
    code, payload = run(
        monkeypatch, capsys,
        ["--config", str(write_config(tmp_path)), "wp-admin", "example-shop-siteground"],
        adapter,
    )

    assert code == 3
    assert payload["mutation_state"] == "unknown"
    assert payload["diagnostics"]["code"] == "wordpress_login_outcome_unknown"
    assert "retry" not in payload["safe_next_action"].lower()


def test_wp_admin_reports_a_clean_failure_as_not_applied(tmp_path, monkeypatch, capsys):
    adapter = StubAdapter(login=PortalError("OpenCLI wp-login failed: portal session is expired"))
    code, payload = run(
        monkeypatch, capsys,
        ["--config", str(write_config(tmp_path)), "wp-admin", "example-shop-siteground"],
        adapter,
    )

    assert code == 1
    assert payload["mutation_state"] == "not_applicable"
    assert payload["diagnostics"]["code"] == "wordpress_login_failed"
    assert "expired" in payload["diagnostics"]["message"]


def test_wp_admin_warns_when_the_named_application_opens_a_staging_host(tmp_path, monkeypatch, capsys):
    adapter = StubAdapter()
    code, payload = run(
        monkeypatch, capsys,
        ["--config", str(write_config(tmp_path)), "wp-admin", "worldinspire-production", "--app", "2"],
        adapter,
    )

    assert code == 0
    assert adapter.calls == [
        {"site_id": STAGING_SITE_ID, "app_id": "2", "expected_domain": "staging2.example-main.com", "foreground": False}
    ]
    assert payload["warnings"] == [
        "Application 2 opens staging2.example-main.com, not the profile domain example-main.com."
    ]


def test_wp_admin_refuses_an_application_with_no_readable_admin_host(tmp_path, monkeypatch, capsys):
    adapter = StubAdapter(apps=[
        {"domain": "example-shop.com", "admin_host": "", "site_id": SITE_ID, "app_id": "1",
         "cms": "woocommerce", "admin_url": "", "status": "active"},
    ])
    code, payload = run(
        monkeypatch, capsys,
        ["--config", str(write_config(tmp_path)), "wp-admin", "example-shop-siteground"],
        adapter,
    )

    assert code == 2
    assert payload["diagnostics"]["code"] == "wordpress_admin_host_unknown"
    assert adapter.calls == []


def test_wp_admin_accepts_a_bare_domain_the_portal_serves(tmp_path, monkeypatch, capsys):
    adapter = StubAdapter()
    code, payload = run(
        monkeypatch, capsys,
        ["--config", str(write_config(tmp_path)), "wp-admin", "example-shop.com"],
        adapter,
    )

    assert code == 0
    assert payload["target"] == "example-shop.com"
    assert payload["evidence"]["resolved_from"] == "portal_inventory"
    assert adapter.calls == [{"site_id": SITE_ID, "app_id": None, "expected_domain": "example-shop.com", "foreground": False}]


def test_wp_admin_refuses_a_target_that_is_neither_a_profile_nor_a_domain(tmp_path, monkeypatch, capsys):
    adapter = StubAdapter()
    code, payload = run(
        monkeypatch, capsys,
        ["--config", str(write_config(tmp_path)), "wp-admin", "not a site"],
        adapter,
    )

    assert code == 2
    assert payload["diagnostics"]["code"] == "unknown_target"
    assert adapter.calls == []


def test_wp_admin_uses_a_pinned_site_id_when_the_profile_domain_is_a_custom_domain(tmp_path, monkeypatch, capsys):
    """A profile whose public_url is customer-facing never matches the portal's
    own site domain, so the pinned id must win over any domain match."""
    opencli = tmp_path / "opencli"
    opencli.write_text("#!/bin/sh\n", encoding="utf-8")
    opencli.chmod(0o755)
    config = tmp_path / "sites.json"
    config.write_text(json.dumps({
        "schema_version": 2,
        "sites": {
            "example-client-crm": {
                "label": "Car Radio Codes CRM",
                "environment": "production",
                "adapter": "portal_only",
                "credential_pointer": "OpenCLI Browser Bridge profile",
                "recovery_pointer": "SiteGround provider backup",
                "public_url": "https://hi.example-client.co.uk",
                "portal_account": "primary",
                "portal_site_id": SITE_ID,
            }
        },
        "portal_accounts": {
            "primary": {
                "label": "Primary",
                "adapter": "opencli",
                "opencli_path": str(opencli),
                "opencli_profile": "profile-alias",
                "credential_pointer": "OpenCLI Browser Bridge profile",
                "expected_domains": ["example-shop.com"],
            }
        },
    }), encoding="utf-8")

    adapter = StubAdapter()
    code, payload = run(monkeypatch, capsys, ["--config", str(config), "wp-admin", "example-client-crm"], adapter)

    assert code == 0
    assert payload["evidence"]["resolved_from"] == "site_profile"
    assert adapter.calls == [{"site_id": SITE_ID, "app_id": None, "expected_domain": "example-shop.com", "foreground": False}]
    assert payload["warnings"] == [
        "Application 1 opens example-shop.com, not the profile domain hi.example-client.co.uk."
    ]


def test_wp_admin_refuses_a_pinned_site_id_the_portal_no_longer_lists(tmp_path, monkeypatch, capsys):
    adapter = StubAdapter(apps=[
        {"domain": "example-other.com", "admin_host": "example-other.com", "site_id": "Zm9vYmFyYmF6",
         "app_id": "1", "cms": "wordpress",
         "admin_url": "https://example-other.com/wp-admin", "status": "active"},
    ])
    code, payload = run(
        monkeypatch, capsys,
        ["--config", str(write_config(tmp_path)), "wp-admin", "example-shop-siteground"],
        adapter,
    )

    assert code == 2
    assert payload["diagnostics"]["code"] == "wordpress_application_not_found"
    assert payload["diagnostics"]["pinned_site_id"] == SITE_ID
    assert adapter.calls == []


def test_wp_admin_opens_quietly_unless_foreground_is_asked_for(tmp_path, monkeypatch, capsys):
    config = write_config(tmp_path)
    quiet = StubAdapter()
    code, payload = run(monkeypatch, capsys, ["--config", str(config), "wp-admin", "example-shop-siteground"], quiet)
    assert code == 0
    assert quiet.calls[0]["foreground"] is False

    loud = StubAdapter()
    code, payload = run(
        monkeypatch, capsys,
        ["--config", str(config), "wp-admin", "example-shop-siteground", "--foreground"],
        loud,
    )
    assert code == 0
    assert loud.calls[0]["foreground"] is True
