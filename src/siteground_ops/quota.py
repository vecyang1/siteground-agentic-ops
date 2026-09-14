from __future__ import annotations

import json
import os
import re
import shlex
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .config import SiteConfig, OpsConfig
from .receipts import redact
from .runner import (
    DEFAULT_NOVAMIRA_WP_OPS,
    NOVAMIRA_TIMEOUT_SECONDS,
    ParamikoWpCliRunner,
    RunnerError,
    build_novamira_runner,
    build_runner,
    probe_public_cache_headers,
)


class QuotaSeverity:
    HEALTHY = "healthy"
    WARNING = "warning"
    CRITICAL = "critical"
    EXHAUSTED = "exhausted"


DEFAULT_INODES_LIMIT = 600_000
DEFAULT_WEB_SPACE_GB_LIMIT = 100.0
DEFAULT_HOURLY_EXECUTION_LIMIT = 4_000

WARN_PERCENT = 80.0
CRITICAL_PERCENT = 90.0
EXHAUSTED_PERCENT = 100.0

DEFAULT_QUOTA_STORE_DIR = Path.home() / ".config" / "siteground-ops" / "quota_telemetry"


def domains_match(domain1: str, domain2: str) -> bool:
    """Return True if two domains match exactly or one is a subdomain of the other."""
    d1 = domain1.lower().replace("https://", "").replace("http://", "").split("/")[0].split(":")[0].strip()
    d2 = domain2.lower().replace("https://", "").replace("http://", "").split("/")[0].split(":")[0].strip()
    if not d1 or not d2:
        return False
    if d1 == d2:
        return True
    if d1.endswith("." + d2) or d2.endswith("." + d1):
        return True
    return False


def evaluate_metric_severity(
    used: float,
    limit: float,
    *,
    warn_pct: float = WARN_PERCENT,
    crit_pct: float = CRITICAL_PERCENT,
    exhausted_pct: float = EXHAUSTED_PERCENT,
) -> str:
    if limit <= 0:
        return QuotaSeverity.HEALTHY
    pct = (used / limit) * 100.0
    if pct >= exhausted_pct:
        return QuotaSeverity.EXHAUSTED
    if pct >= crit_pct:
        return QuotaSeverity.CRITICAL
    if pct >= warn_pct:
        return QuotaSeverity.WARNING
    return QuotaSeverity.HEALTHY


@dataclass(frozen=True)
class SiteQuotaShare:
    domain: str
    web_space_gb: float
    inodes_count: int

    def inodes_percent_of_plan(self, plan_inodes_limit: int) -> float:
        if plan_inodes_limit <= 0:
            return 0.0
        return (self.inodes_count / plan_inodes_limit) * 100.0

    def web_space_percent_of_plan(self, plan_web_space_limit: float) -> float:
        if plan_web_space_limit <= 0:
            return 0.0
        return (self.web_space_gb / plan_web_space_limit) * 100.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "web_space_gb": self.web_space_gb,
            "inodes_count": self.inodes_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SiteQuotaShare:
        return cls(
            domain=str(data.get("domain", "")),
            web_space_gb=float(data.get("web_space_gb", 0.0)),
            inodes_count=int(data.get("inodes_count", 0)),
        )


@dataclass(frozen=True)
class QuotaMetric:
    name: str
    value: float
    limit: float
    free: float
    unit: str
    used_percent: float
    severity: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "limit": self.limit,
            "free": self.free,
            "unit": self.unit,
            "used_percent": round(self.used_percent, 2),
            "severity": self.severity,
            "message": self.message,
        }


@dataclass
class PlanQuotaSnapshot:
    plan_id: str
    plan_name: str
    observed_at: str
    web_space_limit_gb: float = DEFAULT_WEB_SPACE_GB_LIMIT
    web_space_used_gb: float = 0.0
    inodes_limit: int = DEFAULT_INODES_LIMIT
    inodes_used: int = 0
    hourly_execution_limit: int = DEFAULT_HOURLY_EXECUTION_LIMIT
    hourly_execution_peak: int = 0
    cpu_seconds_alert: bool = False
    site_shares: dict[str, SiteQuotaShare] = field(default_factory=dict)
    source: str = "portal"

    @property
    def metrics(self) -> dict[str, QuotaMetric]:
        res: dict[str, QuotaMetric] = {}

        # Inodes
        inode_sev = evaluate_metric_severity(self.inodes_used, self.inodes_limit)
        inode_free = max(0, self.inodes_limit - self.inodes_used)
        inode_pct = (self.inodes_used / self.inodes_limit * 100.0) if self.inodes_limit > 0 else 0.0
        res["inodes"] = QuotaMetric(
            name="inodes",
            value=float(self.inodes_used),
            limit=float(self.inodes_limit),
            free=float(inode_free),
            unit="count",
            used_percent=inode_pct,
            severity=inode_sev,
            message=f"{self.inodes_used:,} of {self.inodes_limit:,} inodes used ({inode_pct:.1f}%)",
        )

        # Web Space
        ws_sev = evaluate_metric_severity(self.web_space_used_gb, self.web_space_limit_gb)
        ws_free = max(0.0, self.web_space_limit_gb - self.web_space_used_gb)
        ws_pct = (self.web_space_used_gb / self.web_space_limit_gb * 100.0) if self.web_space_limit_gb > 0 else 0.0
        res["web_space"] = QuotaMetric(
            name="web_space",
            value=self.web_space_used_gb,
            limit=self.web_space_limit_gb,
            free=ws_free,
            unit="GB",
            used_percent=ws_pct,
            severity=ws_sev,
            message=f"{self.web_space_used_gb:.1f} GB of {self.web_space_limit_gb:.1f} GB used ({ws_pct:.1f}%)",
        )

        # Program Executions
        exec_sev = evaluate_metric_severity(self.hourly_execution_peak, self.hourly_execution_limit)
        exec_pct = (
            (self.hourly_execution_peak / self.hourly_execution_limit * 100.0)
            if self.hourly_execution_limit > 0
            else 0.0
        )
        res["program_executions"] = QuotaMetric(
            name="program_executions",
            value=float(self.hourly_execution_peak),
            limit=float(self.hourly_execution_limit),
            free=float(max(0, self.hourly_execution_limit - self.hourly_execution_peak)),
            unit="executions/hour",
            used_percent=exec_pct,
            severity=exec_sev,
            message=f"Peak {self.hourly_execution_peak:,}/hr (limit: {self.hourly_execution_limit:,}/hr)",
        )

        # CPU Seconds
        cpu_sev = QuotaSeverity.CRITICAL if self.cpu_seconds_alert else QuotaSeverity.HEALTHY
        res["cpu_seconds"] = QuotaMetric(
            name="cpu_seconds",
            value=100.0 if self.cpu_seconds_alert else 0.0,
            limit=100.0,
            free=0.0 if self.cpu_seconds_alert else 100.0,
            unit="status",
            used_percent=100.0 if self.cpu_seconds_alert else 0.0,
            severity=cpu_sev,
            message="Alert: Monthly CPU seconds quota reached" if self.cpu_seconds_alert else "Normal CPU usage",
        )

        return res

    @property
    def overall_severity(self) -> str:
        severities = [m.severity for m in self.metrics.values()]
        for level in (QuotaSeverity.EXHAUSTED, QuotaSeverity.CRITICAL, QuotaSeverity.WARNING):
            if level in severities:
                return level
        return QuotaSeverity.HEALTHY

    def top_sites_by_inodes(self) -> list[SiteQuotaShare]:
        return sorted(self.site_shares.values(), key=lambda s: s.inodes_count, reverse=True)

    def top_sites_by_web_space(self) -> list[SiteQuotaShare]:
        return sorted(self.site_shares.values(), key=lambda s: s.web_space_gb, reverse=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "plan_name": self.plan_name,
            "observed_at": self.observed_at,
            "web_space_limit_gb": self.web_space_limit_gb,
            "web_space_used_gb": self.web_space_used_gb,
            "inodes_limit": self.inodes_limit,
            "inodes_used": self.inodes_used,
            "hourly_execution_limit": self.hourly_execution_limit,
            "hourly_execution_peak": self.hourly_execution_peak,
            "cpu_seconds_alert": self.cpu_seconds_alert,
            "source": self.source,
            "overall_severity": self.overall_severity,
            "metrics": {k: m.to_dict() for k, m in self.metrics.items()},
            "site_shares": {k: s.to_dict() for k, s in self.site_shares.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlanQuotaSnapshot:
        shares = {}
        if isinstance(data.get("site_shares"), dict):
            for k, v in data["site_shares"].items():
                if isinstance(v, dict):
                    shares[k] = SiteQuotaShare.from_dict(v)
        return cls(
            plan_id=str(data.get("plan_id", "")),
            plan_name=str(data.get("plan_name", "")),
            observed_at=str(data.get("observed_at", "")),
            web_space_limit_gb=float(data.get("web_space_limit_gb", DEFAULT_WEB_SPACE_GB_LIMIT)),
            web_space_used_gb=float(data.get("web_space_used_gb", 0.0)),
            inodes_limit=int(data.get("inodes_limit", DEFAULT_INODES_LIMIT)),
            inodes_used=int(data.get("inodes_used", 0)),
            hourly_execution_limit=int(data.get("hourly_execution_limit", DEFAULT_HOURLY_EXECUTION_LIMIT)),
            hourly_execution_peak=int(data.get("hourly_execution_peak", 0)),
            cpu_seconds_alert=bool(data.get("cpu_seconds_alert", False)),
            site_shares=shares,
            source=str(data.get("source", "snapshot")),
        )


class QuotaStore:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or DEFAULT_QUOTA_STORE_DIR

    def _path_for(self, plan_id: str) -> Path:
        clean_id = re.sub(r"[^A-Za-z0-9_-]", "_", plan_id)
        return self.directory / f"{clean_id}.json"

    def save_snapshot(self, snapshot: PlanQuotaSnapshot, path: Path | None = None) -> Path:
        target = path or self._path_for(snapshot.plan_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(snapshot.to_dict(), indent=2), encoding="utf-8")
        return target

    def load_snapshot(self, plan_id: str, path: Path | None = None) -> PlanQuotaSnapshot | None:
        target = path or self._path_for(plan_id)
        if not target.is_file():
            return None
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            return PlanQuotaSnapshot.from_dict(data)
        except Exception:
            return None

    def list_snapshots(self) -> list[str]:
        if not self.directory.is_dir():
            return []
        res = []
        for file in self.directory.glob("*.json"):
            res.append(file.stem)
        return sorted(res)

    def find_plan_for_domain(self, domain: str) -> str | None:
        if not self.directory.is_dir():
            return None
        for file in sorted(self.directory.glob("*.json")):
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
                shares = data.get("site_shares")
                if isinstance(shares, dict):
                    for d in shares.keys():
                        if domains_match(domain, d):
                            return str(data.get("plan_id", file.stem))
            except Exception:
                continue
        return None


@dataclass(frozen=True)
class DiagnosticFinding:
    category: str
    severity: str
    title: str
    detail: str
    metric_value: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "metric_value": self.metric_value,
        }


@dataclass
class SiteDeepDiagnosis:
    site_id: str
    home_url: str
    transport: str
    inode_counts: dict[str, int] = field(default_factory=dict)
    sibling_staging_inodes: dict[str, int] = field(default_factory=dict)
    virtual_cron_enabled: bool = True
    cron_events_count: int = 0
    optimizer_active: bool = False
    dynamic_cache_enabled: bool = True
    edge_cache_hit: bool = True
    autoload_bytes: int = 0
    transients_count: int = 0
    active_plugins_count: int = 0
    findings: list[DiagnosticFinding] = field(default_factory=list)
    plugin_inode_counts: dict[str, int] = field(default_factory=dict)
    top_cron_hooks: dict[str, int] = field(default_factory=dict)
    xmlrpc_enabled: bool = True
    heartbeat_settings: dict[str, int] = field(default_factory=dict)
    crawler_traffic: dict[str, Any] = field(default_factory=dict)
    opcache_inodes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "site_id": self.site_id,
            "home_url": self.home_url,
            "transport": self.transport,
            "inode_counts": self.inode_counts,
            "sibling_staging_inodes": self.sibling_staging_inodes,
            "virtual_cron_enabled": self.virtual_cron_enabled,
            "cron_events_count": self.cron_events_count,
            "optimizer_active": self.optimizer_active,
            "dynamic_cache_enabled": self.dynamic_cache_enabled,
            "edge_cache_hit": self.edge_cache_hit,
            "autoload_bytes": self.autoload_bytes,
            "transients_count": self.transients_count,
            "active_plugins_count": self.active_plugins_count,
            "plugin_inode_counts": self.plugin_inode_counts,
            "top_cron_hooks": self.top_cron_hooks,
            "xmlrpc_enabled": self.xmlrpc_enabled,
            "heartbeat_settings": self.heartbeat_settings,
            "crawler_traffic": self.crawler_traffic,
            "opcache_inodes": self.opcache_inodes,
            "findings": [f.to_dict() for f in self.findings],
        }


