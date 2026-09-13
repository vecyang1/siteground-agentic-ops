from __future__ import annotations

import email
import html
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .config import OpsConfig, SiteConfig
from .quota import QuotaSeverity, domains_match
from .quota_ledger import QuotaAlertRecord, record_quota_alert


def parse_siteground_alert_email(msg: dict[str, Any]) -> Optional[QuotaAlertRecord]:
    """Parse an email dictionary and extract structured quota alert metadata."""
    subject = str(msg.get("subject") or "").strip()
    sender_email = str(msg.get("sender_email") or msg.get("sender") or "").strip().lower()
    sender_name = str(msg.get("sender_name") or "").strip()
    body = str(msg.get("body") or msg.get("summary") or "").strip()
    message_id = int(msg.get("id") or msg.get("message_id") or 0)
    timestamp = int(msg.get("timestamp") or 0)
    date_str = str(msg.get("date") or msg.get("date_received") or "")

    # Verification: must originate from or mention siteground
    is_siteground = (
        "siteground" in sender_email
        or "siteground" in sender_name.lower()
        or "siteground" in subject.lower()
        or "siteground" in body.lower()
    )
    if not is_siteground:
        return None

    alert_type: Optional[str] = None
    severity = QuotaSeverity.WARNING
    plan_name: Optional[str] = None
    plan_id: Optional[str] = None
    stats_url: Optional[str] = None
    domains: list[str] = []

    # 1. Detect Alert Type & Base Severity
    subj_lower = subject.lower()
    body_lower = body.lower()

    if "monthly cpu seconds" in subj_lower or "cpu seconds quota" in body_lower or "monthly cpu has been used" in body_lower:
        alert_type = "cpu_seconds"
        if "100%" in subj_lower or "100%" in body_lower or "limited" in subj_lower:
            severity = QuotaSeverity.EXHAUSTED
        else:
            severity = QuotaSeverity.CRITICAL
    elif "inode quota" in subj_lower or "inode quota" in body_lower or "allowed inode" in body_lower:
        alert_type = "inode_quota"
        pct_match = re.search(r"(\d+)%", subject) or re.search(r"(\d+)%", body)
        pct_val = int(pct_match.group(1)) if pct_match else 90
        if pct_val >= 100:
            severity = QuotaSeverity.EXHAUSTED
        elif pct_val >= 90:
            severity = QuotaSeverity.CRITICAL
        else:
            severity = QuotaSeverity.WARNING
    elif "executions" in subj_lower or "program execution" in body_lower:
        alert_type = "execution_spike"
        severity = QuotaSeverity.CRITICAL
    elif "monthly performance report" in subj_lower:
        alert_type = "performance_report"
        severity = "info"
    elif "resource usage" in subj_lower or "resource usage" in body_lower:
        alert_type = "resource_limit"
        severity = QuotaSeverity.WARNING
    else:
        return None

    # 2. Extract Plan Name
    plan_match = re.search(r"your\s+([A-Za-z0-9\s]+?)\s+hosting plan", body, re.IGNORECASE)
    if not plan_match:
        plan_match = re.search(r"Your\s+([A-Za-z0-9\s]+?)\s+hosting plan", subject, re.IGNORECASE)
    if plan_match:
        plan_name = plan_match.group(1).strip()

    # 3. Extract Statistics and Upgrade URLs (Plan ID)
    stats_match = re.search(r"https://my\.siteground\.com/(?:services/)?hosting/?([A-Za-z0-9+/=]+)/statistics", body)
    if stats_match:
        plan_id = stats_match.group(1).strip()
        stats_url = f"https://my.siteground.com/services/hosting/{plan_id}/statistics"
    else:
        upgrade_match = re.search(r"https://my\.siteground\.com/upgrade/?([A-Za-z0-9+/=]+)", body)
        if upgrade_match:
            plan_id = upgrade_match.group(1).strip()
            stats_url = f"https://my.siteground.com/services/hosting/{plan_id}/statistics"

    # 4. Extract Associated Domains
    # From Subject (e.g. "vectory27.sg-host.com Hosting Plan Reached Allowed Monthly CPU Seconds")
    subj_dom_match = re.match(r"^([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})\s+Hosting Plan", subject, re.IGNORECASE)
    if subj_dom_match:
        domains.append(subj_dom_match.group(1).lower().strip())

    report_dom_match = re.search(r"Monthly performance report for\s+([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", subject, re.IGNORECASE)
    if report_dom_match:
        domains.append(report_dom_match.group(1).lower().strip())

    # From Body (list of affected websites)
    body_affected_match = re.search(
        r"following websites? hosted on [^\n]+ might be affected:\s*([\s\S]+?)(?:Possible solutions|Upgrade to a plan|\n\n\n|$)",
        body,
        re.IGNORECASE,
    )
    if body_affected_match:
        raw_list = body_affected_match.group(1)
        found_doms = re.findall(r"([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", raw_list)
        for d in found_doms:
            d_clean = d.lower().strip()
            if d_clean not in domains and not d_clean.endswith(".com/") and not d_clean.startswith("http"):
                domains.append(d_clean)

    # Snippet for preview
    snippet = body[:250].strip() if body else subject

    return QuotaAlertRecord(
        message_id=message_id,
        sender=sender_email or "noreply@siteground.com",
        subject=subject,
        date_received=date_str,
        timestamp=timestamp,
        alert_type=alert_type,
        severity=severity,
        plan_id=plan_id,
        plan_name=plan_name,
        stats_url=stats_url,
        domains=domains,
        body_snippet=snippet,
    )


