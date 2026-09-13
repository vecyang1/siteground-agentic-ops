from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


DEFAULT_LEDGER_DB_PATH = Path.home() / ".config" / "siteground-ops" / "quota_ledger.db"


def get_ledger_db_path(custom_path: Path | str | None = None) -> Path:
    """Resolve ledger SQLite database path with env var and custom override support."""
    if custom_path:
        p = Path(custom_path).expanduser().resolve()
    else:
        env_val = os.environ.get("OPS_QUOTA_LEDGER_PATH")
        p = Path(env_val).expanduser().resolve() if env_val else DEFAULT_LEDGER_DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def get_ledger_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Return an open SQLite connection with schema initialized."""
    target = get_ledger_db_path(db_path)
    conn = sqlite3.connect(str(target), timeout=10.0)
    conn.row_factory = sqlite3.Row
    init_ledger_schema(conn)
    return conn


def init_ledger_schema(conn: sqlite3.Connection) -> None:
    """Initialize tables, indices, and constraints for SSOT quota ledger."""
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS quota_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER UNIQUE,
            sender TEXT NOT NULL,
            subject TEXT NOT NULL,
            date_received TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            alert_type TEXT NOT NULL,
            severity TEXT NOT NULL,
            plan_id TEXT,
            plan_name TEXT,
            stats_url TEXT,
            domains_json TEXT NOT NULL DEFAULT '[]',
            body_snippet TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'ingested',
            created_at TEXT NOT NULL
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_alerts_plan_id ON quota_alerts(plan_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_alerts_timestamp ON quota_alerts(timestamp)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_alerts_status ON quota_alerts(status)")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS triage_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id TEXT NOT NULL,
            alert_id INTEGER REFERENCES quota_alerts(id),
            observed_at TEXT NOT NULL,
            overall_severity TEXT NOT NULL,
            total_inodes_used INTEGER NOT NULL DEFAULT 0,
            total_inodes_limit INTEGER NOT NULL DEFAULT 0,
            total_web_space_gb REAL NOT NULL DEFAULT 0.0,
            reclaimable_inodes INTEGER NOT NULL DEFAULT 0,
            immediate_action_needed INTEGER NOT NULL DEFAULT 0,
            culprits_json TEXT NOT NULL DEFAULT '[]',
            actions_json TEXT NOT NULL DEFAULT '[]',
            raw_report_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_triage_plan_id ON triage_snapshots(plan_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_triage_alert_id ON triage_snapshots(alert_id)")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS remediation_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            triage_id INTEGER REFERENCES triage_snapshots(id),
            site_id TEXT NOT NULL,
            target_dir TEXT NOT NULL,
            dry_run INTEGER NOT NULL DEFAULT 1,
            inodes_reclaimed INTEGER NOT NULL DEFAULT 0,
            disk_reclaimed_mb REAL NOT NULL DEFAULT 0.0,
            status TEXT NOT NULL,
            recovery_receipt TEXT NOT NULL DEFAULT '',
            command_executed TEXT NOT NULL DEFAULT '',
            output_json TEXT NOT NULL DEFAULT '{}',
            executed_at TEXT NOT NULL
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_remediation_site_id ON remediation_records(site_id)")
    conn.commit()


@dataclass
class QuotaAlertRecord:
    message_id: int
    sender: str
    subject: str
    date_received: str
    timestamp: int
    alert_type: str
    severity: str
    plan_id: Optional[str] = None
    plan_name: Optional[str] = None
    stats_url: Optional[str] = None
    domains: list[str] = field(default_factory=list)
    body_snippet: str = ""
    status: str = "ingested"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    id: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "message_id": self.message_id,
            "sender": self.sender,
            "subject": self.subject,
            "date_received": self.date_received,
            "timestamp": self.timestamp,
            "alert_type": self.alert_type,
            "severity": self.severity,
            "plan_id": self.plan_id,
            "plan_name": self.plan_name,
            "stats_url": self.stats_url,
            "domains": self.domains,
            "body_snippet": self.body_snippet,
            "status": self.status,
            "created_at": self.created_at,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> QuotaAlertRecord:
        try:
            domains = json.loads(row["domains_json"]) if row["domains_json"] else []
        except Exception:
            domains = []
        return cls(
            id=row["id"],
            message_id=row["message_id"],
            sender=row["sender"],
            subject=row["subject"],
            date_received=row["date_received"],
            timestamp=row["timestamp"],
            alert_type=row["alert_type"],
            severity=row["severity"],
            plan_id=row["plan_id"],
            plan_name=row["plan_name"],
            stats_url=row["stats_url"],
            domains=domains,
            body_snippet=row["body_snippet"],
            status=row["status"],
            created_at=row["created_at"],
        )


def record_quota_alert(
    alert: QuotaAlertRecord,
    db_path: Path | str | None = None,
) -> int:
    """Insert or update a quota alert record in the ledger."""
    conn = get_ledger_connection(db_path)
    try:
        cur = conn.cursor()
        domains_str = json.dumps(alert.domains, ensure_ascii=False)
        cur.execute(
            """
            INSERT INTO quota_alerts (
                message_id, sender, subject, date_received, timestamp,
                alert_type, severity, plan_id, plan_name, stats_url,
                domains_json, body_snippet, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
                alert_type = excluded.alert_type,
                severity = excluded.severity,
                plan_id = COALESCE(excluded.plan_id, quota_alerts.plan_id),
                plan_name = COALESCE(excluded.plan_name, quota_alerts.plan_name),
                stats_url = COALESCE(excluded.stats_url, quota_alerts.stats_url),
                domains_json = excluded.domains_json,
                body_snippet = excluded.body_snippet
            """,
            (
                alert.message_id,
                alert.sender,
                alert.subject,
                alert.date_received,
                alert.timestamp,
                alert.alert_type,
                alert.severity,
                alert.plan_id,
                alert.plan_name,
                alert.stats_url,
                domains_str,
                alert.body_snippet,
                alert.status,
                alert.created_at,
            ),
        )
        conn.commit()
        cur.execute("SELECT id FROM quota_alerts WHERE message_id = ?", (alert.message_id,))
        row = cur.fetchone()
        return int(row["id"]) if row else -1
    finally:
        conn.close()


def get_quota_alerts(
    db_path: Path | str | None = None,
    limit: int = 50,
    status: Optional[str] = None,
    plan_id: Optional[str] = None,
) -> list[QuotaAlertRecord]:
    """Retrieve ingested alerts from the ledger."""
    conn = get_ledger_connection(db_path)
    try:
        cur = conn.cursor()
        query = "SELECT * FROM quota_alerts"
        params: list[Any] = []
        conditions: list[str] = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if plan_id:
            conditions.append("plan_id = ?")
            params.append(plan_id)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        cur.execute(query, params)
        return [QuotaAlertRecord.from_row(row) for row in cur.fetchall()]
    finally:
        conn.close()


def get_quota_alert_by_id(
    alert_id: int,
    db_path: Path | str | None = None,
) -> Optional[QuotaAlertRecord]:
    """Retrieve a single quota alert by its primary key ID."""
    conn = get_ledger_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM quota_alerts WHERE id = ?", (alert_id,))
        row = cur.fetchone()
        return QuotaAlertRecord.from_row(row) if row else None
    finally:
        conn.close()


def record_triage_snapshot(
    plan_id: str,
    overall_severity: str,
    total_inodes_used: int,
    total_inodes_limit: int,
    total_web_space_gb: float,
    reclaimable_inodes: int,
    immediate_action_needed: bool,
    culprits: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    raw_report: dict[str, Any],
    alert_id: Optional[int] = None,
    observed_at: Optional[str] = None,
    db_path: Path | str | None = None,
) -> int:
    """Record a triage execution snapshot in the ledger."""
    conn = get_ledger_connection(db_path)
    try:
        cur = conn.cursor()
        now_iso = datetime.now(timezone.utc).isoformat()
        obs_time = observed_at or now_iso
        cur.execute(
            """
            INSERT INTO triage_snapshots (
                plan_id, alert_id, observed_at, overall_severity,
                total_inodes_used, total_inodes_limit, total_web_space_gb,
                reclaimable_inodes, immediate_action_needed,
                culprits_json, actions_json, raw_report_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan_id,
                alert_id,
                obs_time,
                overall_severity,
                total_inodes_used,
                total_inodes_limit,
                total_web_space_gb,
                reclaimable_inodes,
                1 if immediate_action_needed else 0,
                json.dumps(culprits, ensure_ascii=False),
                json.dumps(actions, ensure_ascii=False),
                json.dumps(raw_report, ensure_ascii=False),
                now_iso,
            ),
        )
        conn.commit()
        last_id = cur.lastrowid
        if alert_id and last_id:
            cur.execute("UPDATE quota_alerts SET status = 'triaged' WHERE id = ?", (alert_id,))
            conn.commit()
        return int(last_id) if last_id else -1
    finally:
        conn.close()