DIAGNOSTIC_PHP = r'''
$dir = WP_CONTENT_DIR;
$stats = array();
$subdirs = array("cache", "uploads", "plugins", "themes", "languages", "upgrade", "wp-staging", "upgrade-temp-backup");
foreach ($subdirs as $sub) {
    $target = $dir . "/" . $sub;
    $count = 0;
    if (is_dir($target)) {
        try {
            $it = new RecursiveIteratorIterator(
                new RecursiveDirectoryIterator($target, FilesystemIterator::SKIP_DOTS),
                RecursiveIteratorIterator::CHILD_FIRST
            );
            foreach ($it as $file) {
                $count++;
                if ($count > 150000) break;
            }
        } catch (Exception $e) {
            $count = -1;
        }
    }
    $stats[$sub] = $count;
}

$plugin_counts = array();
$plugins_dir = $dir . "/plugins";
if (is_dir($plugins_dir)) {
    foreach (scandir($plugins_dir) as $item) {
        if ($item === "." || $item === "..") continue;
        $full = $plugins_dir . "/" . $item;
        if (is_dir($full)) {
            $cnt = 0;
            try {
                $it = new RecursiveIteratorIterator(
                    new RecursiveDirectoryIterator($full, FilesystemIterator::SKIP_DOTS)
                );
                foreach ($it as $f) {
                    $cnt++;
                    if ($cnt > 30000) break;
                }
            } catch (Exception $e) {}
            $plugin_counts[$item] = $cnt;
        }
    }
    arsort($plugin_counts);
    $plugin_counts = array_slice($plugin_counts, 0, 10);
}

global $wpdb;
$autoload_size = 0;
$transient_count = 0;
try {
    $autoload_size = (int) $wpdb->get_var("SELECT SUM(LENGTH(option_value)) FROM {$wpdb->options} WHERE autoload IN ('yes', 'on')");
    $transient_count = (int) $wpdb->get_var("SELECT COUNT(*) FROM {$wpdb->options} WHERE option_name LIKE '\_transient\_%' OR option_name LIKE '\_site\_transient\_%'");
} catch (Exception $e) {}

$cron_jobs = get_option("cron");
$cron_count = 0;
$hook_counts = array();
if (is_array($cron_jobs)) {
    foreach ($cron_jobs as $ts => $hooks) {
        if (is_array($hooks)) {
            foreach ($hooks as $h_name => $h_data) {
                $cron_count += count($h_data);
                if (!isset($hook_counts[$h_name])) $hook_counts[$h_name] = 0;
                $hook_counts[$h_name] += count($h_data);
            }
        }
    }
    arsort($hook_counts);
    $hook_counts = array_slice($hook_counts, 0, 10);
}

$opt = class_exists("SiteGround_Optimizer\\Options\\Options") ? new \SiteGround_Optimizer\Options\Options() : null;
$options = $opt ? $opt->fetch_options() : array();
$dynamic_cache = !empty($options["enable_cache"]) || !empty($options["dynamic_cache"]);
$file_caching = !empty($options["file_caching"]);
$heartbeat_settings = array(
    "post_interval" => isset($options["heartbeat_post_interval"]) ? (int)$options["heartbeat_post_interval"] : 0,
    "dashboard_interval" => isset($options["heartbeat_dashboard_interval"]) ? (int)$options["heartbeat_dashboard_interval"] : 0,
    "frontend_interval" => isset($options["heartbeat_frontend_interval"]) ? (int)$options["heartbeat_frontend_interval"] : 0,
);

$xmlrpc_enabled = (bool) apply_filters("xmlrpc_enabled", true);

$opcache_inodes = 0;
if (is_dir("/home/customer/.opcache")) {
    try {
        $it = new RecursiveIteratorIterator(
            new RecursiveDirectoryIterator("/home/customer/.opcache", FilesystemIterator::SKIP_DOTS)
        );
        foreach ($it as $f) {
            $opcache_inodes++;
            if ($opcache_inodes > 30000) break;
        }
    } catch (Exception $e) {}
}

$crawler_traffic = array(
    "sample_count" => 0,
    "cache_miss_count" => 0,
    "cache_hit_count" => 0,
    "wp_cron_count" => 0,
    "top_bots" => array(),
    "top_facet_urls" => array(),
);
$parent = dirname(rtrim(ABSPATH, "/"));
$logs_dir = $parent . "/logs";
$lines = array();
if (is_dir($logs_dir)) {
    $plain_logs = glob($logs_dir . "/*.access.log");
    if (!empty($plain_logs)) {
        sort($plain_logs);
        $latest_plain = end($plain_logs);
        if (is_readable($latest_plain) && filesize($latest_plain) > 0) {
            $fp = @fopen($latest_plain, "r");
            if ($fp) {
                while (!feof($fp) && count($lines) < 200) {
                    $l = fgets($fp, 1024);
                    if ($l !== false) $lines[] = $l;
                }
                @fclose($fp);
            }
        }
    }
    if (empty($lines) && function_exists("gzopen")) {
        $gz_files = glob($logs_dir . "/*.gz");
        if (!empty($gz_files)) {
            sort($gz_files);
            $latest_gz = end($gz_files);
            $zp = @gzopen($latest_gz, "r");
            if ($zp) {
                while (!gzeof($zp) && count($lines) < 200) {
                    $l = gzgets($zp, 1024);
                    if ($l !== false) $lines[] = $l;
                }
                @gzclose($zp);
            }
        }
    }
}
if (!empty($lines)) {
    $bot_counts = array();
    $facet_counts = array();
    $bot_pattern = "/(Amazonbot|PetalBot|Googlebot|bingbot|SemrushBot|AhrefsBot|Bytespider|YandexBot|ClaudeBot|GPTBot|facebookexternalhit|Twitterbot|MJ12bot|DotBot|DataForSeoBot|BLEXBot|Seekport)/i";
    foreach ($lines as $line) {
        if (preg_match("/\\bMISS\\b/", $line)) $crawler_traffic["cache_miss_count"]++;
        if (preg_match("/\\bHIT\\b/", $line)) $crawler_traffic["cache_hit_count"]++;
        if (strpos($line, "doing_wp_cron=") !== false) $crawler_traffic["wp_cron_count"]++;
        if (preg_match($bot_pattern, $line, $bm)) {
            $bname = $bm[1];
            $bot_counts[$bname] = ($bot_counts[$bname] ?? 0) + 1;
        }
        if (preg_match("/\"GET\s+([^\s]*\?[^\s]+)/", $line, $qm)) {
            $qurl = substr($qm[1], 0, 80);
            $facet_counts[$qurl] = ($facet_counts[$qurl] ?? 0) + 1;
        }
    }
    $crawler_traffic["sample_count"] = count($lines);
    arsort($bot_counts);
    arsort($facet_counts);
    $crawler_traffic["top_bots"] = array_slice($bot_counts, 0, 5);
    $crawler_traffic["top_facet_urls"] = array_slice($facet_counts, 0, 5);
}

return array(
    "home_url" => home_url(),
    "content_dir" => $dir,
    "stats" => $stats,
    "plugin_counts" => $plugin_counts,
    "top_cron_hooks" => $hook_counts,
    "disable_wp_cron" => defined("DISABLE_WP_CRON") && DISABLE_WP_CRON,
    "alternate_wp_cron" => defined("ALTERNATE_WP_CRON") && ALTERNATE_WP_CRON,
    "autoload_bytes" => $autoload_size,
    "transients_count" => $transient_count,
    "cron_events_count" => $cron_count,
    "optimizer_active" => (
        defined("SG_CACHEPRESS_VERSION") ||
        function_exists("sg_cachepress_purge_everything")
    ),
    "dynamic_cache" => $dynamic_cache,
    "file_caching" => $file_caching,
    "heartbeat_settings" => $heartbeat_settings,
    "xmlrpc_enabled" => $xmlrpc_enabled,
    "opcache_inodes" => $opcache_inodes,
    "crawler_traffic" => $crawler_traffic,
    "active_plugins_count" => count((array) get_option("active_plugins", array())),
);
'''.strip()


