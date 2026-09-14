from __future__ import annotations

import json
import re
import shlex
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from .config import SiteConfig
from .runner import (
    ParamikoWpCliRunner,
    RunnerError,
    build_novamira_runner,
    build_runner,
    probe_public_cache_headers,
)

CRON_STOP_MARKER = "/* That's all, stop editing! Happy publishing. */"
CRON_FALLBACK_MARKER = "require_once ABSPATH . 'wp-settings.php';"
CRON_DEFINE_STATEMENT = "define( 'DISABLE_WP_CRON', true );"


def inspect_cron(site: SiteConfig) -> dict[str, Any]:
    """Inspect the virtual cron and wp-config state of a WordPress site."""
    if site.adapter == "novamira_mcp":
        runner = build_novamira_runner(site)
        php = """
$config_path = ABSPATH . 'wp-config.php';
$exists = file_exists($config_path);
$is_writable = $exists ? is_writable($config_path) : false;
$content = $exists ? file_get_contents($config_path) : "";

$defined = defined('DISABLE_WP_CRON');
$val = $defined ? (bool)DISABLE_WP_CRON : null;

// Test cron endpoint responsiveness
$cron_url = home_url() . '/wp-cron.php?doing_wp_cron';
$t0 = microtime(true);
$resp = wp_remote_get($cron_url, array('timeout' => 10, 'sslverify' => false));
$elapsed = microtime(true) - $t0;

$cron_http_code = is_wp_error($resp) ? 0 : wp_remote_retrieve_response_code($resp);
$cron_events = function_exists('_get_cron_array') ? count((array)_get_cron_array()) : 0;

return array(
    "home_url" => home_url(),
    "site_id" => "{site_id}",
    "config_exists" => $exists,
    "config_writable" => $is_writable,
    "disable_wp_cron_defined" => $defined,
    "disable_wp_cron_value" => $val,
    "cron_runner_http_status" => $cron_http_code,
    "cron_runner_latency_ms" => round($elapsed * 1000, 2),
    "cron_events_count" => $cron_events,
    "decoupled" => ($defined && $val === true),
    "transport" => "novamira",
);
""".replace("{site_id}", site.site_id)
        data = runner._execute_php(php)
        return {
            "site_id": site.site_id,
            "home_url": data.get("home_url", site.public_url),
            "config_exists": bool(data.get("config_exists")),
            "config_writable": bool(data.get("config_writable")),
            "disable_wp_cron_defined": bool(data.get("disable_wp_cron_defined")),
            "disable_wp_cron_value": data.get("disable_wp_cron_value"),
            "cron_runner_http_status": int(data.get("cron_runner_http_status", 0)),
            "cron_runner_latency_ms": float(data.get("cron_runner_latency_ms", 0.0)),
            "cron_events_count": int(data.get("cron_events_count", 0)),
            "decoupled": bool(data.get("decoupled")),
            "transport": "novamira",
        }

    if site.adapter == "paramiko_wpcli":
        runner = build_runner(site)
        client = runner._connect()
        try:
            cfg_path = f"{site.remote_path}/wp-config.php"
            check_cmd = (
                f"cd {site.remote_path} && "
                f"test -f wp-config.php && test -w wp-config.php && echo 'EXISTS_WRITABLE' || echo 'FAILED'"
            )
            _, stdout, _ = client.exec_command(check_cmd, timeout=15)
            status = stdout.read().decode("utf-8", errors="replace").strip()
            config_exists = "EXISTS" in status
            config_writable = "WRITABLE" in status

            # Check if defined in wp-config.php
            grep_cmd = f"cd {site.remote_path} && grep -E 'DISABLE_WP_CRON' wp-config.php || true"
            _, stdout, _ = client.exec_command(grep_cmd, timeout=15)
            grep_out = stdout.read().decode("utf-8", errors="replace").strip()
            disable_wp_cron_defined = "DISABLE_WP_CRON" in grep_out
            disable_wp_cron_value = None
            if disable_wp_cron_defined:
                disable_wp_cron_value = "true" in grep_out.lower()

            # Test wp-cli cron runner
            cron_test_cmd = f"cd {site.remote_path} && wp cron event run --due-now --quiet && echo 'CRON_SUCCESS' || echo 'CRON_FAILED'"
            _, stdout, _ = client.exec_command(cron_test_cmd, timeout=30)
            cron_res = stdout.read().decode("utf-8", errors="replace").strip()
            cron_runner_ok = "CRON_SUCCESS" in cron_res

            # Count cron events
            cnt_cmd = f"cd {site.remote_path} && wp cron event list --format=count || echo 0"
            _, stdout, _ = client.exec_command(cnt_cmd, timeout=15)
            cnt_str = stdout.read().decode("utf-8", errors="replace").strip()
            cron_events = int(cnt_str) if cnt_str.isdigit() else 0

            return {
                "site_id": site.site_id,
                "home_url": site.public_url,
                "config_exists": config_exists,
                "config_writable": config_writable,
                "disable_wp_cron_defined": disable_wp_cron_defined,
                "disable_wp_cron_value": disable_wp_cron_value,
                "cron_runner_http_status": 200 if cron_runner_ok else 500,
                "cron_runner_latency_ms": 0.0,
                "cron_events_count": cron_events,
                "decoupled": bool(disable_wp_cron_defined and disable_wp_cron_value is True),
                "transport": "ssh",
            }
        finally:
            client.close()

    raise RunnerError(f"Unsupported adapter {site.adapter!r} for cron inspection.")