def get_triage_snapshots(
    plan_id: Optional[str] = None,
    limit: int = 20,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Retrieve historical triage snapshots."""
    conn = get_ledger_connection(db_path)
    try:
        cur = conn.cursor()
        query = "SELECT * FROM triage_snapshots"
        params: list[Any] = []
        if plan_id:
            query += " WHERE plan_id = ?"
            params.append(plan_id)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        cur.execute(query, params)
        rows = cur.fetchall()
        results: list[dict[str, Any]] = []
        for r in rows:
            results.append(
                {
                    "id": r["id"],
                    "plan_id": r["plan_id"],
                    "alert_id": r["alert_id"],
                    "observed_at": r["observed_at"],
                    "overall_severity": r["overall_severity"],
                    "total_inodes_used": r["total_inodes_used"],
                    "total_inodes_limit": r["total_inodes_limit"],
                    "total_web_space_gb": r["total_web_space_gb"],
                    "reclaimable_inodes": r["reclaimable_inodes"],
                    "immediate_action_needed": bool(r["immediate_action_needed"]),
                    "culprits": json.loads(r["culprits_json"]) if r["culprits_json"] else [],
                    "actions": json.loads(r["actions_json"]) if r["actions_json"] else [],
                    "created_at": r["created_at"],
                }
            )
        return results
    finally:
        conn.close()


def record_remediation(
    site_id: str,
    target_dir: str,
    dry_run: bool,
    inodes_reclaimed: int,
    disk_reclaimed_mb: float,
    status: str,
    recovery_receipt: str = "",
    command_executed: str = "",
    output_json: Optional[dict[str, Any]] = None,
    triage_id: Optional[int] = None,
    db_path: Path | str | None = None,
) -> int:
    """Record remediation execution into ledger."""
    conn = get_ledger_connection(db_path)
    try:
        cur = conn.cursor()
        now_iso = datetime.now(timezone.utc).isoformat()
        cur.execute(
            """
            INSERT INTO remediation_records (
                triage_id, site_id, target_dir, dry_run,
                inodes_reclaimed, disk_reclaimed_mb, status,
                recovery_receipt, command_executed, output_json, executed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                triage_id,
                site_id,
                target_dir,
                1 if dry_run else 0,
                inodes_reclaimed,
                disk_reclaimed_mb,
                status,
                recovery_receipt,
                command_executed,
                json.dumps(output_json or {}, ensure_ascii=False),
                now_iso,
            ),
        )
        conn.commit()
        return int(cur.lastrowid) if cur.lastrowid else -1
    finally:
        conn.close()


def get_remediation_records(
    site_id: Optional[str] = None,
    limit: int = 50,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Retrieve historical remediation records."""
    conn = get_ledger_connection(db_path)
    try:
        cur = conn.cursor()
        query = "SELECT * FROM remediation_records"
        params: list[Any] = []
        if site_id:
            query += " WHERE site_id = ?"
            params.append(site_id)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        cur.execute(query, params)
        rows = cur.fetchall()
        results: list[dict[str, Any]] = []
        for r in rows:
            results.append(
                {
                    "id": r["id"],
                    "triage_id": r["triage_id"],
                    "site_id": r["site_id"],
                    "target_dir": r["target_dir"],
                    "dry_run": bool(r["dry_run"]),
                    "inodes_reclaimed": r["inodes_reclaimed"],
                    "disk_reclaimed_mb": r["disk_reclaimed_mb"],
                    "status": r["status"],
                    "recovery_receipt": r["recovery_receipt"],
                    "command_executed": r["command_executed"],
                    "output": json.loads(r["output_json"]) if r["output_json"] else {},
                    "executed_at": r["executed_at"],
                }
            )
        return results
    finally:
        conn.close()
