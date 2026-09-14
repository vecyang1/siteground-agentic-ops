from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from siteground_ops.config import SiteConfig, OpsConfig
from siteground_ops.quota import (
    DEFAULT_HOURLY_EXECUTION_LIMIT,
    DEFAULT_INODES_LIMIT,
    DEFAULT_WEB_SPACE_GB_LIMIT,
    PlanQuotaSnapshot,
    QuotaMetric,
    QuotaSeverity,
    QuotaStore,
    QuotaTriageEngine,
    SiteDeepDiagnosis,
    SiteQuotaShare,
    evaluate_metric_severity,
    probe_site_deep,
)


def test_evaluate_metric_severity() -> None:
    # Under warning threshold -> HEALTHY
    assert evaluate_metric_severity(400_000, 600_000) == QuotaSeverity.HEALTHY
    # At or above 80% -> WARNING
    assert evaluate_metric_severity(480_000, 600_000) == QuotaSeverity.WARNING
    assert evaluate_metric_severity(500_000, 600_000) == QuotaSeverity.WARNING
    # At or above 90% -> CRITICAL
    assert evaluate_metric_severity(542_722, 600_000) == QuotaSeverity.CRITICAL
    # At or above 100% -> EXHAUSTED
    assert evaluate_metric_severity(600_000, 600_000) == QuotaSeverity.EXHAUSTED
    assert evaluate_metric_severity(610_000, 600_000) == QuotaSeverity.EXHAUSTED


def test_plan_quota_snapshot_metrics() -> None:
    snapshot = PlanQuotaSnapshot(
        plan_id="TEFud1ozd0pJUT09",
        plan_name="Hosting Plan 6 251128 (GoGeek)",
        observed_at="2026-09-13T23:00:00Z",
        web_space_limit_gb=100.0,
        web_space_used_gb=18.9,
        inodes_limit=600_000,
        inodes_used=542_722,
        hourly_execution_limit=4_000,
        hourly_execution_peak=15_899,
        cpu_seconds_alert=True,
        site_shares={
            "example-main.com": SiteQuotaShare("example-main.com", 10.28, 257_863),
            "example-shop.com": SiteQuotaShare("example-shop.com", 4.30, 110_015),
            "example-studio.com": SiteQuotaShare("example-studio.com", 2.95, 100_768),
            "vectory44.sg-host.com": SiteQuotaShare("vectory44.sg-host.com", 0.785, 39_885),
        },
    )

    metrics = snapshot.metrics
    assert metrics["inodes"].severity == QuotaSeverity.CRITICAL
    assert metrics["inodes"].used_percent == pytest.approx(90.45, rel=1e-2)
    assert metrics["web_space"].severity == QuotaSeverity.HEALTHY
    # Peak of 15,899 on a 4,000/hr limit is > 100%, hence EXHAUSTED
    assert metrics["program_executions"].severity == QuotaSeverity.EXHAUSTED
    assert metrics["cpu_seconds"].severity == QuotaSeverity.CRITICAL
    assert snapshot.overall_severity == QuotaSeverity.EXHAUSTED

    # Check top inode share site
    top_sites = snapshot.top_sites_by_inodes()
    assert top_sites[0].domain == "example-main.com"
    assert top_sites[0].inodes_percent_of_plan(600_000) == pytest.approx(42.97, rel=1e-2)


def test_quota_store_save_and_load(tmp_path: Path) -> None:
    store = QuotaStore(directory=tmp_path)
    snapshot = PlanQuotaSnapshot(
        plan_id="TEFud1ozd0pJUT09",
        plan_name="Hosting Plan 6 251128 (GoGeek)",
        observed_at="2026-09-13T23:00:00Z",
        web_space_limit_gb=100.0,
        web_space_used_gb=18.9,
        inodes_limit=600_000,
        inodes_used=542_722,
        hourly_execution_limit=4_000,
        hourly_execution_peak=15_899,
        cpu_seconds_alert=True,
        site_shares={
            "example-main.com": SiteQuotaShare("example-main.com", 10.28, 257_863),
            "example-shop.com": SiteQuotaShare("example-shop.com", 4.30, 110_015),
        },
    )
    store.save_snapshot(snapshot)

    loaded = store.load_snapshot("TEFud1ozd0pJUT09")
    assert loaded is not None
    assert loaded.plan_id == "TEFud1ozd0pJUT09"
    assert loaded.inodes_used == 542_722
    assert loaded.cpu_seconds_alert is True
    assert "example-main.com" in loaded.site_shares
    assert loaded.site_shares["example-main.com"].inodes_count == 257_863


def test_probe_site_deep_novamira(monkeypatch: pytest.MonkeyPatch) -> None:
    site = SiteConfig(
        site_id="example-shop-production",
        label="Example Shop",
        environment="production",
        adapter="novamira_mcp",
        credential_pointer="Pointer",
        recovery_pointer="Recovery",
        public_url="https://example-shop.com",
        novamira_server="novamira-example-shop",
    )

    mock_runner = MagicMock()
    mock_runner._execute_php.return_value = {
        "home_url": "https://example-shop.com",
        "content_dir": "/home/customer/www/example-shop.com/public_html/wp-content",
        "stats": {
            "cache": 67,
            "uploads": 4975,
            "plugins": 62787,
            "themes": 2018,
            "languages": 0,
            "upgrade": 0,
        },
        "disable_wp_cron": False,
        "alternate_wp_cron": False,
        "autoload_bytes": 450_000,
        "transients_count": 120,
        "cron_events_count": 60,
        "optimizer_active": True,
        "dynamic_cache": False,
        "file_caching": True,
        "active_plugins_count": 37,
    }

    monkeypatch.setattr("siteground_ops.quota.build_novamira_runner", lambda _site: mock_runner)
    monkeypatch.setattr(
        "siteground_ops.quota.probe_public_cache_headers",
        lambda _url: {"ok": True, "x_proxy_cache": "HIT", "cache_control": None},
    )

    diagnosis = probe_site_deep(site)
    assert diagnosis.site_id == "example-shop-production"
    assert diagnosis.transport == "novamira"
    assert diagnosis.virtual_cron_enabled is True
    assert diagnosis.dynamic_cache_enabled is False
    assert diagnosis.inode_counts["plugins"] == 62787

    # High risk factors identified
    risk_titles = [f.title for f in diagnosis.findings]
    assert any("Virtual WP-Cron" in t for t in risk_titles)
    assert any("Dynamic Cache Disabled" in t for t in risk_titles)
    assert any("Plugin Directory Inode" in t for t in risk_titles)