def decouple_cron(
    site: SiteConfig,
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Decouple Virtual WP-Cron from frontend HTTP requests with fail-closed rollback."""
    # 1. Inspect first
    inspection = inspect_cron(site)
    if not inspection.get("config_exists"):
        raise RunnerError(f"wp-config.php does not exist for site {site.site_id!r}.")
    if not inspection.get("config_writable"):
        raise RunnerError(f"wp-config.php is not writable for site {site.site_id!r}.")

    # Preflight verification: background cron must be proven runnable before we decouple
    cron_status = inspection.get("cron_runner_http_status", 0)
    if cron_status not in (200, 204):
        raise RunnerError(
            f"Preflight check failed: background cron runner returned status {cron_status} "
            f"(expected 200). Refusing to decouple to prevent queue stalls."
        )

    if inspection.get("decoupled"):
        return {
            "site_id": site.site_id,
            "home_url": site.public_url,
            "dry_run": dry_run,
            "already_decoupled": True,
            "mutation_applied": False,
            "backup_path": None,
            "inspection": inspection,
            "transport": inspection.get("transport"),
            "message": "Site already has DISABLE_WP_CRON set to true.",
        }

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    if site.adapter == "novamira_mcp":
        runner = build_novamira_runner(site)
        if dry_run:
            return {
                "site_id": site.site_id,
                "home_url": site.public_url,
                "dry_run": True,
                "already_decoupled": False,
                "mutation_applied": False,
                "backup_path": f"wp-config.php.bak.{timestamp}",
                "proposed_statement": CRON_DEFINE_STATEMENT,
                "inspection": inspection,
                "transport": "novamira",
                "message": "Dry-run successful: preflight runner passed and wp-config is ready for insertion.",
            }

        # Non-dry-run execution via Novamira
        php_decouple = f"""
$config_path = ABSPATH . 'wp-config.php';
$backup_name = 'wp-config.php.bak.{timestamp}';
$backup_path = ABSPATH . $backup_name;

$content = file_get_contents($config_path);
if ($content === false) {{
    return array("home_url" => home_url(), "error" => "Failed to read wp-config.php");
}}

// 1. Create atomic backup
if (!copy($config_path, $backup_path)) {{
    return array("home_url" => home_url(), "error" => "Failed to create atomic backup file: " . $backup_name);
}}

// 2. Inject define('DISABLE_WP_CRON', true);
$statement = "{CRON_DEFINE_STATEMENT}\\n";
if (strpos($content, "{CRON_STOP_MARKER}") !== false) {{
    $new_content = str_replace("{CRON_STOP_MARKER}", $statement . "{CRON_STOP_MARKER}", $content);
}} elseif (strpos($content, "{CRON_FALLBACK_MARKER}") !== false) {{
    $new_content = str_replace("{CRON_FALLBACK_MARKER}", $statement . "{CRON_FALLBACK_MARKER}", $content);
}} else {{
    $new_content = preg_replace('/<\\?php\\s+/', "<?php\\n" . $statement, $content, 1);
}}

if (file_put_contents($config_path, $new_content) === false) {{
    @copy($backup_path, $config_path);
    return array("home_url" => home_url(), "error" => "Failed to write modified wp-config.php; restored from backup");
}}

// 3. Readback verify
$verify_resp = wp_remote_get(home_url(), array('timeout' => 15, 'sslverify' => false));
$http_code = is_wp_error($verify_resp) ? 0 : wp_remote_retrieve_response_code($verify_resp);
if ($http_code !== 200 && $http_code !== 301 && $http_code !== 302) {{
    // Automatic rollback on non-200 home page!
    @copy($backup_path, $config_path);
    return array(
        "home_url" => home_url(),
        "error" => "Readback verification failed: home page returned HTTP " . $http_code . ". Automatically rolled back.",
        "rolled_back" => true,
    );
}}

return array(
    "home_url" => home_url(),
    "success" => true,
    "backup_name" => $backup_name,
    "http_code" => $http_code,
    "decoupled" => true,
);
"""
        res = runner._execute_php(php_decouple)
        if res.get("error"):
            raise RunnerError(f"Novamira cron decouple failed: {res['error']}")

        return {
            "site_id": site.site_id,
            "home_url": site.public_url,
            "dry_run": False,
            "already_decoupled": False,
            "mutation_applied": True,
            "backup_path": res.get("backup_name"),
            "http_code": res.get("http_code"),
            "transport": "novamira",
            "message": "Virtual WP-Cron successfully decoupled and verified with HTTP 200 readback.",
        }

    if site.adapter == "paramiko_wpcli":
        runner = build_runner(site)
        client = runner._connect()
        try:
            backup_name = f"wp-config.php.bak.{timestamp}"
            if dry_run:
                return {
                    "site_id": site.site_id,
                    "home_url": site.public_url,
                    "dry_run": True,
                    "already_decoupled": False,
                    "mutation_applied": False,
                    "backup_path": backup_name,
                    "proposed_statement": CRON_DEFINE_STATEMENT,
                    "inspection": inspection,
                    "transport": "ssh",
                    "message": "Dry-run successful: preflight runner passed and wp-config is ready for insertion.",
                }

            # 1. Create atomic backup
            cp_cmd = f"cd {site.remote_path} && cp wp-config.php {backup_name} && test -f {backup_name} && echo 'BACKUP_OK'"
            _, stdout, _ = client.exec_command(cp_cmd, timeout=15)
            if "BACKUP_OK" not in stdout.read().decode("utf-8", errors="replace"):
                raise RunnerError(f"Failed to create atomic backup {backup_name} via SSH.")

            # 2. Inject define statement
            inject_cmd = (
                f"cd {site.remote_path} && python3 -c \"\n"
                f"with open('wp-config.php', 'r') as f:\n"
                f"    data = f.read()\n"
                f"stmt = \\\"{CRON_DEFINE_STATEMENT}\\\\n\\\"\n"
                f"if '{CRON_STOP_MARKER}' in data:\n"
                f"    new_data = data.replace('{CRON_STOP_MARKER}', stmt + '{CRON_STOP_MARKER}')\n"
                f"elif '{CRON_FALLBACK_MARKER}' in data:\n"
                f"    new_data = data.replace('{CRON_FALLBACK_MARKER}', stmt + '{CRON_FALLBACK_MARKER}')\n"
                f"else:\n"
                f"    new_data = re.sub(r'<\\\\?php\\\\s+', '<?php\\\\n' + stmt, data, count=1)\n"
                f"with open('wp-config.php', 'w') as f:\n"
                f"    f.write(new_data)\n"
                f"\"\n"
            )
            _, stdout, stderr = client.exec_command(inject_cmd, timeout=20)
            err = stderr.read().decode("utf-8", errors="replace").strip()
            if err:
                client.exec_command(f"cd {site.remote_path} && cp {backup_name} wp-config.php")
                raise RunnerError(f"Injection failed: {err}. Rolled back from backup.")

            # 3. Readback verify via WP-CLI
            verify_cmd = f"cd {site.remote_path} && wp core is-installed && wp cron event run --due-now --quiet && echo 'VERIFY_OK'"
            _, stdout, _ = client.exec_command(verify_cmd, timeout=30)
            if "VERIFY_OK" not in stdout.read().decode("utf-8", errors="replace"):
                client.exec_command(f"cd {site.remote_path} && cp {backup_name} wp-config.php")
                raise RunnerError("Readback verification failed via WP-CLI. Automatically rolled back.")

            return {
                "site_id": site.site_id,
                "home_url": site.public_url,
                "dry_run": False,
                "already_decoupled": False,
                "mutation_applied": True,
                "backup_path": backup_name,
                "transport": "ssh",
                "message": "Virtual WP-Cron successfully decoupled and verified with WP-CLI readback.",
            }
        finally:
            client.close()

    raise RunnerError(f"Unsupported adapter {site.adapter!r} for cron decoupling.")


def rollback_cron(
    site: SiteConfig,
    *,
    backup_file: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Roll back wp-config.php to a prior backup file."""
    if site.adapter == "novamira_mcp":
        runner = build_novamira_runner(site)
        file_arg = json.dumps(backup_file) if backup_file else "null"
        dry_val = "true" if dry_run else "false"
        php = f"""
$specified_backup = {file_arg};
$dry_run = {dry_val};
$dir = ABSPATH;

$target_backup = null;
if ($specified_backup) {{
    if (file_exists($dir . $specified_backup)) {{
        $target_backup = $specified_backup;
    }}
}} else {{
    $backups = glob($dir . 'wp-config.php.bak.*');
    if ($backups && count($backups) > 0) {{
        rsort($backups);
        $target_backup = basename($backups[0]);
    }}
}}

if (!$target_backup) {{
    return array("home_url" => home_url(), "error" => "No valid backup file found to rollback.");
}}

if (!$dry_run) {{
    if (!copy($dir . $target_backup, $dir . 'wp-config.php')) {{
        return array("home_url" => home_url(), "error" => "Failed to copy backup over wp-config.php");
    }}
}}

return array(
    "home_url" => home_url(),
    "dry_run" => $dry_run,
    "restored_from" => $target_backup,
    "success" => true,
);
"""
        res = runner._execute_php(php)
        if res.get("error"):
            raise RunnerError(f"Rollback failed: {res['error']}")

        return {
            "site_id": site.site_id,
            "home_url": site.public_url,
            "dry_run": dry_run,
            "restored_from": res.get("restored_from"),
            "mutation_applied": not dry_run,
            "transport": "novamira",
            "message": f"Successfully rolled back from {res.get('restored_from')}.",
        }

    if site.adapter == "paramiko_wpcli":
        runner = build_runner(site)
        client = runner._connect()
        try:
            if backup_file:
                target_cmd = f"test -f {site.remote_path}/{backup_file} && echo '{backup_file}' || echo 'NOT_FOUND'"
            else:
                target_cmd = f"cd {site.remote_path} && ls -t wp-config.php.bak.* 2>/dev/null | head -n 1 || echo 'NOT_FOUND'"

            _, stdout, _ = client.exec_command(target_cmd, timeout=15)
            target = stdout.read().decode("utf-8", errors="replace").strip()
            if not target or target == "NOT_FOUND":
                raise RunnerError("No valid backup file found to rollback.")

            if dry_run:
                return {
                    "site_id": site.site_id,
                    "home_url": site.public_url,
                    "dry_run": True,
                    "restored_from": target,
                    "mutation_applied": False,
                    "transport": "ssh",
                    "message": f"Dry-run rollback target found: {target}.",
                }

            restore_cmd = f"cd {site.remote_path} && cp {target} wp-config.php && echo 'RESTORED'"
            _, stdout, _ = client.exec_command(restore_cmd, timeout=15)
            if "RESTORED" not in stdout.read().decode("utf-8", errors="replace"):
                raise RunnerError(f"Failed to restore wp-config.php from {target}.")

            return {
                "site_id": site.site_id,
                "home_url": site.public_url,
                "dry_run": False,
                "restored_from": target,
                "mutation_applied": True,
                "transport": "ssh",
                "message": f"Successfully rolled back from {target}.",
            }
        finally:
            client.close()

    raise RunnerError(f"Unsupported adapter {site.adapter!r} for cron rollback.")
