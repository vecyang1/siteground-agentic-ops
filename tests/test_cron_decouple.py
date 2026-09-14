from __future__ import annotations

from typing import Any
import pytest

from siteground_ops.config import SiteConfig
from siteground_ops.cron_decouple import (
    decouple_cron,
    inspect_cron,
    rollback_cron,
)
from siteground_ops.runner import RunnerError


def test_inspect_cron_novamira(monkeypatch: pytest.MonkeyPatch) -> None:
    site = SiteConfig(
        site_id="test-novamira-cron",
        label="Test Site",
        public_url="https://test.example.com",
        environment="staging",
        adapter="novamira_mcp",
        credential_pointer="pointer",
        recovery_pointer="recovery",
    )

    class MockNovamiraRunner:
        def _execute_php(self, code: str) -> dict[str, Any]:
            assert "DISABLE_WP_CRON" in code
            return {
                "home_url": "https://test.example.com",
                "site_id": "test-novamira-cron",
                "config_exists": True,
                "config_writable": True,
                "disable_wp_cron_defined": False,
                "disable_wp_cron_value": None,
                "cron_runner_http_status": 200,
                "cron_runner_latency_ms": 45.2,
                "cron_events_count": 25,
                "decoupled": False,
                "transport": "novamira",
            }

    monkeypatch.setattr("siteground_ops.cron_decouple.build_novamira_runner", lambda _s: MockNovamiraRunner())

    res = inspect_cron(site)
    assert res["site_id"] == "test-novamira-cron"
    assert res["decoupled"] is False
    assert res["cron_runner_http_status"] == 200
    assert res["cron_events_count"] == 25


def test_decouple_cron_preflight_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    site = SiteConfig(
        site_id="test-preflight-fail",
        label="Test Site",
        public_url="https://test.example.com",
        environment="staging",
        adapter="novamira_mcp",
        credential_pointer="pointer",
        recovery_pointer="recovery",
    )

    class MockNovamiraRunner:
        def _execute_php(self, code: str) -> dict[str, Any]:
            return {
                "home_url": "https://test.example.com",
                "config_exists": True,
                "config_writable": True,
                "cron_runner_http_status": 500,  # broken runner!
                "decoupled": False,
            }

    monkeypatch.setattr("siteground_ops.cron_decouple.build_novamira_runner", lambda _s: MockNovamiraRunner())

    with pytest.raises(RunnerError, match="Preflight check failed"):
        decouple_cron(site, dry_run=False)


def test_decouple_cron_novamira_dry_run_and_execute(monkeypatch: pytest.MonkeyPatch) -> None:
    site = SiteConfig(
        site_id="test-novamira-decouple",
        label="Test Site",
        public_url="https://test.example.com",
        environment="staging",
        adapter="novamira_mcp",
        credential_pointer="pointer",
        recovery_pointer="recovery",
    )

    class MockNovamiraRunner:
        def _execute_php(self, code: str) -> dict[str, Any]:
            if "DISABLE_WP_CRON" in code and "cron_runner_http_status" in code:
                # inspect call
                return {
                    "home_url": "https://test.example.com",
                    "config_exists": True,
                    "config_writable": True,
                    "disable_wp_cron_defined": False,
                    "disable_wp_cron_value": None,
                    "cron_runner_http_status": 200,
                    "cron_events_count": 10,
                    "decoupled": False,
                }
            if "backup_name" in code:
                # decouple call
                return {
                    "home_url": "https://test.example.com",
                    "success": True,
                    "backup_name": "wp-config.php.bak.20260914_110000",
                    "http_code": 200,
                    "decoupled": True,
                }
            if "restored_from" in code:
                # rollback call
                return {
                    "home_url": "https://test.example.com",
                    "restored_from": "wp-config.php.bak.20260914_110000",
                    "success": True,
                }
            return {"home_url": "https://test.example.com"}

    monkeypatch.setattr("siteground_ops.cron_decouple.build_novamira_runner", lambda _s: MockNovamiraRunner())

    # Dry-run
    dry_res = decouple_cron(site, dry_run=True)
    assert dry_res["dry_run"] is True
    assert dry_res["already_decoupled"] is False
    assert dry_res["mutation_applied"] is False
    assert "DISABLE_WP_CRON" in dry_res["proposed_statement"]

    # Execute
    exec_res = decouple_cron(site, dry_run=False)
    assert exec_res["dry_run"] is False
    assert exec_res["mutation_applied"] is True
    assert exec_res["backup_path"] == "wp-config.php.bak.20260914_110000"

    # Rollback
    rb_res = rollback_cron(site, dry_run=False)
    assert rb_res["mutation_applied"] is True
    assert rb_res["restored_from"] == "wp-config.php.bak.20260914_110000"