def test_probe_site_deep_ssh(monkeypatch: pytest.MonkeyPatch) -> None:
    site = SiteConfig(
        site_id="example-main-production",
        label="World Inspire Lab",
        environment="production",
        adapter="paramiko_wpcli",
        credential_pointer="Pointer",
        recovery_pointer="Recovery",
        public_url="https://example-main.com",
        env_file=Path("/path/to/.env"),
        key_file=Path("/path/to/key"),
        remote_path="~/www/example-main.com/public_html",
    )

    mock_client = MagicMock()
    
    def fake_exec_command(cmd: str, timeout: int = 30) -> tuple[Any, Any, Any]:
        mock_stdout = MagicMock()
        if "wp-content/*" in cmd:
            mock_stdout.read.return_value = b"wp-content/plugins:79264\nwp-content/wp-staging:21681\nwp-content/uploads:13449\nwp-content/upgrade-temp-backup:4269\n"
        elif "for d in ~/www/*" in cmd or "staging" in cmd:
            mock_stdout.read.return_value = b"/home/user/www/staging2.example-main.com: 114820\n/home/user/www/example-main.com: 129613\n"
        elif "DISABLE_WP_CRON" in cmd:
            mock_stdout.read.return_value = b"false\n"
        elif "cron" in cmd:
            mock_stdout.read.return_value = b"45\n"
        elif "autoload" in cmd:
            mock_stdout.read.return_value = b"524288\n"
        elif "transient" in cmd:
            mock_stdout.read.return_value = b"85\n"
        elif "plugin list" in cmd:
            mock_stdout.read.return_value = b"40\n"
        else:
            mock_stdout.read.return_value = b""
        return (MagicMock(), mock_stdout, MagicMock())

    mock_client.exec_command.side_effect = fake_exec_command
    mock_runner = MagicMock()
    mock_runner._connect.return_value = mock_client
    mock_runner.cache_status.return_value = {
        "edge_cache_active": False,
        "edge_headers": {"x_proxy_cache": "MISS", "cache_control": "no-cache"},
        "wordpress_app_status": {"optimizer_active": True, "dynamic_cache": None},
    }

    monkeypatch.setattr("siteground_ops.quota.build_runner", lambda _site: mock_runner)
    monkeypatch.setattr(
        "siteground_ops.quota.probe_public_cache_headers",
        lambda _url: {"ok": True, "x_proxy_cache": "MISS", "cache_control": "no-cache"},
    )

    diagnosis = probe_site_deep(site)
    assert diagnosis.site_id == "example-main-production"
    assert diagnosis.transport == "ssh"
    assert diagnosis.virtual_cron_enabled is True
    assert diagnosis.sibling_staging_inodes.get("staging2.example-main.com") == 114820
    assert diagnosis.inode_counts["wp-staging"] == 21681

    risk_titles = [f.title for f in diagnosis.findings]
    assert any("Sibling Staging Inode Bloat" in t for t in risk_titles)
    assert any("WP-Staging Inode Bloat" in t for t in risk_titles)
    assert any("Virtual WP-Cron" in t for t in risk_titles)
    assert any("Edge Cache Miss" in t for t in risk_titles)


def test_quota_triage_synthesis() -> None:
    snapshot = PlanQuotaSnapshot(
        plan_id="TEFud1ozd0pJUT09",
        plan_name="Hosting Plan 6 251128 (GoGeek)",
        observed_at="2026-09-13T23:00:00Z",
        web_space_limit_gb=100.0,
        web_space_used_gb=18.9,
        inodes_limit=600_000,
        inodes_used=542_722,
        hourly_execution_limit=4_000,
        hourly_execution_peak=15_899,
        cpu_seconds_alert=True,
        site_shares={
            "example-main.com": SiteQuotaShare("example-main.com", 10.28, 257_863),
            "example-shop.com": SiteQuotaShare("example-shop.com", 4.30, 110_015),
        },
    )

    diag_world = SiteDeepDiagnosis(
        site_id="example-main-production",
        home_url="https://example-main.com",
        transport="ssh",
        inode_counts={"plugins": 79264, "wp-staging": 21681, "upgrade-temp-backup": 4269, "uploads": 13449},
        sibling_staging_inodes={"staging2.example-main.com": 114820},
        virtual_cron_enabled=True,
        cron_events_count=45,
        optimizer_active=True,
        dynamic_cache_enabled=True,
        edge_cache_hit=False,
        autoload_bytes=524288,
        transients_count=85,
        active_plugins_count=40,
        findings=[],
    )

    diag_xinchao = SiteDeepDiagnosis(
        site_id="example-shop-production",
        home_url="https://example-shop.com",
        transport="novamira",
        inode_counts={"plugins": 62787, "uploads": 4975, "cache": 67},
        sibling_staging_inodes={},
        virtual_cron_enabled=True,
        cron_events_count=60,
        optimizer_active=True,
        dynamic_cache_enabled=False,
        edge_cache_hit=True,
        autoload_bytes=450000,
        transients_count=120,
        active_plugins_count=37,
        findings=[],
    )

    engine = QuotaTriageEngine()
    report = engine.triage(snapshot, [diag_world, diag_xinchao])

    assert report.plan_id == "TEFud1ozd0pJUT09"
    assert report.overall_severity == QuotaSeverity.EXHAUSTED
    assert report.immediate_action_needed is True

    # Check top culprits
    culprit_types = [c.category for c in report.top_culprits]
    assert "INODES_STAGING" in culprit_types
    assert "CRON_VIRTUAL" in culprit_types
    assert "CACHE_BYPASS" in culprit_types

    # Check safe remediation actions
    actions = report.remediation_actions
    assert len(actions) > 0
    # Top action should reclaim inodes from staging2 (114k inodes!)
    staging_remediation = next(a for a in actions if "staging2.example-main.com" in a.description)
    assert staging_remediation.estimated_inode_savings == 114820

    # Cron action should recommend disabling virtual wp-cron
    cron_remediation = next(a for a in actions if "DISABLE_WP_CRON" in a.command_hint)
    assert cron_remediation is not None


