from __future__ import annotations

import sqlite3
from pathlib import Path
import pytest

from siteground_ops.quota_ledger import (
    QuotaAlertRecord,
    get_ledger_connection,
    get_quota_alert_by_id,
    get_quota_alerts,
    get_remediation_records,
    get_triage_snapshots,
    record_quota_alert,
    record_remediation,
    record_triage_snapshot,
)


def test_ledger_schema_init_and_idempotency(tmp_path: Path) -> None:
    db_file = tmp_path / "ledger_test.db"
    conn = get_ledger_connection(db_file)
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {r[0] for r in cur.fetchall()}
    assert "quota_alerts" in tables
    assert "triage_snapshots" in tables
    assert "remediation_records" in tables
    conn.close()

    # Reconnect to verify idempotency (no duplicate table errors)
    conn2 = get_ledger_connection(db_file)
    conn2.close()


def test_record_and_retrieve_quota_alerts(tmp_path: Path) -> None:
    db_file = tmp_path / "alerts.db"
    alert1 = QuotaAlertRecord(
        message_id=1001,
        sender="noreply@siteground.com",
        subject="Important: Your hosting plan is now limited. 100% of your monthly CPU has been used.",
        date_received="2026-09-13 12:00:00",
        timestamp=1789257600,
        alert_type="cpu_seconds",
        severity="exhausted",
        plan_id="TEFud1ozd0pJUT09",
        plan_name="Hosting Plan 6 251128",
        stats_url="https://my.siteground.com/services/hosting/TEFud1ozd0pJUT09/statistics",
        domains=["example-main.com", "example-shop.com"],
        body_snippet="CPU usage reached 100%",
    )
    rec_id1 = record_quota_alert(alert1, db_path=db_file)
    assert rec_id1 > 0

    # Retrieve by ID
    loaded = get_quota_alert_by_id(rec_id1, db_path=db_file)
    assert loaded is not None
    assert loaded.message_id == 1001
    assert loaded.alert_type == "cpu_seconds"
    assert loaded.severity == "exhausted"
    assert loaded.plan_id == "TEFud1ozd0pJUT09"
    assert loaded.domains == ["example-main.com", "example-shop.com"]
    assert loaded.status == "ingested"

    # Insert second alert
    alert2 = QuotaAlertRecord(
        message_id=1002,
        sender="noreply@siteground.com",
        subject="Your Hosting Plan 3 plan reached 90% of allowed Inode quota",
        date_received="2026-09-13 13:00:00",
        timestamp=1789261200,
        alert_type="inode_quota",
        severity="critical",
        plan_id="TFEvK1ozb1BKUT09",
        plan_name="Hosting Plan 3",
        stats_url="https://my.siteground.com/services/hosting/TFEvK1ozb1BKUT09/statistics",
        domains=["example-studio.com"],
        body_snippet="90% inodes consumed",
    )
    rec_id2 = record_quota_alert(alert2, db_path=db_file)
    assert rec_id2 > 0

    # Query with filters
    all_alerts = get_quota_alerts(db_path=db_file)
    assert len(all_alerts) == 2

    cpu_alerts = get_quota_alerts(db_path=db_file, plan_id="TEFud1ozd0pJUT09")
    assert len(cpu_alerts) == 1
    assert cpu_alerts[0].message_id == 1001

    # Conflict update on same message_id
    alert1_updated = QuotaAlertRecord(
        message_id=1001,
        sender="noreply@siteground.com",
        subject="Updated Subject",
        date_received="2026-09-13 12:00:00",
        timestamp=1789257600,
        alert_type="cpu_seconds",
        severity="exhausted",
        plan_id="TEFud1ozd0pJUT09",
        domains=["example-main.com", "example-shop.com", "example-studio.com"],
        body_snippet="Updated snippet",
    )
    rec_id_up = record_quota_alert(alert1_updated, db_path=db_file)
    assert rec_id_up == rec_id1
    reloaded = get_quota_alert_by_id(rec_id1, db_path=db_file)
    assert reloaded is not None
    assert len(reloaded.domains) == 3


def test_triage_snapshot_lifecycle(tmp_path: Path) -> None:
    db_file = tmp_path / "triage.db"
    alert = QuotaAlertRecord(
        message_id=5001,
        sender="noreply@siteground.com",
        subject="Alert Subject",
        date_received="2026-09-13 10:00:00",
        timestamp=1789250000,
        alert_type="inode_quota",
        severity="critical",
        plan_id="TEST_PLAN_1",
    )
    alert_id = record_quota_alert(alert, db_path=db_file)

    triage_id = record_triage_snapshot(
        plan_id="TEST_PLAN_1",
        overall_severity="critical",
        total_inodes_used=540000,
        total_inodes_limit=600000,
        total_web_space_gb=20.5,
        reclaimable_inodes=160000,
        immediate_action_needed=True,
        culprits=[{"category": "INODES_ORPHANED", "site_id": "example-main"}],
        actions=[{"action_type": "automated", "category": "INODES_RECLAMATION"}],
        raw_report={"status": "ok"},
        alert_id=alert_id,
        db_path=db_file,
    )
    assert triage_id > 0

    # Verify alert status transitioned to 'triaged'
    updated_alert = get_quota_alert_by_id(alert_id, db_path=db_file)
    assert updated_alert is not None
    assert updated_alert.status == "triaged"

    # Query snapshots
    snaps = get_triage_snapshots(plan_id="TEST_PLAN_1", db_path=db_file)
    assert len(snaps) == 1
    assert snaps[0]["reclaimable_inodes"] == 160000
    assert snaps[0]["immediate_action_needed"] is True
    assert len(snaps[0]["culprits"]) == 1


def test_remediation_records(tmp_path: Path) -> None:
    db_file = tmp_path / "remediation.db"
    rem_id = record_remediation(
        site_id="example-main-production",
        target_dir="upgrade-temp-backup",
        dry_run=True,
        inodes_reclaimed=19526,
        disk_reclaimed_mb=340.5,
        status="dry_run",
        recovery_receipt="audit-receipt-001",
        command_executed="find wp-content/upgrade-temp-backup -delete",
        output_json={"verified": True},
        db_path=db_file,
    )
    assert rem_id > 0

    records = get_remediation_records(site_id="example-main-production", db_path=db_file)
    assert len(records) == 1
    assert records[0]["inodes_reclaimed"] == 19526
    assert records[0]["dry_run"] is True
    assert records[0]["recovery_receipt"] == "audit-receipt-001"


def test_ledger_deceptive_and_edge_paths(tmp_path: Path) -> None:
    db_file = tmp_path / "edge.db"
    # Query non-existent alert
    assert get_quota_alert_by_id(9999, db_path=db_file) is None

    # Empty table queries
    assert get_quota_alerts(db_path=db_file) == []
    assert get_triage_snapshots(db_path=db_file) == []
    assert get_remediation_records(db_path=db_file) == []
