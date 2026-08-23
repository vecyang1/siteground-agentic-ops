from __future__ import annotations

import json
from pathlib import Path

import pytest

from siteground_ops.config import ConfigError, load_config


def write_portal_config(tmp_path: Path, *, schema_version: int = 2) -> Path:
    payload = {
        "schema_version": schema_version,
        "sites": {
            "prod": {
                "label": "Production",
                "environment": "production",
                "adapter": "novamira_mcp",
                "credential_pointer": "novamira-ops exact server",
                "recovery_pointer": "SiteGround backup owner",
                "public_url": "https://example.com",
                "novamira_server": "novamira-example",
                "portal_account": "primary",
                "portal_site_id": "EXAMPLESITEID003",
                "portal_plan_id": "EXAMPLEPLANID005",
            }
        },
        "portal_accounts": {
            "primary": {
                "label": "Primary SiteGround account",
                "adapter": "opencli",
                "opencli_path": "/Users/example/.local/bin/opencli",
                "opencli_profile": "profile-alias",
                "credential_pointer": "OpenCLI Browser Bridge logged-in Chrome profile",
                "expected_domains": ["example.com"],
            }
        },
    }
    path = tmp_path / "sites.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_schema_v2_loads_portal_accounts_and_exact_site_mapping(tmp_path: Path) -> None:
    config = load_config(write_portal_config(tmp_path))

    account = config.portal_accounts["primary"]
    site = config.sites["prod"]
    assert account.adapter == "opencli"
    assert account.opencli_path == Path("/Users/example/.local/bin/opencli")
    assert account.opencli_profile == "profile-alias"
    assert account.expected_domains == ("example.com",)
    assert site.portal_account == "primary"
    assert site.portal_site_id == "EXAMPLESITEID003"
    assert site.portal_plan_id == "EXAMPLEPLANID005"


def test_schema_v1_remains_valid_without_portal_accounts(tmp_path: Path) -> None:
    path = write_portal_config(tmp_path, schema_version=1)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("portal_accounts")
    for site in payload["sites"].values():
        site.pop("portal_account")
        site.pop("portal_site_id")
        site.pop("portal_plan_id")
    path.write_text(json.dumps(payload), encoding="utf-8")

    config = load_config(path)

    assert config.schema_version == 1
    assert config.portal_accounts == {}


def test_portal_config_rejects_secret_like_fields(tmp_path: Path) -> None:
    path = write_portal_config(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["portal_accounts"]["primary"]["session_token"] = "never-inline"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ConfigError, match="secret-like field"):
        load_config(path)


def test_portal_mapping_requires_a_known_account(tmp_path: Path) -> None:
    path = write_portal_config(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["sites"]["prod"]["portal_account"] = "missing"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ConfigError, match="unknown portal account"):
        load_config(path)


def test_portal_only_site_requires_exact_portal_mapping(tmp_path: Path) -> None:
    path = write_portal_config(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["sites"]["portal-only"] = {
        "label": "Portal-only SiteGround site",
        "environment": "production",
        "adapter": "portal_only",
        "credential_pointer": "OpenCLI Browser Bridge logged-in Chrome profile",
        "recovery_pointer": "SiteGround backup owner before any future mutation",
        "public_url": "https://portal-only.example.com",
        "portal_account": "primary",
        "portal_site_id": "EXAMPLESITEID003",
        "portal_plan_id": "EXAMPLEPLANID005",
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    config = load_config(path)
    site = config.sites["portal-only"]

    assert site.adapter == "portal_only"
    assert site.ready is False
    assert site.read_ready is False
    assert site.read_transports == []
    assert site.portal_account == "primary"


@pytest.mark.parametrize("missing_field", ["portal_account", "portal_site_id"])
def test_portal_only_site_refuses_incomplete_mapping(tmp_path: Path, missing_field: str) -> None:
    path = write_portal_config(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["sites"]["portal-only"] = {
        "label": "Portal-only SiteGround site",
        "environment": "production",
        "adapter": "portal_only",
        "credential_pointer": "OpenCLI Browser Bridge logged-in Chrome profile",
        "recovery_pointer": "SiteGround backup owner before any future mutation",
        "public_url": "https://portal-only.example.com",
        "portal_account": "primary",
        "portal_site_id": "EXAMPLESITEID003",
        "portal_plan_id": "EXAMPLEPLANID005",
    }
    del payload["sites"]["portal-only"][missing_field]
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ConfigError, match="portal_only"):
        load_config(path)