def write_test_config(tmp_path: Path) -> Path:
    config = {
        "schema_version": 2,
        "portal_accounts": {
            "primary-siteground": {
                "label": "Primary",
                "adapter": "opencli",
                "opencli_path": "/path/to/opencli",
                "opencli_profile": "profile",
                "credential_pointer": "Browser pointer",
                "expected_domains": ["test.example.com"],
            }
        },
        "sites": {
            "test-site": {
                "label": "Test Site",
                "environment": "production",
                "adapter": "novamira_mcp",
                "novamira_server": "novamira-test",
                "public_url": "https://test.example.com",
                "portal_account": "primary-siteground",
                "portal_plan_id": "TEFud1ozd0pJUT09",
                "credential_pointer": "Test credential",
                "recovery_pointer": "Test recovery",
            }
        },
    }
    cfg_file = tmp_path / "sites.json"
    cfg_file.write_text(json.dumps(config), encoding="utf-8")
    return cfg_file


def test_cli_quota_record_and_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from siteground_ops.cli import main

    cfg_file = write_test_config(tmp_path)
    store_dir = tmp_path / "quota_telemetry"
    monkeypatch.setattr("siteground_ops.quota.DEFAULT_QUOTA_STORE_DIR", store_dir)

    # 1. Record snapshot
    code = main([
        "--config", str(cfg_file),
        "quota", "record",
        "--plan", "TEFud1ozd0pJUT09",
        "--name", "Test GoGeek Plan",
        "--inodes-used", "542722",
        "--inodes-limit", "600000",
        "--web-space-used-gb", "18.9",
        "--web-space-limit-gb", "100.0",
        "--executions-peak", "15899",
        "--executions-limit", "4000",
        "--cpu-alert",
    ])
    assert code == 0
    rec = json.loads(capsys.readouterr().out)
    assert rec["ok"] is True
    assert rec["operation"] == "quota-record"
    assert rec["evidence"]["plan_id"] == "TEFud1ozd0pJUT09"

    # 2. Check snapshot
    code = main([
        "--config", str(cfg_file),
        "quota", "check",
        "--plan", "TEFud1ozd0pJUT09",
    ])
    assert code == 0
    check_rec = json.loads(capsys.readouterr().out)
    assert check_rec["ok"] is True
    assert check_rec["operation"] == "quota-check"
    assert check_rec["evidence"]["severity"] == QuotaSeverity.EXHAUSTED
    assert check_rec["evidence"]["metrics"]["inodes"]["severity"] == QuotaSeverity.CRITICAL