def _analyze_site_findings(diagnosis: SiteDeepDiagnosis) -> list[DiagnosticFinding]:
    findings: list[DiagnosticFinding] = []

    # 1. Crawler / Bot Facet Scrape Surge
    crawler = diagnosis.crawler_traffic
    if crawler:
        bots = crawler.get("top_bots", {})
        facets = crawler.get("top_facet_urls", {})
        misses = crawler.get("cache_miss_count", 0)
        total_samples = crawler.get("sample_count", 0)
        wp_cron_hits = crawler.get("wp_cron_count", 0)
        has_miss_surge = total_samples > 0 and (misses / total_samples) > 0.4
        if bots or wp_cron_hits > 0 or has_miss_surge:
            miss_pct = (misses / max(1, total_samples)) * 100.0
            if bots and facets:
                bot_summary = ", ".join(f"{b}: {c} requests" for b, c in bots.items())
                title = "Aggressive Bot Crawling & Facet Scrape Surge"
                detail = (
                    f"Automated crawlers ({bot_summary}) are querying combinatorial filter/facet URLs, generating "
                    f"{misses} cache misses in a {total_samples}-request sample ({miss_pct:.1f}% miss rate) "
                    f"and triggering {wp_cron_hits} direct/spawned wp-cron executions, causing massive CPU seconds consumption."
                )
            elif bots:
                bot_summary = ", ".join(f"{b}: {c} requests" for b, c in bots.items())
                title = "Aggressive Bot Crawling Traffic"
                detail = (
                    f"Automated crawlers ({bot_summary}) account for high request volume, generating "
                    f"{misses} cache misses in a {total_samples}-request sample ({miss_pct:.1f}% miss rate) "
                    f"and triggering {wp_cron_hits} wp-cron executions."
                )
            elif facets:
                title = "Combinatorial Facet/Query Scrape Surge"
                detail = (
                    f"Un-cached requests are hitting query string/facet URLs ({len(facets)} distinct patterns), "
                    f"generating {misses} cache misses in {total_samples} requests ({miss_pct:.1f}% miss rate) "
                    f"and triggering {wp_cron_hits} wp-cron executions."
                )
            else:
                title = "High Uncached Request & WP-Cron Trigger Volume"
                detail = (
                    f"Sampled traffic exhibits a high cache miss rate ({misses}/{total_samples}, {miss_pct:.1f}%) "
                    f"and triggered {wp_cron_hits} direct/spawned wp-cron executions, accelerating program execution counts."
                )

            findings.append(
                DiagnosticFinding(
                    category="CRAWLER_SCRAPE_SURGE",
                    severity=QuotaSeverity.CRITICAL if (wp_cron_hits > 3 or misses > 30) else QuotaSeverity.WARNING,
                    title=title,
                    detail=detail,
                    metric_value=crawler,
                )
            )

    # 2. Virtual Cron
    if diagnosis.virtual_cron_enabled:
        hook_details = ""
        if diagnosis.top_cron_hooks:
            hook_details = " Frequent hooks include: " + ", ".join(
                f"{h} ({c})" for h, c in list(diagnosis.top_cron_hooks.items())[:5]
            ) + "."
        findings.append(
            DiagnosticFinding(
                category="CRON_VIRTUAL",
                severity=QuotaSeverity.CRITICAL,
                title="Virtual WP-Cron Enabled (DISABLE_WP_CRON is false)",
                detail=(
                    f"Virtual WP-Cron executes on live web requests. With {diagnosis.cron_events_count} scheduled "
                    "hooks, incoming bot hits or visitor requests trigger synchronous PHP background jobs, "
                    f"causing massive Program Execution spikes and CPU exhaustion.{hook_details}"
                ),
                metric_value={"cron_events": diagnosis.cron_events_count, "top_hooks": diagnosis.top_cron_hooks},
            )
        )

    # 3. Dynamic Cache
    if not diagnosis.dynamic_cache_enabled:
        findings.append(
            DiagnosticFinding(
                category="CACHE_DYNAMIC_OFF",
                severity=QuotaSeverity.CRITICAL,
                title="SiteGround Dynamic Cache Disabled",
                detail=(
                    "Dynamic cache is disabled in Speed Optimizer. Every request bypasses Nginx and executes PHP, "
                    "dramatically multiplying CPU seconds and program execution count."
                ),
                metric_value=False,
            )
        )

    # 4. Edge Cache Miss
    if not diagnosis.edge_cache_hit:
        findings.append(
            DiagnosticFinding(
                category="CACHE_BYPASS",
                severity=QuotaSeverity.WARNING,
                title="Edge Cache Miss / Cache-Control Bypass",
                detail="Public requests produce an edge cache MISS (e.g. no-cache headers or plugin bypass).",
                metric_value=False,
            )
        )

    # 5. XML-RPC Active
    if diagnosis.xmlrpc_enabled:
        findings.append(
            DiagnosticFinding(
                category="XMLRPC_ACTIVE",
                severity=QuotaSeverity.WARNING,
                title="XML-RPC Interface Active (/xmlrpc.php)",
                detail=(
                    "XML-RPC is enabled on the WordPress site. Unauthenticated pingback and brute-force bot requests "
                    "can target /xmlrpc.php, triggering PHP worker processes and compounding hourly execution limits."
                ),
                metric_value=True,
            )
        )

    # 6. Inode Bloat in Plugins
    plugin_inodes = diagnosis.inode_counts.get("plugins", 0)
    if plugin_inodes > 30_000:
        breakdown_msg = ""
        if diagnosis.plugin_inode_counts:
            top_p = ", ".join(f"{p} ({c:,} files)" for p, c in list(diagnosis.plugin_inode_counts.items())[:5])
            breakdown_msg = f" Top consumers: {top_p}."
        findings.append(
            DiagnosticFinding(
                category="INODES_PLUGINS",
                severity=QuotaSeverity.CRITICAL if plugin_inodes > 60_000 else QuotaSeverity.WARNING,
                title="High Plugin Directory Inode Count",
                detail=(
                    f"wp-content/plugins contains {plugin_inodes:,} files across {diagnosis.active_plugins_count} "
                    f"active plugins.{breakdown_msg}"
                ),
                metric_value={"total": plugin_inodes, "top_plugins": diagnosis.plugin_inode_counts},
            )
        )

    # 7. Inode Bloat in Sibling Staging Sites
    for staging_name, count in diagnosis.sibling_staging_inodes.items():
        if count > 10_000:
            findings.append(
                DiagnosticFinding(
                    category="INODES_STAGING",
                    severity=QuotaSeverity.CRITICAL,
                    title="Sibling Staging Inode Bloat",
                    detail=(
                        f"Sibling staging directory '{staging_name}' under hosting account root consumes "
                        f"{count:,} inodes."
                    ),
                    metric_value=count,
                )
            )

    # 8. WP-Staging Bloat
    staging_inodes = diagnosis.inode_counts.get("wp-staging", 0)
    if staging_inodes > 5_000:
        findings.append(
            DiagnosticFinding(
                category="INODES_WP_STAGING",
                severity=QuotaSeverity.CRITICAL if staging_inodes > 15_000 else QuotaSeverity.WARNING,
                title="WP-Staging Inode Bloat",
                detail=f"wp-content/wp-staging contains {staging_inodes:,} files.",
                metric_value=staging_inodes,
            )
        )

    # 9. Upgrade Temp Backup
    upgrade_inodes = diagnosis.inode_counts.get("upgrade-temp-backup", 0)
    if upgrade_inodes > 1_000:
        findings.append(
            DiagnosticFinding(
                category="INODES_TEMP_BACKUP",
                severity=QuotaSeverity.CRITICAL if upgrade_inodes > 10_000 else QuotaSeverity.WARNING,
                title="Stale Upgrade Temporary Backup Inodes",
                detail=f"wp-content/upgrade-temp-backup contains {upgrade_inodes:,} temporary files.",
                metric_value=upgrade_inodes,
            )
        )

    # 10. OpCache Accumulation
    if diagnosis.opcache_inodes > 5_000:
        findings.append(
            DiagnosticFinding(
                category="INODES_OPCACHE",
                severity=QuotaSeverity.WARNING,
                title="OpCache File Accumulation",
                detail=(
                    f"PHP OpCache directory (/home/customer/.opcache) contains {diagnosis.opcache_inodes:,} files, "
                    "counting against the account-level inode quota."
                ),
                metric_value=diagnosis.opcache_inodes,
            )
        )

    # 11. Autoload Bloat
    if diagnosis.autoload_bytes > 800_000:
        findings.append(
            DiagnosticFinding(
                category="AUTOLOAD_BLOAT",
                severity=QuotaSeverity.WARNING,
                title="High wp_options Autoload Size",
                detail=(
                    f"wp_options autoload size is {diagnosis.autoload_bytes / 1024:.1f} KB (>800 KB). "
                    "Every PHP execution loads this data into memory on boot."
                ),
                metric_value=diagnosis.autoload_bytes,
            )
        )

    return findings