def find_default_envelope_index() -> Optional[Path]:
    """Locate macOS Mail.app Envelope Index SQLite database."""
    env_override = os.environ.get("APPLE_MAIL_SQLITE_PATH")
    if env_override:
        p = Path(os.path.expanduser(env_override))
        if p.is_file() and os.access(p, os.R_OK):
            return p

    base_dir = Path.home() / "Library" / "Mail"
    for v in ["V12", "V11", "V10", "V9"]:
        candidate = base_dir / v / "MailData" / "Envelope Index"
        if candidate.is_file() and os.access(candidate, os.R_OK):
            return candidate

    matches = sorted(base_dir.glob("V*/MailData/Envelope Index"), reverse=True)
    for m in matches:
        if m.is_file() and os.access(m, os.R_OK):
            return m

    return None


def extract_emlx_body(emlx_path: Path) -> str:
    """Extract clean text body from an .emlx message file."""
    try:
        with open(emlx_path, "rb") as f:
            first_line = f.readline()
            if first_line.strip().isdigit():
                byte_count = int(first_line.strip())
                raw_bytes = f.read(byte_count)
            else:
                raw_bytes = first_line + f.read()
        msg = email.message_from_bytes(raw_bytes)
        plain_parts: list[str] = []
        html_parts: list[str] = []
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                if ctype == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        plain_parts.append(payload.decode("utf-8", errors="ignore"))
                elif ctype == "text/html":
                    payload = part.get_payload(decode=True)
                    if payload:
                        html_parts.append(payload.decode("utf-8", errors="ignore"))
        else:
            ctype = msg.get_content_type()
            payload = msg.get_payload(decode=True)
            if payload:
                decoded = payload.decode("utf-8", errors="ignore")
                if ctype == "text/html":
                    html_parts.append(decoded)
                else:
                    plain_parts.append(decoded)

        clean_plain = "\n\n".join(p.strip() for p in plain_parts if p and p.strip()).strip()
        if clean_plain:
            return clean_plain
        if html_parts:
            raw_html = "\n\n".join(html_parts)
            raw_html = re.sub(r"<style[^>]*>.*?</style>", " ", raw_html, flags=re.DOTALL | re.IGNORECASE)
            raw_html = re.sub(r"<script[^>]*>.*?</script>", " ", raw_html, flags=re.DOTALL | re.IGNORECASE)
            clean_text = re.sub(r"<[^>]+>", " ", raw_html)
            clean_text = html.unescape(clean_text).replace("\xa0", " ")
            clean_text = re.sub(r"\s+", " ", clean_text).strip()
            return clean_text
    except Exception:
        pass
    return ""