def test_cli_quota_diagnose(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from siteground_ops.cli import main

    cfg_file = write_test_config(tmp_path)
    
    mock_diag = SiteDeepDiagnosis(
        site_id="test-site",
        home_url="https://test.example.com",
        transport="novamira",
        inode_counts={"plugins": 45000, "uploads": 3200},
        virtual_cron_enabled=True,
        dynamic_cache_enabled=False,
    )
    monkeypatch.setattr("siteground_ops.cli.probe_site_deep", lambda _s: mock_diag)

    code = main([
        "--config", str(cfg_file),
        "quota", "diagnose",
        "test-site",
    ])
    assert code == 0
    diag_rec = json.loads(capsys.readouterr().out)
    assert diag_rec["ok"] is True
    assert diag_rec["operation"] == "quota-diagnose"
    assert diag_rec["evidence"]["virtual_cron_enabled"] is True
    assert diag_rec["evidence"]["dynamic_cache_enabled"] is False


def test_cli_quota_triage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from siteground_ops.cli import main

    cfg_file = write_test_config(tmp_path)
    store_dir = tmp_path / "quota_telemetry"
    monkeypatch.setattr("siteground_ops.quota.DEFAULT_QUOTA_STORE_DIR", store_dir)

    # Pre-record snapshot
    snap = PlanQuotaSnapshot(
        plan_id="TEFud1ozd0pJUT09",
        plan_name="Test Plan",
        observed_at="2026-09-13T23:00:00Z",
        inodes_used=542722,
        inodes_limit=600000,
        cpu_seconds_alert=True,
        hourly_execution_peak=15899,
        hourly_execution_limit=4000,
    )
    QuotaStore(directory=store_dir).save_snapshot(snap)

    mock_diag = SiteDeepDiagnosis(
        site_id="test-site",
        home_url="https://test.example.com",
        transport="novamira",
        inode_counts={"plugins": 45000, "wp-staging": 18000},
        virtual_cron_enabled=True,
        dynamic_cache_enabled=False,
    )
    monkeypatch.setattr("siteground_ops.cli.probe_site_deep", lambda _s: mock_diag)

    code = main([
        "--config", str(cfg_file),
        "quota", "triage",
        "TEFud1ozd0pJUT09",
    ])
    assert code == 0
    triage_rec = json.loads(capsys.readouterr().out)
    assert triage_rec["ok"] is True
    assert triage_rec["operation"] == "quota-triage"
    assert triage_rec["evidence"]["overall_severity"] == QuotaSeverity.EXHAUSTED
    assert len(triage_rec["evidence"]["top_culprits"]) > 0
    assert len(triage_rec["evidence"]["remediation_actions"]) > 0


def test_quota_store_find_plan_for_domain(tmp_path: Path) -> None:
    store = QuotaStore(directory=tmp_path)
    snapshot = PlanQuotaSnapshot(
        plan_id="TEFud1ozd0pJUT09",
        plan_name="Hosting Plan",
        observed_at="2026-09-13T23:00:00Z",
        site_shares={
            "example-main.com": SiteQuotaShare("example-main.com", 10.28, 257_863),
            "example-shop.com": SiteQuotaShare("example-shop.com", 4.30, 110_015),
        },
    )
    store.save_snapshot(snapshot)

    assert store.find_plan_for_domain("example-shop.com") == "TEFud1ozd0pJUT09"
    assert store.find_plan_for_domain("https://example-shop.com/") == "TEFud1ozd0pJUT09"
    assert store.find_plan_for_domain("unknown.example.org") is None


def test_probe_site_deep_crawler_and_bot_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    site = SiteConfig(
        site_id="example-shop-production",
        label="Shop",
        environment="production",
        adapter="novamira_mcp",
        credential_pointer="Pointer",
        recovery_pointer="Recovery",
        public_url="https://example-shop.com",
        novamira_server="novamira-example-shop-com",
    )

    mock_runner = MagicMock()
    mock_runner._execute_php.return_value = {
        "home_url": "https://example-shop.com",
        "content_dir": "/home/customer/www/example-shop.com/public_html/wp-content",
        "stats": {
            "cache": 78,
            "uploads": 4974,
            "plugins": 62787,
            "themes": 2018,
            "upgrade-temp-backup": 19526,
            "languages": 0,
            "upgrade": 0,
        },
        "plugin_counts": {
            "surecart": 9118,
            "modular-connector": 3659,
            "elementor": 3023,
        },
        "top_cron_hooks": {
            "fluentcrm_scheduled_minute_tasks": 1,
            "action_scheduler_run_queue": 1,
        },
        "disable_wp_cron": False,
        "alternate_wp_cron": False,
        "autoload_bytes": 143947,
        "transients_count": 4,
        "cron_events_count": 60,
        "optimizer_active": True,
        "dynamic_cache": True,
        "file_caching": True,
        "heartbeat_settings": {"post_interval": 120, "dashboard_interval": 60, "frontend_interval": 120},
        "xmlrpc_enabled": True,
        "opcache_inodes": 10454,
        "crawler_traffic": {
            "sample_count": 200,
            "cache_miss_count": 129,
            "cache_hit_count": 35,
            "wp_cron_count": 25,
            "top_bots": {"Amazonbot": 110, "PetalBot": 6},
            "top_facet_urls": {"/shop/?products-order=asc": 6},
        },
        "active_plugins_count": 55,
    }

    monkeypatch.setattr("siteground_ops.quota.build_novamira_runner", lambda _site: mock_runner)
    monkeypatch.setattr(
        "siteground_ops.quota.probe_public_cache_headers",
        lambda _url: {"ok": True, "x_proxy_cache": "HIT", "cache_control": None},
    )

    diag = probe_site_deep(site)
    assert diag.site_id == "example-shop-production"
    assert diag.virtual_cron_enabled is True
    assert diag.dynamic_cache_enabled is True
    assert diag.xmlrpc_enabled is True
    assert diag.opcache_inodes == 10454
    assert diag.plugin_inode_counts["surecart"] == 9118

    cat_map = {f.category: f for f in diag.findings}
    assert "CRAWLER_SCRAPE_SURGE" in cat_map
    assert cat_map["CRAWLER_SCRAPE_SURGE"].severity == QuotaSeverity.CRITICAL
    assert "Amazonbot" in cat_map["CRAWLER_SCRAPE_SURGE"].detail

    assert "INODES_TEMP_BACKUP" in cat_map
    assert cat_map["INODES_TEMP_BACKUP"].severity == QuotaSeverity.CRITICAL

    assert "XMLRPC_ACTIVE" in cat_map
    assert cat_map["XMLRPC_ACTIVE"].severity == QuotaSeverity.WARNING

    assert "INODES_OPCACHE" in cat_map
    assert cat_map["INODES_OPCACHE"].severity == QuotaSeverity.WARNING

    # Triage synthesis with crawler and bot actions
    snap = PlanQuotaSnapshot(
        plan_id="TEFud1ozd0pJUT09",
        plan_name="GoGeek",
        observed_at="2026-09-13T23:00:00Z",
        inodes_used=542722,
        inodes_limit=600000,
        hourly_execution_peak=15899,
        hourly_execution_limit=4000,
        cpu_seconds_alert=True,
        site_shares={"example-shop.com": SiteQuotaShare("example-shop.com", 4.3, 110015)},
    )
    engine = QuotaTriageEngine()
    report = engine.triage(snap, [diag])

    culprit_cats = {c.category for c in report.top_culprits}
    assert "CRAWLER_SCRAPE_SURGE" in culprit_cats
    assert "INODES_TEMP_BACKUP" in culprit_cats
    assert "XMLRPC_ACTIVE" in culprit_cats

    action_cats = {a.category for a in report.remediation_actions}
    assert "BOT_SCRAPER_DEFENSE" in action_cats
    assert "INODES_RECLAMATION" in action_cats
    assert "SECURITY_HARDENING" in action_cats

    temp_backup_action = next(a for a in report.remediation_actions if a.category == "INODES_RECLAMATION" and "upgrade-temp-backup" in a.command_hint)
    assert temp_backup_action.estimated_inode_savings == 19526
    assert temp_backup_action.priority == 1


def test_cli_quota_triage_auto_plan_resolution(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from siteground_ops.cli import main

    # Configuration where test-site has NO portal_plan_id
    config = {
        "schema_version": 2,
        "portal_accounts": {
            "primary-siteground": {
                "label": "Primary",
                "adapter": "opencli",
                "opencli_path": "/path/to/opencli",
                "opencli_profile": "profile",
                "credential_pointer": "Browser pointer",
                "expected_domains": ["test.example.com"],
            }
        },
        "sites": {
            "test-site": {
                "label": "Test Site",
                "environment": "production",
                "adapter": "novamira_mcp",
                "novamira_server": "novamira-test",
                "public_url": "https://test.example.com",
                "portal_account": "primary-siteground",
                "credential_pointer": "Test credential",
                "recovery_pointer": "Test recovery",
            }
        },
    }
    cfg_file = tmp_path / "sites.json"
    cfg_file.write_text(json.dumps(config), encoding="utf-8")

    store_dir = tmp_path / "quota_telemetry"
    monkeypatch.setattr("siteground_ops.quota.DEFAULT_QUOTA_STORE_DIR", store_dir)

    # Pre-record snapshot in store containing test.example.com
    snap = PlanQuotaSnapshot(
        plan_id="RESOLVED_PLAN_123",
        plan_name="Resolved Plan",
        observed_at="2026-09-13T23:00:00Z",
        inodes_used=542722,
        inodes_limit=600000,
        cpu_seconds_alert=True,
        hourly_execution_peak=15899,
        hourly_execution_limit=4000,
        site_shares={"test.example.com": SiteQuotaShare("test.example.com", 4.3, 110015)},
    )
    QuotaStore(directory=store_dir).save_snapshot(snap)

    mock_diag = SiteDeepDiagnosis(
        site_id="test-site",
        home_url="https://test.example.com",
        transport="novamira",
        inode_counts={"plugins": 45000, "upgrade-temp-backup": 12000},
        virtual_cron_enabled=True,
        dynamic_cache_enabled=True,
        crawler_traffic={"top_bots": {"Amazonbot": 50}, "wp_cron_count": 10, "cache_miss_count": 40, "sample_count": 60},
    )
    monkeypatch.setattr("siteground_ops.cli.probe_site_deep", lambda _s: mock_diag)

    # Calling triage on test-site should auto-resolve to RESOLVED_PLAN_123
    code = main([
        "--config", str(cfg_file),
        "quota", "triage",
        "test-site",
    ])
    assert code == 0
    triage_rec = json.loads(capsys.readouterr().out)
    assert triage_rec["ok"] is True
    assert triage_rec["evidence"]["plan_id"] == "RESOLVED_PLAN_123"
    assert triage_rec["evidence"]["plan_metrics"]["inodes"]["value"] == 542722.0
    assert triage_rec["evidence"]["overall_severity"] == QuotaSeverity.EXHAUSTED


def test_cli_quota_triage_explicit_plan_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from siteground_ops.cli import main

    cfg_file = write_test_config(tmp_path)
    store_dir = tmp_path / "quota_telemetry"
    monkeypatch.setattr("siteground_ops.quota.DEFAULT_QUOTA_STORE_DIR", store_dir)

    snap = PlanQuotaSnapshot(
        plan_id="EXPLICIT_PLAN_999",
        plan_name="Explicit Plan",
        observed_at="2026-09-13T23:00:00Z",
        inodes_used=500000,
        inodes_limit=600000,
    )
    QuotaStore(directory=store_dir).save_snapshot(snap)

    mock_diag = SiteDeepDiagnosis(
        site_id="test-site",
        home_url="https://test.example.com",
        transport="novamira",
        inode_counts={"plugins": 10000},
    )
    monkeypatch.setattr("siteground_ops.cli.probe_site_deep", lambda _s: mock_diag)

    code = main([
        "--config", str(cfg_file),
        "quota", "triage",
        "test-site",
        "--plan", "EXPLICIT_PLAN_999",
    ])
    assert code == 0
    triage_rec = json.loads(capsys.readouterr().out)
    assert triage_rec["evidence"]["plan_id"] == "EXPLICIT_PLAN_999"


def test_clean_site_inodes_forbidden_target(tmp_path: Path) -> None:
    from siteground_ops.config import SiteConfig
    from siteground_ops.quota import clean_site_inodes

    site = SiteConfig(
        site_id="test-site",
        label="Test Site",
        public_url="https://test.example.com",
        environment="staging",
        adapter="novamira_mcp",
        credential_pointer="pointer",
        recovery_pointer="recovery",
    )
    with pytest.raises(ValueError, match="not an allowed cleanup target"):
        clean_site_inodes(site, "plugins", dry_run=True)

    with pytest.raises(ValueError, match="not an allowed cleanup target"):
        clean_site_inodes(site, "../../etc", dry_run=True)


def test_clean_site_inodes_novamira_dry_run_and_execute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from siteground_ops.config import SiteConfig
    from siteground_ops.quota import clean_site_inodes

    site = SiteConfig(
        site_id="test-site",
        label="Test Site",
        public_url="https://test.example.com",
        environment="staging",
        adapter="novamira_mcp",
        credential_pointer="pointer",
        recovery_pointer="recovery",
    )

    class MockNovamiraRunner:
        def _execute_php(self, code: str) -> dict[str, Any]:
            assert "WP_CONTENT_DIR" in code
            if "$dry_run = true;" in code:
                return {
                    "home_url": "https://test.example.com",
                    "target_dir": "upgrade-temp-backup",
                    "path": "wp-content/upgrade-temp-backup",
                    "dry_run": True,
                    "observed_files": 15000,
                    "observed_bytes": 100000000,
                    "deleted_files": 0,
                    "deleted_dirs": 0,
                    "errors": [],
                }
            return {
                "home_url": "https://test.example.com",
                "target_dir": "upgrade-temp-backup",
                "path": "wp-content/upgrade-temp-backup",
                "dry_run": False,
                "observed_files": 15000,
                "observed_bytes": 100000000,
                "deleted_files": 15000,
                "deleted_dirs": 120,
                "errors": [],
            }

    monkeypatch.setattr("siteground_ops.quota.build_novamira_runner", lambda _s: MockNovamiraRunner())

    dry_res = clean_site_inodes(site, "upgrade-temp-backup", dry_run=True)
    assert dry_res["dry_run"] is True
    assert dry_res["observed_files"] == 15000
    assert dry_res["deleted_files"] == 0

    exec_res = clean_site_inodes(site, "upgrade-temp-backup", dry_run=False)
    assert exec_res["dry_run"] is False
    assert exec_res["deleted_files"] == 15000


def test_clean_site_inodes_ssh_dry_run_and_execute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io
    from siteground_ops.config import SiteConfig
    from siteground_ops.quota import clean_site_inodes

    site = SiteConfig(
        site_id="ssh-clean-site",
        label="SSH Clean Site",
        public_url="https://ssh.example.com",
        environment="production",
        adapter="paramiko_wpcli",
        credential_pointer="pointer",
        recovery_pointer="recovery",
        remote_path="/home/customer/www/ssh.example.com/public_html",
    )

    class MockChannel:
        def __init__(self, output: str) -> None:
            self._data = io.BytesIO(output.encode("utf-8"))

        def read(self) -> bytes:
            return self._data.read()

    class MockSSHClient:
        def exec_command(self, cmd: str, timeout: int = 15):
            out = "0"
            if "delete" in cmd or "rm -rf" in cmd:
                # after deletion, 0 files remain
                out = "0"
            elif "find 'wp-content/upgrade-temp-backup' -mindepth 1 | wc -l" in cmd:
                out = "4200"
            elif "du -sk" in cmd:
                out = "65000"
            return None, MockChannel(out), MockChannel("")

        def close(self) -> None:
            pass

    class MockRunner:
        def _connect(self):
            return MockSSHClient()

    monkeypatch.setattr("siteground_ops.quota.build_runner", lambda _s: MockRunner())

    dry_res = clean_site_inodes(site, "upgrade-temp-backup", dry_run=True)
    assert dry_res["transport"] == "ssh"
    assert dry_res["dry_run"] is True
    assert dry_res["observed_files"] == 4200
    assert dry_res["deleted_files"] == 0

    exec_res = clean_site_inodes(site, "upgrade-temp-backup", dry_run=False)
    assert exec_res["transport"] == "ssh"
    assert exec_res["dry_run"] is False
    assert exec_res["observed_files"] == 4200
    assert exec_res["deleted_files"] == 4200


def test_cli_quota_clean_confirmation_guard(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from siteground_ops.cli import main

    cfg_file = write_test_config(tmp_path)
    # Missing confirmation
    code = main([
        "--config", str(cfg_file),
        "quota", "clean",
        "test-site",
        "--target-dir", "upgrade-temp-backup",
    ])
    assert code == 2
    rec = json.loads(capsys.readouterr().out)
    assert rec["ok"] is False
    assert rec["mutation_state"] == "refused"
    assert rec["diagnostics"]["code"] == "mutation_confirmation_required"


def test_cli_quota_clean_dry_run_and_execution(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from siteground_ops.cli import main

    cfg_file = write_test_config(tmp_path)

    def mock_clean(_site, target_dir, dry_run=True):
        return {
            "site_id": _site.site_id,
            "home_url": _site.public_url,
            "target_dir": target_dir,
            "path": f"wp-content/{target_dir}",
            "dry_run": dry_run,
            "observed_files": 19526,
            "observed_bytes": 210000000,
            "deleted_files": 0 if dry_run else 19526,
            "deleted_dirs": 0 if dry_run else 100,
            "errors": [],
            "transport": "novamira",
        }

    monkeypatch.setattr("siteground_ops.cli.clean_site_inodes", mock_clean)

    # 1. Dry run
    code = main([
        "--config", str(cfg_file),
        "quota", "clean",
        "test-site",
        "--target-dir", "upgrade-temp-backup",
        "--dry-run",
    ])
    assert code == 0
    rec = json.loads(capsys.readouterr().out)
    assert rec["ok"] is True
    assert rec["mutation_state"] == "not_applicable"
    assert rec["evidence"]["dry_run"] is True
    assert rec["evidence"]["observed_files"] == 19526

    # 2. Execution with confirmation
    code = main([
        "--config", str(cfg_file),
        "quota", "clean",
        "test-site",
        "--target-dir", "upgrade-temp-backup",
        "--confirm-target", "test-site",
        "--recovery-receipt", "operator-approved-test-cleanup",
    ])
    assert code == 0
    rec = json.loads(capsys.readouterr().out)
    assert rec["ok"] is True
    assert rec["mutation_state"] == "applied"
    assert rec["evidence"]["deleted_files"] == 19526
    assert rec["evidence"]["recovery_receipt"] == "operator-approved-test-cleanup"


def test_quota_triage_detects_unprobed_heavy_site_share() -> None:
    from siteground_ops.quota import (
        PlanQuotaSnapshot,
        QuotaSeverity,
        QuotaTriageEngine,
        SiteDeepDiagnosis,
        SiteQuotaShare,
    )

    snap = PlanQuotaSnapshot(
        plan_id="PLAN_MULTI",
        plan_name="GoGeek",
        observed_at="2026-09-13T23:00:00Z",
        inodes_used=540000,
        inodes_limit=600000,
        site_shares={
            "probed.example.com": SiteQuotaShare("probed.example.com", 4.0, 110000),
            "heavy-unprobed.example.com": SiteQuotaShare("heavy-unprobed.example.com", 10.0, 250000),
        },
    )

    diag = SiteDeepDiagnosis(
        site_id="probed-site",
        home_url="https://probed.example.com",
        transport="novamira",
        inode_counts={"plugins": 50000, "upgrade-temp-backup": 15000},
    )

    engine = QuotaTriageEngine()
    report = engine.triage(snap, [diag])

    culprit_categories = [c.category for c in report.top_culprits]
    assert "INODES_SITE_SHARE" in culprit_categories
    heavy_culprit = next(c for c in report.top_culprits if c.category == "INODES_SITE_SHARE")
    assert heavy_culprit.site_id == "heavy-unprobed.example.com"
    assert "250,000 inodes" in heavy_culprit.impact_summary
    assert heavy_culprit.severity == QuotaSeverity.CRITICAL


def test_domains_match_accuracy() -> None:
    from siteground_ops.quota import domains_match

    assert domains_match("example.com", "example.com") is True
    assert domains_match("https://example.com/", "http://example.com") is True
    assert domains_match("staging.example.com", "example.com") is True
    assert domains_match("example.com", "staging.example.com") is True
    assert domains_match("probed.example.com", "heavy-unprobed.example.com") is False
    assert domains_match("alpha.example.com", "beta.example.com") is False
    assert domains_match("", "example.com") is False


def test_cli_quota_triage_expands_to_all_plan_sites(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from siteground_ops.cli import main

    cfg_data = {
        "schema_version": 2,
        "portal_accounts": {
            "primary-siteground": {
                "label": "Test Portal Account",
                "adapter": "opencli",
                "opencli_path": "/path/to/opencli",
                "opencli_profile": "5angnuvf",
                "credential_pointer": "pointer",
                "expected_domains": ["site-a.example.com", "site-b.example.com"],
            }
        },
        "sites": {
            "site-a": {
                "adapter": "novamira_mcp",
                "novamira_server": "novamira-a",
                "environment": "production",
                "label": "Site A",
                "public_url": "https://site-a.example.com",
                "credential_pointer": "pointer",
                "recovery_pointer": "recovery",
                "portal_plan_id": "SHARED_PLAN_1",
            },
            "site-b": {
                "adapter": "novamira_mcp",
                "novamira_server": "novamira-b",
                "environment": "production",
                "label": "Site B",
                "public_url": "https://site-b.example.com",
                "credential_pointer": "pointer",
                "recovery_pointer": "recovery",
                # Note: portal_plan_id not set on site-b, but present in snapshot
            },
        },
    }
    cfg_file = tmp_path / "sites.json"
    cfg_file.write_text(json.dumps(cfg_data), encoding="utf-8")

    store_dir = tmp_path / "quota_telemetry"
    monkeypatch.setattr("siteground_ops.quota.DEFAULT_QUOTA_STORE_DIR", store_dir)

    snap = PlanQuotaSnapshot(
        plan_id="SHARED_PLAN_1",
        plan_name="Shared Plan",
        observed_at="2026-09-13T23:00:00Z",
        inodes_used=400000,
        inodes_limit=600000,
        site_shares={
            "site-a.example.com": SiteQuotaShare("site-a.example.com", 3.0, 100000),
            "site-b.example.com": SiteQuotaShare("site-b.example.com", 8.0, 200000),
        },
    )
    QuotaStore(directory=store_dir).save_snapshot(snap)

    probed_sites: list[str] = []

    def mock_probe(site):
        probed_sites.append(site.site_id)
        return SiteDeepDiagnosis(
            site_id=site.site_id,
            home_url=site.public_url,
            transport="novamira",
            inode_counts={"plugins": 10000},
        )

    monkeypatch.setattr("siteground_ops.cli.probe_site_deep", mock_probe)

    # Calling triage for site-a should probe BOTH site-a AND site-b
    code = main([
        "--config", str(cfg_file),
        "quota", "triage",
        "site-a",
    ])
    assert code == 0
    triage_rec = json.loads(capsys.readouterr().out)
    assert triage_rec["ok"] is True
    assert "site-a" in probed_sites
    assert "site-b" in probed_sites
    assert len(triage_rec["evidence"]["site_diagnostics"]) == 2


def test_probe_site_deep_ssh_telemetry_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io
    from siteground_ops.config import SiteConfig
    from siteground_ops.quota import probe_site_deep

    site = SiteConfig(
        site_id="ssh-test-site",
        label="SSH Test Site",
        public_url="https://ssh.example.com",
        environment="production",
        adapter="paramiko_wpcli",
        credential_pointer="pointer",
        recovery_pointer="recovery",
        remote_path="/home/customer/www/ssh.example.com/public_html",
    )

    class MockChannel:
        def __init__(self, output: str) -> None:
            self._data = io.BytesIO(output.encode("utf-8"))

        def read(self) -> bytes:
            return self._data.read()

    class MockSSHClient:
        def exec_command(self, cmd: str, timeout: int = 15):
            out = ""
            if "DISABLE_WP_CRON" in cmd:
                out = "false"
            elif "count((array) get_option" in cmd:
                out = "42"
            elif "arsort($h)" in cmd:
                out = json.dumps({"action_scheduler_run_queue": 15, "wp_scheduled_delete": 2})
            elif "heartbeat" in cmd:
                out = json.dumps({"post_interval": 120, "dashboard_interval": 60, "frontend_interval": 60})
            elif "logs_dir" in cmd:
                out = (
                    '127.0.0.1 - - [13/Sep/2026:10:00:00 +0000] "GET /shop/?filter=1 HTTP/1.1" 200 1200 "-" "Mozilla/5.0 (compatible; Amazonbot/0.1)" MISS\n'
                    '127.0.0.1 - - [13/Sep/2026:10:00:01 +0000] "POST /wp-cron.php?doing_wp_cron=123 HTTP/1.1" 200 0 "-" "-" MISS\n'
                    '127.0.0.1 - - [13/Sep/2026:10:00:02 +0000] "GET /about HTTP/1.1" 200 5000 "-" "Mozilla/5.0" HIT\n'
                )
            elif "xmlrpc_enabled" in cmd:
                out = "1"
            elif "autoload" in cmd:
                out = "100"
            elif "transient" in cmd:
                out = "5"
            elif "plugin list" in cmd:
                out = "10"
            elif "wp-content/plugins/*" in cmd:
                out = "woocommerce:5000\nsurecart:8000\n"
            elif "wp-content/*" in cmd:
                out = "wp-content/plugins:13000\nwp-content/upgrade-temp-backup:5000\n"
            elif "staging" in cmd:
                out = ""
            elif "opcache" in cmd:
                out = "3500"
            return None, MockChannel(out), MockChannel("")

        def close(self) -> None:
            pass

    class MockRunner:
        def _connect(self):
            return MockSSHClient()

    monkeypatch.setattr("siteground_ops.quota.build_runner", lambda _s: MockRunner())
    monkeypatch.setattr("siteground_ops.quota.probe_public_cache_headers", lambda _u: {"x_proxy_cache": "HIT"})

    diag = probe_site_deep(site)
    assert diag.transport == "ssh"
    assert diag.virtual_cron_enabled is True
    assert diag.top_cron_hooks == {"action_scheduler_run_queue": 15, "wp_scheduled_delete": 2}
    assert diag.heartbeat_settings.get("post_interval") == 120
    assert diag.crawler_traffic["sample_count"] == 3
    assert diag.crawler_traffic["cache_miss_count"] == 2
    assert diag.crawler_traffic["cache_hit_count"] == 1
    assert diag.crawler_traffic["wp_cron_count"] == 1
    assert "Amazonbot" in diag.crawler_traffic["top_bots"]


def test_clean_sibling_staging_validation() -> None:
    from siteground_ops.config import SiteConfig
    from siteground_ops.quota import clean_sibling_staging

    site = SiteConfig(
        site_id="test-ssh",
        label="Test Site",
        public_url="https://main.example.com",
        environment="production",
        adapter="paramiko_wpcli",
        credential_pointer="pointer",
        recovery_pointer="recovery",
    )

    with pytest.raises(ValueError, match="must not contain slashes"):
        clean_sibling_staging(site, "staging/dir", dry_run=True)

    with pytest.raises(ValueError, match="not recognized as a staging directory"):
        clean_sibling_staging(site, "production-clone.example.com", dry_run=True)

    # Test exact collision with production
    site_staging = SiteConfig(
        site_id="test-staging",
        label="Test Staging",
        public_url="https://staging.example.com",
        environment="staging",
        adapter="paramiko_wpcli",
        credential_pointer="pointer",
        recovery_pointer="recovery",
    )
    with pytest.raises(ValueError, match="matches the production site domain"):
        clean_sibling_staging(site_staging, "staging.example.com", dry_run=True)


def test_clean_sibling_staging_ssh(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    from siteground_ops.config import SiteConfig
    from siteground_ops.quota import clean_sibling_staging

    site = SiteConfig(
        site_id="ssh-staging-clean",
        label="SSH Staging Clean Site",
        public_url="https://prod.example.com",
        environment="production",
        adapter="paramiko_wpcli",
        credential_pointer="pointer",
        recovery_pointer="recovery",
    )

    class MockChannel:
        def __init__(self, output: str) -> None:
            self._data = io.BytesIO(output.encode("utf-8"))

        def read(self) -> bytes:
            return self._data.read()

    class MockSSHClient:
        def exec_command(self, cmd: str, timeout: int = 15):
            out = "0"
            if "if [ -d" in cmd:
                out = "1"
            elif "find" in cmd and "wc -l" in cmd:
                out = "114820"
            elif "du -sk" in cmd:
                out = "3000000"
            elif "rm -rf" in cmd:
                out = "1"
            return None, MockChannel(out), MockChannel("")

        def close(self) -> None:
            pass

    class MockRunner:
        def _connect(self):
            return MockSSHClient()

    monkeypatch.setattr("siteground_ops.quota.build_runner", lambda _s: MockRunner())

    # Dry run
    dry_res = clean_sibling_staging(site, "staging2.example.com", dry_run=True)
    assert dry_res["dry_run"] is True
    assert dry_res["observed_files"] == 114820
    assert dry_res["deleted_files"] == 0
    assert dry_res["inodes_reclaimed"] == 0

    # Non-dry run
    exec_res = clean_sibling_staging(site, "staging2.example.com", dry_run=False)
    assert exec_res["dry_run"] is False
    assert exec_res["observed_files"] == 114820
    assert exec_res["deleted_files"] == 114820
    assert exec_res["inodes_reclaimed"] == 114820
    assert exec_res["deleted_dirs"] == 1



