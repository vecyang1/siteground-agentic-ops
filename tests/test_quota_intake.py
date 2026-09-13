from __future__ import annotations

import json
from pathlib import Path
import pytest

from siteground_ops.config import OpsConfig, SiteConfig
from siteground_ops.quota import QuotaSeverity
from siteground_ops.quota_intake import (
    QuotaAlertRecord,
    match_alert_to_config,
    parse_siteground_alert_email,
    scan_json_messages,
)
from siteground_ops.quota_ledger import (
    get_quota_alert_by_id,
    get_quota_alerts,
    record_quota_alert,
)


def test_parse_cpu_seconds_exhausted_alert() -> None:
    msg = {
        "id": 1393,
        "sender_email": "noreply@siteground.com",
        "sender_name": "SiteGround",
        "subject": "Important: Your hosting plan is now limited. 100% of your monthly CPU has been used.",
        "date": "2026-09-13 00:32:03",
        "timestamp": 1789259523,
        "body": (
            "Dear Vector, We would like to inform you that your Hosting Plan 3 hosting plan has reached "
            "100% or more of its allowed CPU seconds quota. It is now limited till the end of the current month. "
            "To review the CPU seconds usage in your account, go to the Statistics menu: "
            "https://my.siteground.com/services/hosting/TFEvK1ozb1BKUT09/statistics "
            "Upgrade to a plan suitable: https://my.siteground.com/upgrade/TFEvK1ozb1BKUT09/ "
        ),
    }

    alert = parse_siteground_alert_email(msg)
    assert alert is not None
    assert alert.message_id == 1393
    assert alert.alert_type == "cpu_seconds"
    assert alert.severity == QuotaSeverity.EXHAUSTED
    assert alert.plan_name == "Hosting Plan 3"
    assert alert.plan_id == "TFEvK1ozb1BKUT09"
    assert alert.stats_url == "https://my.siteground.com/services/hosting/TFEvK1ozb1BKUT09/statistics"


def test_parse_inode_quota_alert_with_affected_websites() -> None:
    msg = {
        "id": 1455,
        "sender_email": "noreply@siteground.com",
        "sender_name": "SiteGround.com",
        "subject": "Important: Your Hosting Plan 3 hosting plan reached 90% of the allowed Inode quota",
        "date": "2026-09-13 19:16:05",
        "timestamp": 1789326965,
        "body": (
            "Dear Customer,\n\nWe would like to warn you that your Hosting Plan 3 hosting plan has already "
            "exceeded 90% of its maximum allowed inode quota.\n\n"
            "If you do not take timely action, the following websites hosted on your Hosting Plan 3 plan might be affected:\n\n"
            "example-main.com\nexample-shop.com\nexample-studio.com\n\n"
            "Possible solutions:\n"
            "1. Review and reduce the inodes usage: "
            "https://my.siteground.com/services/hosting/TFEvK1ozb1BKUT09/statistics\n"
        ),
    }

    alert = parse_siteground_alert_email(msg)
    assert alert is not None
    assert alert.message_id == 1455
    assert alert.alert_type == "inode_quota"
    assert alert.severity == QuotaSeverity.CRITICAL
    assert alert.plan_name == "Hosting Plan 3"
    assert alert.plan_id == "TFEvK1ozb1BKUT09"
    assert "example-main.com" in alert.domains
    assert "example-shop.com" in alert.domains
    assert "example-studio.com" in alert.domains


def test_parse_subject_domain_and_performance_report() -> None:
    msg_subj = {
        "id": 7837,
        "sender_email": "noreply@siteground.com",
        "subject": "example-main.com Hosting Plan Reached Allowed Monthly CPU Seconds",
        "date": "2026-09-13 01:12:00",
        "timestamp": 1789261920,
        "body": "",
    }
    alert1 = parse_siteground_alert_email(msg_subj)
    assert alert1 is not None
    assert alert1.alert_type == "cpu_seconds"
    assert alert1.domains == ["example-main.com"]

    msg_rep = {
        "id": 7729,
        "sender_email": "noreply@siteground.com",
        "subject": "Monthly performance report for example-shop.com",
        "date": "2026-09-13 04:56:17",
        "timestamp": 1789275377,
        "body": "Traffic and speed summary...",
    }
    alert2 = parse_siteground_alert_email(msg_rep)
    assert alert2 is not None
    assert alert2.alert_type == "performance_report"
    assert alert2.domains == ["example-shop.com"]


def test_parse_deceptive_and_non_siteground_messages() -> None:
    # 1. Non-siteground message
    msg_spam = {
        "id": 901,
        "sender_email": "newsletter@randomvendor.example.org",
        "sender_name": "Random News",
        "subject": "Weekly Tech Trends",
        "body": "Check out the latest gadgets.",
    }
    assert parse_siteground_alert_email(msg_spam) is None

    # 2. SiteGround marketing / non-quota email
    msg_mktg = {
        "id": 902,
        "sender_email": "noreply@siteground.com",
        "sender_name": "SiteGround",
        "subject": "Sales Receipt for service renewal",
        "body": "Thank you for your payment. Invoice #12345.",
    }
    assert parse_siteground_alert_email(msg_mktg) is None

    # 3. Empty dictionary
    assert parse_siteground_alert_email({}) is None