def scan_mail_threads(
    since_days: int = 30,
    limit: int = 50,
    db_path: Optional[Path] = None,
) -> list[QuotaAlertRecord]:
    """Scan local macOS Mail Envelope Index for SiteGround quota alert threads."""
    target_db = db_path or find_default_envelope_index()
    if not target_db or not target_db.is_file():
        return []

    alerts: list[QuotaAlertRecord] = []
    cutoff_ts = int(time.time()) - (since_days * 86400)
    uri = f"file:{target_db.resolve()}?mode=ro"

    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """
            SELECT 
                m.ROWID as id,
                mb.url as mailbox_url,
                a.address as sender_email,
                a.comment as sender_name,
                s.subject as subject,
                sum_t.summary as summary,
                m.date_received as date_received_ts
            FROM messages m
            JOIN mailboxes mb ON m.mailbox = mb.ROWID
            LEFT JOIN addresses a ON m.sender = a.ROWID
            LEFT JOIN subjects s ON m.subject = s.ROWID
            LEFT JOIN summaries sum_t ON m.summary = sum_t.ROWID
            WHERE m.date_received >= ?
              AND (
                  a.address LIKE '%siteground%'
                  OR a.comment LIKE '%SiteGround%'
                  OR s.subject LIKE '%Hosting Plan%'
                  OR s.subject LIKE '%CPU Seconds%'
                  OR s.subject LIKE '%Inode%'
              )
            ORDER BY m.date_received DESC
            LIMIT ?
            """,
            (cutoff_ts, limit),
        )
        rows = cur.fetchall()
        conn.close()

        for row in rows:
            msg_id = row["id"]
            mailbox_url = row["mailbox_url"] or ""
            dt_ts = row["date_received_ts"]
            dt_str = datetime.fromtimestamp(dt_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if dt_ts else ""

            body_text = (row["summary"] or "").strip()
            m_uuid = re.search(r"//([A-Fa-f0-9\-]+)/", mailbox_url)
            uuid = m_uuid.group(1) if m_uuid else ""
            if uuid:
                mail_data_dir = target_db.parent.parent / uuid
                candidates = list(mail_data_dir.glob(f"**/Messages/{msg_id}.emlx"))
                if candidates and candidates[0].is_file():
                    full_body = extract_emlx_body(candidates[0])
                    if full_body:
                        body_text = full_body

            msg_dict = {
                "id": msg_id,
                "sender_email": row["sender_email"] or "",
                "sender_name": row["sender_name"] or "",
                "subject": row["subject"] or "",
                "summary": row["summary"] or "",
                "body": body_text,
                "timestamp": dt_ts,
                "date": dt_str,
            }

            parsed = parse_siteground_alert_email(msg_dict)
            if parsed:
                alerts.append(parsed)

    except Exception:
        pass

    return alerts


def scan_json_messages(messages: list[dict[str, Any]]) -> list[QuotaAlertRecord]:
    """Parse a list of raw message dictionaries into QuotaAlertRecord list."""
    results: list[QuotaAlertRecord] = []
    for msg in messages:
        parsed = parse_siteground_alert_email(msg)
        if parsed:
            results.append(parsed)
    return results


def match_alert_to_config(
    alert: QuotaAlertRecord,
    config: OpsConfig,
) -> tuple[Optional[str], list[str]]:
    """Match an alert to configured hosting plans and sites."""
    matched_sites: list[str] = []
    matched_plan_id: Optional[str] = alert.plan_id

    # 1. Match by portal_plan_id
    if alert.plan_id:
        for site in config.sites.values():
            if site.portal_plan_id == alert.plan_id:
                if site.site_id not in matched_sites:
                    matched_sites.append(site.site_id)

    # 2. Match by domain against public_url
    for alert_dom in alert.domains:
        for site in config.sites.values():
            if site.public_url and domains_match(alert_dom, site.public_url):
                if site.site_id not in matched_sites:
                    matched_sites.append(site.site_id)
                if not matched_plan_id and site.portal_plan_id:
                    matched_plan_id = site.portal_plan_id

        # Also match against portal_accounts expected_domains
        for p_acc_id, p_acc in getattr(config, "portal_accounts", {}).items():
            for exp_dom in getattr(p_acc, "expected_domains", []):
                if domains_match(alert_dom, exp_dom):
                    for site in config.sites.values():
                        if site.portal_account == p_acc_id and site.site_id not in matched_sites:
                            matched_sites.append(site.site_id)

    return matched_plan_id, matched_sites