def probe_site_deep(site: SiteConfig) -> SiteDeepDiagnosis:
    if site.adapter == "novamira_mcp":
        runner = build_novamira_runner(site)
        data = runner._execute_php(DIAGNOSTIC_PHP)

        cache_headers = probe_public_cache_headers(site.public_url)
        edge_hit = cache_headers.get("x_proxy_cache") == "HIT"

        diagnosis = SiteDeepDiagnosis(
            site_id=site.site_id,
            home_url=str(data.get("home_url", site.public_url)),
            transport="novamira",
            inode_counts=data.get("stats", {}),
            sibling_staging_inodes={},
            virtual_cron_enabled=not bool(data.get("disable_wp_cron")),
            cron_events_count=int(data.get("cron_events_count", 0)),
            optimizer_active=bool(data.get("optimizer_active")),
            dynamic_cache_enabled=bool(data.get("dynamic_cache")),
            edge_cache_hit=edge_hit,
            autoload_bytes=int(data.get("autoload_bytes", 0)),
            transients_count=int(data.get("transients_count", 0)),
            active_plugins_count=int(data.get("active_plugins_count", 0)),
            plugin_inode_counts=data.get("plugin_counts", {}),
            top_cron_hooks=data.get("top_cron_hooks", {}),
            xmlrpc_enabled=bool(data.get("xmlrpc_enabled", True)),
            heartbeat_settings=data.get("heartbeat_settings", {}),
            crawler_traffic=data.get("crawler_traffic", {}),
            opcache_inodes=int(data.get("opcache_inodes", 0)),
        )
        diagnosis.findings = _analyze_site_findings(diagnosis)
        return diagnosis

    if site.adapter == "paramiko_wpcli":
        runner = build_runner(site)
        client = runner._connect()
        try:
            # 1. Inodes in wp-content subdirectories
            cmd_wp_content = (
                f"cd {site.remote_path} && for d in wp-content/*; do "
                'if [ -d "$d" ]; then echo "$d:$(find "$d" | wc -l)"; fi; done'
            )
            _, stdout, _ = client.exec_command(cmd_wp_content, timeout=45)
            inode_counts: dict[str, int] = {}
            for line in stdout.read().decode("utf-8", errors="replace").splitlines():
                line = line.strip()
                if ":" in line:
                    key, val = line.split(":", 1)
                    sub = key.replace("wp-content/", "").strip()
                    try:
                        inode_counts[sub] = int(val.strip())
                    except ValueError:
                        pass

            # 2. Sibling staging directories under ~/www/*
            cmd_siblings = (
                'for d in ~/www/*; do if [ -d "$d" ]; then echo "$d: $(find "$d" | wc -l)"; fi; done'
            )
            _, stdout, _ = client.exec_command(cmd_siblings, timeout=45)
            sibling_staging: dict[str, int] = {}
            primary_host = site.public_url.replace("https://", "").replace("http://", "").rstrip("/")
            for line in stdout.read().decode("utf-8", errors="replace").splitlines():
                line = line.strip()
                if ":" in line:
                    p, cnt = line.split(":", 1)
                    dirname = Path(p.strip()).name
                    if dirname and dirname != primary_host:
                        try:
                            sibling_staging[dirname] = int(cnt.strip())
                        except ValueError:
                            pass

            # 3. DISABLE_WP_CRON
            cmd_cron = f"cd {site.remote_path} && wp config get DISABLE_WP_CRON --format=json 2>/dev/null || echo false"
            _, stdout, _ = client.exec_command(cmd_cron, timeout=15)
            raw_cron = stdout.read().decode("utf-8", errors="replace").strip().lower()
            virtual_cron_enabled = raw_cron != "true"

            # 4. Cron events count
            cmd_cron_count = f"cd {site.remote_path} && wp eval 'echo count((array) get_option(\"cron\"));' 2>/dev/null || echo 0"
            _, stdout, _ = client.exec_command(cmd_cron_count, timeout=15)
            raw_cnt = stdout.read().decode("utf-8", errors="replace").strip()
            cron_events_count = int(raw_cnt) if raw_cnt.isdigit() else 0

            # 5. Autoload bytes
            cmd_autoload = (
                f"cd {site.remote_path} && wp eval "
                "'global $wpdb; echo (int)$wpdb->get_var(\"SELECT SUM(LENGTH(option_value)) FROM {$wpdb->options} WHERE autoload IN (\\\"yes\\\", \\\"on\\\")\");' 2>/dev/null || echo 0"
            )
            _, stdout, _ = client.exec_command(cmd_autoload, timeout=15)
            raw_auto = stdout.read().decode("utf-8", errors="replace").strip()
            autoload_bytes = int(raw_auto) if raw_auto.isdigit() else 0

            # 6. Transients count
            cmd_transients = (
                f"cd {site.remote_path} && wp eval "
                r"'global $wpdb; echo (int)$wpdb->get_var(\"SELECT COUNT(*) FROM {$wpdb->options} WHERE option_name LIKE \\\"\_transient\_%\\\"\");' 2>/dev/null || echo 0"
            )
            _, stdout, _ = client.exec_command(cmd_transients, timeout=15)
            raw_trans = stdout.read().decode("utf-8", errors="replace").strip()
            transients_count = int(raw_trans) if raw_trans.isdigit() else 0

            # 7. Active plugins count
            cmd_plugins_count = f"cd {site.remote_path} && wp plugin list --status=active --format=count 2>/dev/null || echo 0"
            _, stdout, _ = client.exec_command(cmd_plugins_count, timeout=15)
            raw_pcount = stdout.read().decode("utf-8", errors="replace").strip()
            active_plugins_count = int(raw_pcount) if raw_pcount.isdigit() else 0

            # 8. Cache status & headers
            cache_headers = probe_public_cache_headers(site.public_url)
            edge_hit = cache_headers.get("x_proxy_cache") == "HIT"

            # 9. Top plugin inodes
            cmd_plugin_counts = (
                f"cd {site.remote_path} && for d in wp-content/plugins/*; do "
                'if [ -d "$d" ]; then echo "$(basename "$d"):$(find "$d" | wc -l)"; fi; done'
            )
            _, stdout, _ = client.exec_command(cmd_plugin_counts, timeout=30)
            plugin_inode_counts: dict[str, int] = {}
            for line in stdout.read().decode("utf-8", errors="replace").splitlines():
                if ":" in line:
                    pname, pcnt = line.strip().split(":", 1)
                    if pcnt.strip().isdigit():
                        plugin_inode_counts[pname] = int(pcnt.strip())
            plugin_inode_counts = dict(sorted(plugin_inode_counts.items(), key=lambda x: x[1], reverse=True)[:10])

            # 10. XML-RPC
            cmd_xmlrpc = f"cd {site.remote_path} && wp eval 'echo apply_filters(\"xmlrpc_enabled\", true) ? \"1\" : \"0\";' 2>/dev/null || echo 1"
            _, stdout, _ = client.exec_command(cmd_xmlrpc, timeout=10)
            xmlrpc_enabled = stdout.read().decode("utf-8", errors="replace").strip() != "0"

            # 11. OpCache inodes
            cmd_opcache = 'if [ -d ~/.opcache ]; then find ~/.opcache | wc -l; else echo 0; fi'
            _, stdout, _ = client.exec_command(cmd_opcache, timeout=15)
            raw_op = stdout.read().decode("utf-8", errors="replace").strip()
            opcache_inodes = int(raw_op) if raw_op.isdigit() else 0

            # 12. Top recurring cron hooks
            cmd_cron_hooks = (
                f"cd {site.remote_path} && wp eval "
                "'$c = get_option(\"cron\"); $h = array(); if(is_array($c)){foreach($c as $hooks){if(is_array($hooks)){foreach($hooks as $k=>$v){$h[$k]=($h[$k]??0)+count($v);}}}} arsort($h); echo json_encode(array_slice($h, 0, 10));' "
                "--format=json 2>/dev/null || echo '{}'"
            )
            _, stdout, _ = client.exec_command(cmd_cron_hooks, timeout=15)
            raw_hooks = stdout.read().decode("utf-8", errors="replace").strip()
            top_cron_hooks: dict[str, int] = {}
            try:
                parsed_h = json.loads(raw_hooks)
                if isinstance(parsed_h, dict):
                    top_cron_hooks = {str(k): int(v) for k, v in parsed_h.items()}
            except Exception:
                pass

            # 13. Heartbeat settings
            cmd_heartbeat = (
                f"cd {site.remote_path} && wp eval "
                "'$o = class_exists(\"SiteGround_Optimizer\\\\Options\\\\Options\") ? (new \\SiteGround_Optimizer\\Options\\Options())->fetch_options() : array(); echo json_encode(array(\"post_interval\"=>(int)($o[\"heartbeat_post_interval\"]??0), \"dashboard_interval\"=>(int)($o[\"heartbeat_dashboard_interval\"]??0), \"frontend_interval\"=>(int)($o[\"heartbeat_frontend_interval\"]??0)));' "
                "--format=json 2>/dev/null || echo '{}'"
            )
            _, stdout, _ = client.exec_command(cmd_heartbeat, timeout=15)
            raw_hb = stdout.read().decode("utf-8", errors="replace").strip()
            heartbeat_settings: dict[str, int] = {}
            try:
                parsed_hb = json.loads(raw_hb)
                if isinstance(parsed_hb, dict):
                    heartbeat_settings = {str(k): int(v) for k, v in parsed_hb.items()}
            except Exception:
                pass

            # 14. Crawler traffic sample from access log
            crawler_traffic: dict[str, Any] = {
                "sample_count": 0,
                "cache_miss_count": 0,
                "cache_hit_count": 0,
                "wp_cron_count": 0,
                "top_bots": {},
                "top_facet_urls": {},
            }
            cmd_log = (
                f"logs_dir=\"~/www/{primary_host}/logs\"; "
                "if [ -d \"$logs_dir\" ]; then "
                "  plain=$(ls -t \"$logs_dir\"/*.access.log 2>/dev/null | head -1); "
                "  if [ -n \"$plain\" ] && [ -s \"$plain\" ]; then head -200 \"$plain\"; "
                "  else gz=$(ls -t \"$logs_dir\"/*.gz 2>/dev/null | head -1); "
                "    if [ -n \"$gz\" ]; then zcat \"$gz\" 2>/dev/null | head -200; fi; "
                "  fi; "
                "fi"
            )
            _, stdout, _ = client.exec_command(cmd_log, timeout=15)
            log_output = stdout.read().decode("utf-8", errors="replace")
            log_lines = [l for l in log_output.splitlines() if l.strip()]
            if log_lines:
                crawler_traffic["sample_count"] = len(log_lines)
                bot_counts: dict[str, int] = {}
                facet_counts: dict[str, int] = {}
                bot_re = re.compile(
                    r"(Amazonbot|PetalBot|Googlebot|bingbot|SemrushBot|AhrefsBot|Bytespider|YandexBot|"
                    r"ClaudeBot|GPTBot|facebookexternalhit|Twitterbot|MJ12bot|DotBot|DataForSeoBot|BLEXBot|Seekport)",
                    re.IGNORECASE,
                )
                facet_re = re.compile(r'"GET\s+([^\s]*\?[^\s]+)')
                miss_re = re.compile(r"\bMISS\b")
                hit_re = re.compile(r"\bHIT\b")
                for l in log_lines:
                    if miss_re.search(l):
                        crawler_traffic["cache_miss_count"] += 1
                    if hit_re.search(l):
                        crawler_traffic["cache_hit_count"] += 1
                    if "doing_wp_cron=" in l:
                        crawler_traffic["wp_cron_count"] += 1
                    bm = bot_re.search(l)
                    if bm:
                        bname = bm.group(1)
                        bot_counts[bname] = bot_counts.get(bname, 0) + 1
                    fm = facet_re.search(l)
                    if fm:
                        qurl = fm.group(1)[:80]
                        facet_counts[qurl] = facet_counts.get(qurl, 0) + 1
                crawler_traffic["top_bots"] = dict(sorted(bot_counts.items(), key=lambda x: x[1], reverse=True)[:5])
                crawler_traffic["top_facet_urls"] = dict(sorted(facet_counts.items(), key=lambda x: x[1], reverse=True)[:5])

            diagnosis = SiteDeepDiagnosis(
                site_id=site.site_id,
                home_url=site.public_url,
                transport="ssh",
                inode_counts=inode_counts,
                sibling_staging_inodes=sibling_staging,
                virtual_cron_enabled=virtual_cron_enabled,
                cron_events_count=cron_events_count,
                optimizer_active=True,
                dynamic_cache_enabled=True,
                edge_cache_hit=edge_hit,
                autoload_bytes=autoload_bytes,
                transients_count=transients_count,
                active_plugins_count=active_plugins_count,
                plugin_inode_counts=plugin_inode_counts,
                top_cron_hooks=top_cron_hooks,
                xmlrpc_enabled=xmlrpc_enabled,
                heartbeat_settings=heartbeat_settings,
                crawler_traffic=crawler_traffic,
                opcache_inodes=opcache_inodes,
            )
            diagnosis.findings = _analyze_site_findings(diagnosis)
            return diagnosis
        finally:
            client.close()

    raise RunnerError(f"Unsupported adapter {site.adapter!r} for deep quota diagnosis.")