def test_match_alert_to_config() -> None:
    config = OpsConfig(
        schema_version=2,
        portal_accounts={},
        sites={
            "site-main": SiteConfig(
                site_id="site-main",
                label="Main Site",
                environment="production",
                adapter="novamira_mcp",
                credential_pointer="test_ptr",
                recovery_pointer="test_rec",
                public_url="https://example-main.com",
                portal_plan_id="TEFud1ozd0pJUT09",
            ),
            "site-shop": SiteConfig(
                site_id="site-shop",
                label="Shop Site",
                environment="production",
                adapter="novamira_mcp",
                credential_pointer="test_ptr",
                recovery_pointer="test_rec",
                public_url="https://example-shop.com",
                portal_plan_id="TEFud1ozd0pJUT09",
            ),
        }
    )

    alert = QuotaAlertRecord(
        message_id=999,
        sender="noreply@siteground.com",
        subject="Important Alert",
        date_received="2026-09-13",
        timestamp=1789250000,
        alert_type="cpu_seconds",
        severity="critical",
        plan_id="TEFud1ozd0pJUT09",
        domains=["example-main.com", "example-shop.com"],
    )

    matched_plan, matched_sites = match_alert_to_config(alert, config)
    assert matched_plan == "TEFud1ozd0pJUT09"
    assert "site-main" in matched_sites
    assert "site-shop" in matched_sites
    assert "site-main" in matched_sites
    assert "site-shop" in matched_sites


def test_scan_json_messages_batch() -> None:
    msgs = [
        {
            "id": 101,
            "sender_email": "noreply@siteground.com",
            "subject": "example-main.com Hosting Plan Reached Allowed Monthly CPU Seconds",
            "body": "",
        },
        {
            "id": 102,
            "sender_email": "spam@example.org",
            "subject": "Hello",
            "body": "No quota info",
        },
    ]
    alerts = scan_json_messages(msgs)
    assert len(alerts) == 1
    assert alerts[0].message_id == 101


def test_cli_quota_intake_and_ledger_e2e(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from siteground_ops.cli import main

    db_file = tmp_path / "ledger_cli.db"
    monkeypatch.setenv("OPS_QUOTA_LEDGER_PATH", str(db_file))

    # Create dummy config
    cfg_file = tmp_path / "sites.json"
    cfg_data = {
        "schema_version": 2,
        "sites": {
            "example-site": {
                "label": "Example Site",
                "environment": "production",
                "adapter": "novamira_mcp",
                "credential_pointer": "test_ptr",
                "recovery_pointer": "test_rec",
                "public_url": "https://example-main.com",
                "portal_plan_id": "TEFud1ozd0pJUT09",
                "novamira_server": "test_server",
            }
        },
        "portal_accounts": {
            "primary": {
                "label": "Primary Portal",
                "adapter": "opencli",
                "opencli_path": "/bin/echo",
                "opencli_profile": "Default",
                "credential_pointer": "ptr",
                "expected_domains": ["example-main.com"],
            }
        },
    }
    cfg_file.write_text(json.dumps(cfg_data))

    # Mock emails file
    emails_file = tmp_path / "emails.json"
    emails_data = [
        {
            "id": 8801,
            "sender_email": "noreply@siteground.com",
            "sender_name": "SiteGround",
            "subject": "example-main.com Hosting Plan Reached Allowed Monthly CPU Seconds",
            "date": "2026-09-13 10:00:00",
            "timestamp": 1789250000,
            "body": "Your Hosting Plan 6 251128 plan CPU exceeded. Stats: https://my.siteground.com/services/hosting/TEFud1ozd0pJUT09/statistics",
        }
    ]
    emails_file.write_text(json.dumps(emails_data))

    # 1. Test quota intake via CLI
    code = main([
        "--config", str(cfg_file),
        "quota", "intake",
        "--file", str(emails_file),
    ])
    assert code == 0
    rec = json.loads(capsys.readouterr().out)
    assert rec["ok"] is True
    assert rec["operation"] == "quota-intake"
    assert rec["evidence"]["alerts_found"] == 1
    assert rec["evidence"]["alerts_recorded"] == 1
    assert "TEFud1ozd0pJUT09" in rec["evidence"]["matched_plans"]

    # 2. Test quota ledger list
    code = main([
        "--config", str(cfg_file),
        "quota", "ledger", "list",
    ])
    assert code == 0
    list_rec = json.loads(capsys.readouterr().out)
    assert list_rec["ok"] is True
    assert list_rec["evidence"]["alerts_count"] == 1
    alert_id = list_rec["evidence"]["alerts"][0]["id"]

    # 3. Test quota ledger show
    code = main([
        "--config", str(cfg_file),
        "quota", "ledger", "show",
        "--id", str(alert_id),
    ])
    assert code == 0
    show_rec = json.loads(capsys.readouterr().out)
    assert show_rec["ok"] is True
    assert show_rec["evidence"]["message_id"] == 8801
    assert show_rec["evidence"]["plan_id"] == "TEFud1ozd0pJUT09"

    # 4. Test quota ledger show missing ID (fail-closed exit code 2)
    code = main([
        "--config", str(cfg_file),
        "quota", "ledger", "show",
    ])
    assert code == 2
    err_rec = json.loads(capsys.readouterr().out)
    assert err_rec["ok"] is False
    assert err_rec["diagnostics"]["code"] == "missing_alert_id"