ALLOWED_CLEANUP_TARGETS = frozenset({
    "upgrade-temp-backup",
    "wp-staging",
    "cache",
})


def clean_site_inodes(
    site: SiteConfig,
    target_dir: str,
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    if target_dir not in ALLOWED_CLEANUP_TARGETS:
        raise ValueError(
            f"Target directory {target_dir!r} is not an allowed cleanup target. "
            f"Allowed targets: {sorted(ALLOWED_CLEANUP_TARGETS)}"
        )

    if site.adapter == "novamira_mcp":
        runner = build_novamira_runner(site)
        target_sub_json = json.dumps(target_dir)
        dry_run_val = "true" if dry_run else "false"
        php = f"""
$target_sub = {target_sub_json};
$dry_run = {dry_run_val};
$dir = WP_CONTENT_DIR . '/' . $target_sub;
$file_count = 0;
$bytes_count = 0;
$deleted_files = 0;
$deleted_dirs = 0;
$errors = array();

if (is_dir($dir)) {{
    try {{
        $it = new RecursiveIteratorIterator(
            new RecursiveDirectoryIterator($dir, FilesystemIterator::SKIP_DOTS),
            RecursiveIteratorIterator::CHILD_FIRST
        );
        foreach ($it as $item) {{
            $file_count++;
            $size = 0;
            try {{ $size = $item->getSize(); }} catch (Exception $e) {{}}
            $bytes_count += $size;
            if (!$dry_run) {{
                if ($item->isDir()) {{
                    if (!@rmdir($item->getRealPath())) {{
                        $errors[] = "Failed rmdir: " . $item->getFilename();
                    }} else {{
                        $deleted_dirs++;
                    }}
                }} else {{
                    if (!@unlink($item->getRealPath())) {{
                        $errors[] = "Failed unlink: " . $item->getFilename();
                    }} else {{
                        $deleted_files++;
                    }}
                }}
            }}
        }}
    }} catch (Exception $e) {{
        $errors[] = $e->getMessage();
    }}
}}

return array(
    "home_url" => home_url(),
    "target_dir" => $target_sub,
    "path" => "wp-content/" . $target_sub,
    "dry_run" => (bool) $dry_run,
    "observed_files" => $file_count,
    "observed_bytes" => $bytes_count,
    "deleted_files" => $deleted_files,
    "deleted_dirs" => $deleted_dirs,
    "errors" => array_slice($errors, 0, 10),
);
""".strip()
        data = runner._execute_php(php)
        return {
            "site_id": site.site_id,
            "home_url": data.get("home_url", site.public_url),
            "target_dir": target_dir,
            "path": f"wp-content/{target_dir}",
            "dry_run": dry_run,
            "observed_files": int(data.get("observed_files", 0)),
            "observed_bytes": int(data.get("observed_bytes", 0)),
            "deleted_files": int(data.get("deleted_files", 0)),
            "deleted_dirs": int(data.get("deleted_dirs", 0)),
            "inodes_reclaimed": int(data.get("deleted_files", 0)) if not dry_run else 0,
            "disk_reclaimed_mb": round(int(data.get("observed_bytes", 0)) / (1024 * 1024), 2) if not dry_run else 0.0,
            "errors": data.get("errors", []),
            "transport": "novamira",
        }

    if site.adapter == "paramiko_wpcli":
        runner = build_runner(site)
        client = runner._connect()
        try:
            rel_path = f"wp-content/{target_dir}"
            check_cmd = (
                f"cd {site.remote_path} && if [ -d '{rel_path}' ]; then "
                f"find '{rel_path}' -mindepth 1 | wc -l; else echo 0; fi"
            )
            _, stdout, _ = client.exec_command(check_cmd, timeout=30)
            raw_cnt = stdout.read().decode("utf-8", errors="replace").strip()
            observed_files = int(raw_cnt) if raw_cnt.isdigit() else 0

            bytes_cmd = (
                f"cd {site.remote_path} && if [ -d '{rel_path}' ]; then "
                f"du -sk '{rel_path}' 2>/dev/null | cut -f1; else echo 0; fi"
            )
            _, stdout, _ = client.exec_command(bytes_cmd, timeout=15)
            raw_kb = stdout.read().decode("utf-8", errors="replace").strip()
            observed_bytes = int(raw_kb) * 1024 if raw_kb.isdigit() else 0

            deleted_files = 0
            command_executed = check_cmd
            if not dry_run and observed_files > 0:
                del_cmd = (
                    f"cd {site.remote_path} && if [ -d '{rel_path}' ]; then "
                    f"(find '{rel_path}' -mindepth 1 -delete 2>/dev/null || rm -rf '{rel_path}'/*) && "
                    f"find '{rel_path}' -mindepth 1 | wc -l; else echo 0; fi"
                )
                command_executed = del_cmd
                _, stdout, _ = client.exec_command(del_cmd, timeout=45)
                raw_rem = stdout.read().decode("utf-8", errors="replace").strip()
                rem = int(raw_rem) if raw_rem.isdigit() else 0
                deleted_files = max(0, observed_files - rem)

            return {
                "site_id": site.site_id,
                "home_url": site.public_url,
                "target_dir": target_dir,
                "path": rel_path,
                "dry_run": dry_run,
                "observed_files": observed_files,
                "observed_bytes": observed_bytes,
                "deleted_files": deleted_files,
                "deleted_dirs": 0,
                "inodes_reclaimed": deleted_files if not dry_run else 0,
                "disk_reclaimed_mb": round(observed_bytes / (1024 * 1024), 2) if not dry_run else 0.0,
                "command_executed": command_executed,
                "errors": [],
                "transport": "ssh",
            }
        finally:
            client.close()

    raise RunnerError(f"Unsupported adapter {site.adapter!r} for inode cleanup.")


def clean_sibling_staging(
    site: SiteConfig,
    staging_dir_name: str,
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Safely purge an obsolete sibling staging directory under ~/www/ via SSH."""
    if not staging_dir_name or "/" in staging_dir_name or "\\" in staging_dir_name or ".." in staging_dir_name:
        raise ValueError(
            f"Invalid staging directory name {staging_dir_name!r}: must not contain slashes or directory traversal."
        )

    clean_name = staging_dir_name.strip()
    if "staging" not in clean_name.lower():
        raise ValueError(
            f"Directory {clean_name!r} is not recognized as a staging directory (must contain 'staging')."
        )

    if site.public_url:
        prod_host = urlsplit(site.public_url).netloc.lower()
        if prod_host and prod_host == clean_name.lower():
            raise ValueError(
                f"Target {clean_name!r} matches the production site domain and cannot be deleted."
            )

    if site.adapter != "paramiko_wpcli":
        raise RunnerError(
            f"Adapter {site.adapter!r} does not support account-level staging directory cleanup (requires SSH)."
        )

    runner = build_runner(site)
    client = runner._connect()
    try:
        staging_path = f"~/www/{clean_name}"

        # Verify directory exists
        check_exists = f"if [ -d {staging_path} ]; then echo 1; else echo 0; fi"
        _, stdout, _ = client.exec_command(check_exists, timeout=15)
        raw_exists = stdout.read().decode("utf-8", errors="replace").strip()
        if raw_exists != "1":
            raise RunnerError(f"Staging directory {staging_path} does not exist on remote host.")

        # Count files/inodes
        cnt_cmd = f"find {staging_path} -mindepth 1 | wc -l"
        _, stdout, _ = client.exec_command(cnt_cmd, timeout=45)
        raw_cnt = stdout.read().decode("utf-8", errors="replace").strip()
        observed_files = int(raw_cnt) if raw_cnt.isdigit() else 0

        # Count bytes
        bytes_cmd = f"du -sk {staging_path} 2>/dev/null | cut -f1"
        _, stdout, _ = client.exec_command(bytes_cmd, timeout=20)
        raw_kb = stdout.read().decode("utf-8", errors="replace").strip()
        observed_bytes = int(raw_kb) * 1024 if raw_kb.isdigit() else 0

        deleted_files = 0
        deleted_dirs = 0
        command_executed = cnt_cmd
        if not dry_run:
            del_cmd = f"rm -rf {staging_path} && if [ ! -d {staging_path} ]; then echo 1; else echo 0; fi"
            command_executed = f"rm -rf {staging_path}"
            _, stdout, _ = client.exec_command(del_cmd, timeout=90)
            raw_del = stdout.read().decode("utf-8", errors="replace").strip()
            if raw_del != "1":
                raise RunnerError(f"Failed to completely delete {staging_path}.")
            deleted_files = observed_files
            deleted_dirs = 1

        inodes_reclaimed = deleted_files if not dry_run else 0
        disk_reclaimed_mb = round(observed_bytes / (1024 * 1024), 2) if not dry_run else 0.0

        return {
            "site_id": site.site_id,
            "home_url": site.public_url,
            "target_staging": clean_name,
            "path": staging_path,
            "dry_run": dry_run,
            "observed_files": observed_files,
            "observed_bytes": observed_bytes,
            "deleted_files": deleted_files,
            "deleted_dirs": deleted_dirs,
            "inodes_reclaimed": inodes_reclaimed,
            "disk_reclaimed_mb": disk_reclaimed_mb,
            "command_executed": command_executed,
            "errors": [],
            "transport": "ssh",
        }
    finally:
        client.close()


@dataclass(frozen=True)
class CulpritFinding:
    category: str
    severity: str
    site_id: str
    impact_summary: str
    technical_details: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "severity": self.severity,
            "site_id": self.site_id,
            "impact_summary": self.impact_summary,
            "technical_details": self.technical_details,
        }


@dataclass(frozen=True)
class RemediationAction:
    priority: int
    target: str
    category: str
    action_type: str
    command_hint: str
    description: str
    estimated_inode_savings: int = 0
    estimated_execution_savings: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "priority": self.priority,
            "target": self.target,
            "category": self.category,
            "action_type": self.action_type,
            "command_hint": self.command_hint,
            "description": self.description,
            "estimated_inode_savings": self.estimated_inode_savings,
            "estimated_execution_savings": self.estimated_execution_savings,
        }


@dataclass
class QuotaTriageReport:
    plan_id: str
    overall_severity: str
    immediate_action_needed: bool
    plan_metrics: dict[str, Any]
    site_diagnostics: list[SiteDeepDiagnosis]
    top_culprits: list[CulpritFinding]
    remediation_actions: list[RemediationAction]

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "overall_severity": self.overall_severity,
            "immediate_action_needed": self.immediate_action_needed,
            "plan_metrics": self.plan_metrics,
            "site_diagnostics": [d.to_dict() for d in self.site_diagnostics],
            "top_culprits": [c.to_dict() for c in self.top_culprits],
            "remediation_actions": [a.to_dict() for a in self.remediation_actions],
        }


class QuotaTriageEngine:
    def triage(
        self,
        snapshot: PlanQuotaSnapshot,
        site_diagnoses: list[SiteDeepDiagnosis],
    ) -> QuotaTriageReport:
        culprits: list[CulpritFinding] = []
        actions: list[RemediationAction] = []

        overall_sev = snapshot.overall_severity
        action_needed = overall_sev in (QuotaSeverity.CRITICAL, QuotaSeverity.EXHAUSTED)

        # 0. Plan-Level Site Share Analysis (detecting heavy unprobed sites)
        diagnosed_hosts = [
            diag.home_url for diag in site_diagnoses
        ]
        for site_share in snapshot.top_sites_by_inodes():
            is_diagnosed = any(
                domains_match(site_share.domain, dh)
                for dh in diagnosed_hosts
            )
            pct = site_share.inodes_percent_of_plan(snapshot.inodes_limit)
            if site_share.inodes_count > 50_000 and not is_diagnosed:
                culprits.append(
                    CulpritFinding(
                        category="INODES_SITE_SHARE",
                        severity=QuotaSeverity.CRITICAL if site_share.inodes_count > 100_000 else QuotaSeverity.WARNING,
                        site_id=site_share.domain,
                        impact_summary=(
                            f"Domain '{site_share.domain}' consumes {site_share.inodes_count:,} inodes "
                            f"({pct:.1f}% of plan limit) on hosting plan."
                        ),
                        technical_details=(
                            f"Web space: {site_share.web_space_gb:.2f} GB. In-site deep diagnosis was not performed "
                            "for this domain (ensure site profile is configured in sites.json)."
                        ),
                    )
                )

        # 1. Analyze Inode Culprits
        for diag in site_diagnoses:
            # Check Sibling Staging Sites (e.g. staging2.example.com)
            for staging_name, count in diag.sibling_staging_inodes.items():
                if count > 10_000:
                    culprits.append(
                        CulpritFinding(
                            category="INODES_STAGING",
                            severity=QuotaSeverity.CRITICAL,
                            site_id=diag.site_id,
                            impact_summary=f"Orphaned sibling staging directory '{staging_name}' consumes {count:,} inodes ({count/snapshot.inodes_limit*100:.1f}% of plan limit).",
                            technical_details=f"Path: ~/www/{staging_name}/. Contains full duplicate WordPress file tree.",
                        )
                    )
                    actions.append(
                        RemediationAction(
                            priority=1,
                            target=diag.site_id,
                            category="INODES_RECLAMATION",
                            action_type="manual",
                            command_hint=f"rm -rf ~/www/{staging_name}",
                            description=f"Verify whether staging site '{staging_name}' is obsolete. Deleting it reclaims {count:,} inodes immediately.",
                            estimated_inode_savings=count,
                        )
                    )

            # Check wp-staging within site
            wp_staging_cnt = diag.inode_counts.get("wp-staging", 0)
            if wp_staging_cnt > 5_000:
                culprits.append(
                    CulpritFinding(
                        category="INODES_WP_STAGING",
                        severity=QuotaSeverity.CRITICAL if wp_staging_cnt > 15_000 else QuotaSeverity.WARNING,
                        site_id=diag.site_id,
                        impact_summary=f"Stale WP-Staging directory in {diag.site_id} consumes {wp_staging_cnt:,} inodes.",
                        technical_details="Path: wp-content/wp-staging/ created by WP Staging plugin backup/migration.",
                    )
                )
                actions.append(
                    RemediationAction(
                        priority=1,
                        target=diag.site_id,
                        category="INODES_RECLAMATION",
                        action_type="automated",
                        command_hint=(
                            f"siteground-ops quota clean {diag.site_id} --target-dir wp-staging "
                            f"--confirm-target {diag.site_id} --recovery-receipt <receipt>"
                        ),
                        description=f"Purge stale staging copy files in {diag.site_id} to reclaim {wp_staging_cnt:,} inodes.",
                        estimated_inode_savings=wp_staging_cnt,
                    )
                )

            # Check upgrade temporary backup
            upgrade_cnt = diag.inode_counts.get("upgrade-temp-backup", 0)
            if upgrade_cnt > 1_000:
                culprits.append(
                    CulpritFinding(
                        category="INODES_TEMP_BACKUP",
                        severity=QuotaSeverity.CRITICAL if upgrade_cnt > 10_000 else QuotaSeverity.WARNING,
                        site_id=diag.site_id,
                        impact_summary=f"Stale upgrade temporary backup directory in {diag.site_id} consumes {upgrade_cnt:,} inodes.",
                        technical_details="Path: wp-content/upgrade-temp-backup/. Abandoned temporary copies created during plugin/core auto-updates.",
                    )
                )
                actions.append(
                    RemediationAction(
                        priority=1 if upgrade_cnt > 5_000 else 2,
                        target=diag.site_id,
                        category="INODES_RECLAMATION",
                        action_type="automated",
                        command_hint=(
                            f"siteground-ops quota clean {diag.site_id} --target-dir upgrade-temp-backup "
                            f"--confirm-target {diag.site_id} --recovery-receipt <receipt>"
                        ),
                        description=f"Clear abandoned temporary upgrade backups in {diag.site_id} ({upgrade_cnt:,} inodes).",
                        estimated_inode_savings=upgrade_cnt,
                    )
                )

            # Check plugins inode bloat
            plugin_inodes = diag.inode_counts.get("plugins", 0)
            if plugin_inodes > 30_000:
                top_plugins_desc = ""
                if diag.plugin_inode_counts:
                    top_plugins_desc = " Top plugin consumers: " + ", ".join(
                        f"{p} ({c:,} files)" for p, c in list(diag.plugin_inode_counts.items())[:3]
                    ) + "."
                culprits.append(
                    CulpritFinding(
                        category="INODES_PLUGINS",
                        severity=QuotaSeverity.CRITICAL if plugin_inodes > 60_000 else QuotaSeverity.WARNING,
                        site_id=diag.site_id,
                        impact_summary=f"Plugins directory on {diag.site_id} consumes {plugin_inodes:,} inodes across {diag.active_plugins_count} active plugins.{top_plugins_desc}",
                        technical_details="Path: wp-content/plugins/.",
                    )
                )

            # Check opcache accumulation
            if diag.opcache_inodes > 5_000:
                culprits.append(
                    CulpritFinding(
                        category="INODES_OPCACHE",
                        severity=QuotaSeverity.WARNING,
                        site_id=diag.site_id,
                        impact_summary=f"OpCache directory on {diag.site_id} consumes {diag.opcache_inodes:,} files.",
                        technical_details="Path: /home/customer/.opcache/.",
                    )
                )

        # 2. Analyze Program Execution & CPU Seconds Culprits
        for diag in site_diagnoses:
            # Check crawler / bot scraping surge
            crawler = diag.crawler_traffic
            if crawler and (crawler.get("top_bots") or crawler.get("wp_cron_count", 0) > 0 or crawler.get("cache_miss_count", 0) > 30):
                bots_str = ", ".join(f"{b} ({c})" for b, c in crawler.get("top_bots", {}).items()) if crawler.get("top_bots") else "facet crawlers"
                culprits.append(
                    CulpritFinding(
                        category="CRAWLER_SCRAPE_SURGE",
                        severity=QuotaSeverity.CRITICAL,
                        site_id=diag.site_id,
                        impact_summary=f"Aggressive bot crawling ({bots_str}) generating heavy cache misses and triggering wp-cron executions on {diag.site_id}.",
                        technical_details=f"Crawler traffic sample: {crawler.get('cache_miss_count', 0)} misses, {crawler.get('wp_cron_count', 0)} wp-cron triggers in {crawler.get('sample_count', 0)} requests.",
                    )
                )
                actions.append(
                    RemediationAction(
                        priority=1,
                        target=diag.site_id,
                        category="BOT_SCRAPER_DEFENSE",
                        action_type="manual",
                        command_hint="Add robots.txt Disallow rule or block via SiteGround Site Tools > Security > Blocked IPs / Cloudflare WAF",
                        description=(
                            f"Block aggressive bots (e.g. Amazonbot) from indexing faceted shop search parameters "
                            f"on {diag.site_id}. Stops repetitive query string cache misses and eliminates hundreds of PHP executions per hour."
                        ),
                        estimated_execution_savings="Stops up to thousands of bot-induced PHP executions/hr",
                    )
                )

            if diag.virtual_cron_enabled:
                hook_summary = ""
                if diag.top_cron_hooks:
                    hook_summary = f" Frequent hooks: {', '.join(list(diag.top_cron_hooks.keys())[:3])}."
                culprits.append(
                    CulpritFinding(
                        category="CRON_VIRTUAL",
                        severity=QuotaSeverity.CRITICAL,
                        site_id=diag.site_id,
                        impact_summary=f"Virtual WP-Cron active on {diag.site_id}. Every visitor/bot visit executes {diag.cron_events_count} scheduled hooks.{hook_summary}",
                        technical_details="DISABLE_WP_CRON is unset/false. Triggers PHP executions on every HTTP request.",
                    )
                )
                actions.append(
                    RemediationAction(
                        priority=1,
                        target=diag.site_id,
                        category="CRON_STABILIZATION",
                        action_type="manual",
                        command_hint="wp config set DISABLE_WP_CRON true --raw && setup SiteGround/External cron",
                        description=(
                            f"Disable virtual wp-cron in wp-config.php on {diag.site_id}. Delegate wp-cron execution "
                            "to system crontab or an external scheduler (every 10-15 mins) to prevent bot-driven execution spikes."
                        ),
                        estimated_execution_savings="Eliminates up to 80% of unnecessary background PHP spawns",
                    )
                )

            if not diag.dynamic_cache_enabled:
                culprits.append(
                    CulpritFinding(
                        category="CACHE_DYNAMIC_OFF",
                        severity=QuotaSeverity.CRITICAL,
                        site_id=diag.site_id,
                        impact_summary=f"Dynamic cache is disabled on {diag.site_id}. PHP processes all unauthenticated page loads.",
                        technical_details="SiteGround Optimizer dynamic_cache is false.",
                    )
                )
                actions.append(
                    RemediationAction(
                        priority=1,
                        target=diag.site_id,
                        category="CACHE_ACCELERATION",
                        action_type="manual",
                        command_hint=f"Enable Dynamic Cache in Speed Optimizer on {diag.site_id} or Site Tools > Caching",
                        description=f"Enable SiteGround Dynamic Cache on {diag.site_id} to absorb traffic at Nginx reverse proxy layer.",
                        estimated_execution_savings="Caches 70-90% of repeat web requests at Nginx layer",
                    )
                )

            if not diag.edge_cache_hit:
                culprits.append(
                    CulpritFinding(
                        category="CACHE_BYPASS",
                        severity=QuotaSeverity.WARNING,
                        site_id=diag.site_id,
                        impact_summary=f"Edge cache MISS on {diag.site_id}. Header cache-control bypasses edge cache.",
                        technical_details="Site emits no-cache or fails to set proper caching headers for Nginx proxy.",
                    )
                )

            if diag.xmlrpc_enabled:
                culprits.append(
                    CulpritFinding(
                        category="XMLRPC_ACTIVE",
                        severity=QuotaSeverity.WARNING,
                        site_id=diag.site_id,
                        impact_summary=f"XML-RPC enabled on {diag.site_id}. Exposes site to automated bot probing and pingback amplification.",
                        technical_details="xmlrpc_enabled filter is true.",
                    )
                )
                actions.append(
                    RemediationAction(
                        priority=2,
                        target=diag.site_id,
                        category="SECURITY_HARDENING",
                        action_type="manual",
                        command_hint="add_filter('xmlrpc_enabled', '__return_false');",
                        description=f"Disable XML-RPC interface on {diag.site_id} if no remote posting apps are used, preventing brute-force execution spikes.",
                        estimated_execution_savings="Prevents unauthenticated bot brute-force storms",
                    )
                )

        # Sort actions by priority (1 first)
        actions.sort(key=lambda a: a.priority)

        return QuotaTriageReport(
            plan_id=snapshot.plan_id,
            overall_severity=overall_sev,
            immediate_action_needed=action_needed,
            plan_metrics={k: m.to_dict() for k, m in snapshot.metrics.items()},
            site_diagnostics=site_diagnoses,
            top_culprits=culprits,
            remediation_actions=actions,
        )
