from __future__ import annotations

import re
import json
import hashlib
import os
import selectors
import shlex
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from .config import SiteConfig
from .receipts import redact


class RunnerError(RuntimeError):
    pass


SAFE_REMOTE_PATH = re.compile(r"^(?:~/|/)[A-Za-z0-9._/@+-]+(?:/[A-Za-z0-9._@+-]+)*/?$ ".strip())
DEFAULT_NOVAMIRA_WP_OPS = (
    Path.home() / ".gemini" / "antigravity" / "skills" / "novamira-ops" / "scripts" / "wp_ops.py"
)
MAX_NOVAMIRA_OUTPUT_BYTES = 1_000_000
MAX_NOVAMIRA_ERROR_BYTES = 64_000
NOVAMIRA_TIMEOUT_SECONDS = 90
PINNED_NOVAMIRA_MCP_VERSION = "0.3.5"
DEFAULT_NOVAMIRA_RUNTIME_MANIFEST = Path.home() / ".config" / "siteground-ops" / "novamira-runtime.json"
NOVAMIRA_RUNTIME_ENV = "SITEGROUND_OPS_NOVAMIRA_RUNTIME"
NOVAMIRA_CONFIG_PATHS = (
    Path.home() / ".gemini" / "antigravity" / "mcp_config.json",
    Path.home() / ".gemini" / "config" / "mcp_config.json",
)
NOVAMIRA_REQUIRED_ENV = ("WP_API_URL", "WP_API_USERNAME", "WP_API_PASSWORD")
NOVAMIRA_BRIDGE_ENV_ALLOWLIST = (
    "HOME",
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TMP",
    "TEMP",
    "NOVAMIRA_MCP_CONFIG",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)


def _remote_path_is_safe(remote_path: str | None) -> bool:
    if not remote_path or not SAFE_REMOTE_PATH.fullmatch(remote_path):
        return False
    remote_tail = remote_path[2:] if remote_path.startswith("~/") else remote_path
    return not any(part in {".", ".."} for part in remote_tail.split("/"))


def _parse_ssh_port(value: str | None) -> int:
    if not value:
        raise RunnerError("SSH_PORT is required.")
    try:
        port = int(value)
    except ValueError as exc:
        raise RunnerError("SSH_PORT must be an integer.") from exc
    if not 1 <= port <= 65535:
        raise RunnerError("SSH_PORT is outside the valid range.")
    return port


DOCTOR_PHP = r'''
return array(
    "home_url" => home_url(),
    "wordpress_version" => get_bloginfo("version"),
    "siteground_optimizer_active" => (
        defined("SG_CACHEPRESS_VERSION") ||
        function_exists("sg_cachepress_purge_everything")
    ),
);
'''.strip()

INVENTORY_PHP = r'''
if (!function_exists("get_plugins")) {
    require_once ABSPATH . "wp-admin/includes/plugin.php";
}
$active_plugins = array_flip((array) get_option("active_plugins", array()));
$network_plugins = is_multisite()
    ? (array) get_site_option("active_sitewide_plugins", array())
    : array();
$plugin_state = get_site_transient("update_plugins");
$theme_state = get_site_transient("update_themes");
$core_state = get_site_transient("update_core");
$update_checked_at = array("core" => null, "plugins" => null, "themes" => null);
foreach (array("plugins" => $plugin_state, "themes" => $theme_state, "core" => $core_state) as $name => $state) {
    if (is_object($state) && isset($state->last_checked) && is_numeric($state->last_checked)) {
        $update_checked_at[$name] = (int) $state->last_checked;
    }
}

$plugins = array();
$plugin_updates = array();
foreach (get_plugins() as $file => $details) {
    $status = isset($network_plugins[$file])
        ? "network-active"
        : (isset($active_plugins[$file]) ? "active" : "inactive");
    $plugin_id = explode("/", $file)[0];
    $plugins[] = array(
        "id" => $plugin_id,
        "file" => $file,
        "name" => isset($details["Name"]) ? $details["Name"] : $file,
        "status" => $status,
        "version" => isset($details["Version"]) ? $details["Version"] : "",
    );
    if (is_object($plugin_state) && isset($plugin_state->response[$file])) {
        $candidate = $plugin_state->response[$file];
        $plugin_updates[] = array(
            "id" => $plugin_id,
            "file" => $file,
            "version" => isset($details["Version"]) ? $details["Version"] : "",
            "update_version" => isset($candidate->new_version) ? $candidate->new_version : "",
        );
    }
}

$themes = array();
$theme_updates = array();
$active_theme = get_stylesheet();
foreach (wp_get_themes() as $slug => $theme) {
    $themes[] = array(
        "id" => $slug,
        "name" => $slug,
        "status" => ($slug === $active_theme) ? "active" : "inactive",
        "version" => $theme->get("Version"),
    );
    if (is_object($theme_state) && isset($theme_state->response[$slug])) {
        $candidate = $theme_state->response[$slug];
        $theme_updates[] = array(
            "id" => $slug,
            "name" => $slug,
            "version" => $theme->get("Version"),
            "update_version" => isset($candidate["new_version"]) ? $candidate["new_version"] : "",
        );
    }
}

$core_updates = array();
if (is_object($core_state) && isset($core_state->updates) && is_array($core_state->updates)) {
    foreach ($core_state->updates as $candidate) {
        if (!is_object($candidate) || !isset($candidate->response) || $candidate->response === "latest") {
            continue;
        }
        $core_updates[] = array(
            "response" => $candidate->response,
            "update_version" => isset($candidate->current) ? $candidate->current : "",
        );
    }
}

return array(
    "home_url" => home_url(),
    "plugins" => $plugins,
    "themes" => $themes,
    "updates" => array(
        "core" => $core_updates,
        "plugins" => $plugin_updates,
        "themes" => $theme_updates,
    ),
        "update_checked_at" => $update_checked_at,
);
'''.strip()


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        process.wait()
    except ChildProcessError:
        pass


def _run_bounded_command(
    command: list[str],
    *,
    capture_output: bool,
    text: bool,
    timeout: float,
    check: bool,
    shell: bool,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    if not capture_output or not text or check or shell:
        raise RunnerError("Novamira bridge execution requires the fixed bounded subprocess contract.")
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        start_new_session=True,
        env=env,
    )
    if process.stdout is None or process.stderr is None:
        _terminate_process_group(process)
        raise RunnerError("Novamira bridge streams could not be opened.")

    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, ("stdout", MAX_NOVAMIRA_OUTPUT_BYTES))
    selector.register(process.stderr, selectors.EVENT_READ, ("stderr", MAX_NOVAMIRA_ERROR_BYTES))
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout
    returncode: int | None = None
    process_group_terminated = False
    try:
        while selector.get_map():
            if returncode is None:
                returncode = process.poll()
            if returncode is not None and not process_group_terminated:
                _terminate_process_group(process)
                process_group_terminated = True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_process_group(process)
                raise subprocess.TimeoutExpired(command, timeout)
            events = selector.select(min(remaining, 0.1))
            if not events:
                continue
            for key, _ in events:
                name, limit = key.data
                target = buffers[name]
                chunk = os.read(key.fd, min(65_536, limit - len(target) + 1))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                target.extend(chunk)
                if len(target) > limit:
                    _terminate_process_group(process)
                    raise RunnerError("Novamira bridge output limit exceeded.")

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate_process_group(process)
            raise subprocess.TimeoutExpired(command, timeout)
        if returncode is None:
            returncode = process.wait(timeout=remaining)
        if returncode != 0 and not process_group_terminated:
            _terminate_process_group(process)
    except BaseException:
        _terminate_process_group(process)
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()

    return subprocess.CompletedProcess(
        command,
        returncode,
        stdout=bytes(buffers["stdout"]).decode("utf-8", errors="replace"),
        stderr=bytes(buffers["stderr"]).decode("utf-8", errors="replace"),
    )


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _runtime_tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    try:
        resolved_root = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RunnerError("Pinned Novamira MCP runtime root is unavailable.") from exc
    paths = sorted(root.rglob("*"), key=lambda value: value.relative_to(root).as_posix())
    for path in paths:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        stat = path.lstat()
        if path.is_symlink():
            try:
                path.resolve(strict=True).relative_to(resolved_root)
            except ValueError as exc:
                raise RunnerError("Pinned Novamira MCP runtime symlink points outside the verified tree.") from exc
            except (OSError, RuntimeError) as exc:
                raise RunnerError("Pinned Novamira MCP runtime contains an invalid symlink.") from exc
            digest.update(b"L\0" + relative + b"\0" + os.readlink(path).encode("utf-8") + b"\n")
            continue
        if not path.is_file():
            continue
        digest.update(b"F\0" + relative + b"\0" + str(stat.st_mode & 0o777).encode("ascii") + b"\0")
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(65_536), b""):
                digest.update(chunk)
        digest.update(b"\n")
    return digest.hexdigest()


def _runtime_manifest_path() -> Path:
    configured = os.getenv(NOVAMIRA_RUNTIME_ENV, "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_NOVAMIRA_RUNTIME_MANIFEST


def _load_pinned_novamira_runtime() -> list[str]:
    manifest_path = _runtime_manifest_path()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RunnerError(f"A pinned Novamira MCP runtime manifest is required: {manifest_path}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise RunnerError("Pinned Novamira MCP runtime manifest has an unsupported schema.")
    if manifest.get("package_name") != "@automattic/mcp-wordpress-remote":
        raise RunnerError("Pinned Novamira MCP runtime package identity is invalid.")
    if manifest.get("package_version") != PINNED_NOVAMIRA_MCP_VERSION:
        raise RunnerError(
            f"Pinned Novamira MCP runtime must be version {PINNED_NOVAMIRA_MCP_VERSION}."
        )
    command = manifest.get("command")
    runtime_root_value = manifest.get("runtime_root")
    expected_hash = manifest.get("tree_sha256")
    if (
        not isinstance(command, list)
        or len(command) != 2
        or not all(isinstance(value, str) and value for value in command)
        or not isinstance(runtime_root_value, str)
        or not isinstance(expected_hash, str)
    ):
        raise RunnerError("Pinned Novamira MCP runtime manifest is incomplete.")
    runtime_root = Path(runtime_root_value).expanduser()
    node_path = Path(command[0]).expanduser()
    proxy_path = Path(command[1]).expanduser()
    package_root = runtime_root / "node_modules" / "@automattic" / "mcp-wordpress-remote"
    package_json_path = package_root / "package.json"
    expected_proxy = package_root / "dist" / "proxy.js"
    if (
        not runtime_root.is_absolute()
        or not runtime_root.is_dir()
        or not node_path.is_absolute()
        or not node_path.is_file()
        or not os.access(node_path, os.X_OK)
        or not proxy_path.is_absolute()
        or not proxy_path.is_file()
        or proxy_path != expected_proxy
    ):
        raise RunnerError("Pinned Novamira MCP runtime files are unavailable.")
    if proxy_path.is_symlink() or package_json_path.is_symlink():
        raise RunnerError("Pinned Novamira MCP proxy and package metadata must be regular files.")
    try:
        package = json.loads(package_json_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RunnerError("Pinned Novamira MCP package metadata is unreadable.") from exc
    if (
        not isinstance(package, dict)
        or package.get("name") != "@automattic/mcp-wordpress-remote"
        or package.get("version") != PINNED_NOVAMIRA_MCP_VERSION
    ):
        raise RunnerError("Pinned Novamira MCP package metadata does not match the reviewed version.")
    if _runtime_tree_hash(runtime_root) != expected_hash:
        raise RunnerError("Pinned Novamira MCP runtime integrity does not match its owner manifest.")
    return [str(node_path), str(proxy_path)]


def _novamira_config_paths() -> tuple[Path, ...]:
    configured = os.getenv("NOVAMIRA_MCP_CONFIG", "").strip()
    return (Path(configured).expanduser(),) if configured else NOVAMIRA_CONFIG_PATHS


def _minimal_novamira_bridge_environment() -> dict[str, str]:
    environment = {
        name: os.environ[name]
        for name in NOVAMIRA_BRIDGE_ENV_ALLOWLIST
        if os.environ.get(name)
    }
    environment.setdefault("HOME", str(Path.home()))
    environment.setdefault("PATH", os.defpath)
    return environment


def _novamira_server_configured(server_name: str | None) -> bool:
    if not server_name:
        return False
    for path in _novamira_config_paths():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, PermissionError, UnicodeError, json.JSONDecodeError, OSError):
            continue
        servers = data.get("mcpServers") if isinstance(data, dict) else None
        server = servers.get(server_name) if isinstance(servers, dict) else None
        values = server.get("env") if isinstance(server, dict) else None
        if isinstance(values, dict) and all(isinstance(values.get(key), str) and values[key] for key in NOVAMIRA_REQUIRED_ENV):
            return True
    return False


def _novamira_local_readiness_issues(site: SiteConfig) -> list[str]:
    issues: list[str] = []
    if not site.novamira_server:
        issues.append("novamira_server")
    configured_path = os.getenv("NOVAMIRA_WP_OPS", "").strip()
    script_path = Path(configured_path).expanduser() if configured_path else DEFAULT_NOVAMIRA_WP_OPS
    if not script_path.is_absolute() or not script_path.is_file():
        issues.append("novamira_bridge")
    try:
        _load_pinned_novamira_runtime()
    except RunnerError:
        issues.append("novamira_runtime")
    if not _novamira_server_configured(site.novamira_server):
        issues.append("novamira_server_credentials")
    return issues


def read_transport_status(site: SiteConfig) -> dict[str, Any]:
    missing: dict[str, list[str]] = {}
    available: list[str] = []
    ssh_issues = ssh_local_readiness_issues(site)
    if not ssh_issues:
        available.append("ssh")
    else:
        missing["ssh"] = ssh_issues
    novamira_issues = _novamira_local_readiness_issues(site)
    if not novamira_issues:
        available.append("novamira")
    else:
        missing["novamira"] = novamira_issues
    return {"available": available, "missing": missing}


class ParamikoWpCliRunner:
    def __init__(self, site: SiteConfig, *, paramiko_module: ModuleType | Any | None = None) -> None:
        if not _remote_path_is_safe(site.remote_path):
            raise RunnerError("Unsafe remote_path; only a simple absolute or home-relative path is allowed.")
        self.site = site
        if paramiko_module is None:
            try:
                import paramiko as paramiko_module  # type: ignore[no-redef]
            except ImportError as exc:
                raise RunnerError("Paramiko is required for the SSH adapter.") from exc
        self.paramiko = paramiko_module

    def _remote_prefix(self) -> str:
        if self.site.remote_path.startswith("~/"):
            tail = self.site.remote_path[2:]
            return f'cd "$HOME"/{shlex.quote(tail)} && '
        return f"cd {shlex.quote(self.site.remote_path)} && "

    def _connect(self) -> Any:
        if self.site.env_file is None or self.site.key_file is None:
            raise RunnerError("SSH credential owner paths are not configured.")
        try:
            env = read_env_file(self.site.env_file)
        except OSError as exc:
            raise RunnerError(f"Could not read credential owner: {exc}") from exc
        required = ["SSH_HOST", "SSH_USER", "SSH_PORT", "SSH_CONFIRMED_PASS"]
        missing = [name for name in required if not env.get(name)]
        if missing:
            raise RunnerError(f"Credential owner is missing required names: {', '.join(missing)}")
        port = _parse_ssh_port(env.get("SSH_PORT"))

        client = self.paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(self.paramiko.RejectPolicy())
        try:
            key = self.paramiko.Ed25519Key.from_private_key_file(
                str(self.site.key_file), password=env["SSH_CONFIRMED_PASS"]
            )
            client.connect(
                hostname=env["SSH_HOST"],
                port=port,
                username=env["SSH_USER"],
                pkey=key,
                allow_agent=False,
                look_for_keys=False,
                timeout=20,
                banner_timeout=20,
                auth_timeout=20,
            )
        except Exception:
            client.close()
            raise
        return client

    def _run_many(self, commands: list[tuple[str, list[str], bool]]) -> dict[str, str | None]:
        client = self._connect()
        results: dict[str, str | None] = {}
        try:
            for name, tokens, optional in commands:
                command = self._remote_prefix() + shlex.join(["wp", *tokens])
                _, stdout, stderr = client.exec_command(command, timeout=45)
                status = stdout.channel.recv_exit_status()
                output = stdout.read().decode("utf-8", errors="replace").strip()
                error = stderr.read().decode("utf-8", errors="replace").strip()
                if status != 0:
                    if optional:
                        # Only `core check-update` documents exit 1 as no updates.
                        results[name] = "[]" if name == "core_updates" and status == 1 else None
                        continue
                    detail = redact(error or output or f"exit {status}")
                    raise RunnerError(f"WP-CLI {name} failed: {detail}")
                results[name] = output
        finally:
            client.close()
        return results

    def doctor(self) -> dict[str, Any]:
        values = self._run_many(
            [
                ("wp_cli_version", ["--version"], False),
                ("wordpress_version", ["core", "version"], False),
                ("home_url", ["option", "get", "home"], False),
                ("siteground_cache_cli", ["sg", "--help"], True),
            ]
        )
        return {
            "home_url": values["home_url"],
            "siteground_cache_cli": values["siteground_cache_cli"] is not None,
            "wordpress_version": values["wordpress_version"],
            "wp_cli_version": values["wp_cli_version"],
        }

    @staticmethod
    def _json_or_empty(value: str | None) -> Any:
        if not value:
            return []
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise RunnerError(f"Expected JSON from read-only WP-CLI command: {exc.msg}") from exc

    def inventory(self) -> dict[str, Any]:
        values = self._run_many(
            [
                ("plugins", ["plugin", "list", "--format=json"], False),
                ("themes", ["theme", "list", "--format=json"], False),
                ("core_updates", ["core", "check-update", "--format=json"], True),
                ("plugin_updates", ["plugin", "list", "--update=available", "--format=json"], True),
                ("theme_updates", ["theme", "list", "--update=available", "--format=json"], True),
            ]
        )
        failed_optional = [
            name for name in ("core_updates", "plugin_updates", "theme_updates") if values[name] is None
        ]
        if failed_optional:
            raise RunnerError(f"WP-CLI update checks unavailable: {', '.join(failed_optional)}")
        plugins = [NovamiraMcpRunner._normalize_plugin(row) for row in self._json_or_empty(values["plugins"])]
        themes = [NovamiraMcpRunner._normalize_theme(row) for row in self._json_or_empty(values["themes"])]
        return {
            "plugins": plugins,
            "themes": themes,
            "updates": {
                "core": self._json_or_empty(values["core_updates"]),
                "plugins": [
                    NovamiraMcpRunner._normalize_plugin_update(row)
                    for row in self._json_or_empty(values["plugin_updates"])
                ],
                "themes": [
                    NovamiraMcpRunner._normalize_theme_update(row)
                    for row in self._json_or_empty(values["theme_updates"])
                ],
            },
            "update_checked_at": {"core": None, "plugins": None, "themes": None},
            "update_source": "wp_cli_live_checks",
        }

    def purge_cache(self, request_id: str) -> dict[str, Any]:
        before = self._run_many([("home_url", ["option", "get", "home"], False)])["home_url"]
        if self._normalized_origin(before) != self._normalized_origin(self.site.public_url):
            raise RunnerError("Remote WordPress home URL does not match the configured public_url.")
        values = self._run_many([("cache_purge", ["sg", "purge"], False)])
        after = self._run_many([("home_url", ["option", "get", "home"], False)])["home_url"]
        if self._normalized_origin(after) != self._normalized_origin(self.site.public_url):
            raise RunnerError("Post-purge WordPress home URL no longer matches the configured public_url.")
        request = urllib.request.Request(
            self.site.public_url + "/",
            headers={"User-Agent": "siteground-ops/0.1 readback"},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            status = response.status
            cache_header = response.headers.get("x-proxy-cache")
            final_url = response.geturl()
            response.read(1024)
        if not 200 <= status < 400:
            raise RunnerError(f"Public readback returned HTTP {status}.")
        if self._normalized_origin(final_url) != self._normalized_origin(self.site.public_url):
            raise RunnerError("Public readback redirected to a different origin.")
        return {
            "command": "wp sg purge",
            "command_output": values["cache_purge"],
            "request_id": request_id,
            "readback": {
                "home_url": after,
                "http_status": status,
                "x_proxy_cache": cache_header,
            },
        }

    @staticmethod
    def _normalized_origin(value: str | None) -> str:
        if not value:
            return ""
        from urllib.parse import urlsplit

        parsed = urlsplit(value.strip())
        return f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}{parsed.path.rstrip('/') or '/'}"


class NovamiraMcpRunner:
    def __init__(
        self,
        site: SiteConfig,
        *,
        script_path: Path | None = None,
        run_command: Callable[..., Any] | None = None,
        mcp_runner_command: list[str] | None = None,
    ) -> None:
        if not site.novamira_server:
            raise RunnerError("Site has no configured novamira_server.")
        configured_path = os.getenv("NOVAMIRA_WP_OPS", "").strip()
        self.script_path = (
            Path(configured_path).expanduser()
            if configured_path
            else script_path or DEFAULT_NOVAMIRA_WP_OPS
        )
        if not self.script_path.is_absolute():
            raise RunnerError("NOVAMIRA_WP_OPS must resolve to an absolute path.")
        self.site = site
        self.run_command = run_command or _run_bounded_command
        self.mcp_runner_command = mcp_runner_command

    def _execute_php(self, code: str) -> dict[str, Any]:
        command = [
            sys.executable,
            str(self.script_path),
            "php",
            code,
            "--server",
            self.site.novamira_server,
        ]
        try:
            environment = None
            if self.mcp_runner_command:
                environment = _minimal_novamira_bridge_environment()
                environment["NOVAMIRA_MCP_RUNNER"] = shlex.join(self.mcp_runner_command)
            result = self.run_command(
                command,
                capture_output=True,
                text=True,
                timeout=NOVAMIRA_TIMEOUT_SECONDS,
                check=False,
                shell=False,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            raise RunnerError("Novamira read timed out before a valid receipt was returned.") from exc
        except OSError as exc:
            raise RunnerError("Novamira bridge could not be started from its configured owner path.") from exc

        if result.returncode != 0:
            raise RunnerError("Novamira bridge refused or failed the exact-site read.")
        stdout = result.stdout
        if not isinstance(stdout, str) or len(stdout.encode("utf-8")) > MAX_NOVAMIRA_OUTPUT_BYTES:
            raise RunnerError("Novamira returned an invalid or oversized read payload.")
        try:
            response = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise RunnerError("Novamira returned malformed JSON for the read operation.") from exc
        if not isinstance(response, dict) or response.get("success") is not True:
            raise RunnerError("Novamira did not return an explicit success response.")
        data = response.get("data")
        if not isinstance(data, dict) or data.get("success") is not True:
            raise RunnerError("Novamira returned an ambiguous ability result.")
        value = data.get("return_value")
        if not isinstance(value, dict):
            raise RunnerError("Novamira returned an ambiguous read payload.")
        self._require_site_identity(value.get("home_url"))
        return value

    def _require_site_identity(self, home_url: Any) -> None:
        if not isinstance(home_url, str):
            raise RunnerError("Novamira home_url identity is missing or invalid.")
        if ParamikoWpCliRunner._normalized_origin(home_url) != ParamikoWpCliRunner._normalized_origin(
            self.site.public_url
        ):
            raise RunnerError("Novamira home_url does not match the configured public_url.")

    def doctor(self) -> dict[str, Any]:
        value = self._execute_php(DOCTOR_PHP)
        wordpress_version = value.get("wordpress_version")
        optimizer_active = value.get("siteground_optimizer_active")
        if not isinstance(wordpress_version, str) or not isinstance(optimizer_active, bool):
            raise RunnerError("Novamira returned an incomplete doctor payload.")
        return {
            "home_url": value["home_url"],
            "siteground_cache_cli": None,
            "siteground_optimizer_active": optimizer_active,
            "wordpress_version": wordpress_version,
            "wp_cli_version": None,
        }

    def inventory(self) -> dict[str, Any]:
        value = self._execute_php(INVENTORY_PHP)
        plugins = value.get("plugins")
        themes = value.get("themes")
        updates = value.get("updates")
        update_checked_at = value.get("update_checked_at")
        if not isinstance(plugins, list) or not isinstance(themes, list) or not isinstance(updates, dict):
            raise RunnerError("Novamira returned an incomplete inventory payload.")
        if not all(isinstance(updates.get(name), list) for name in ("core", "plugins", "themes")):
            raise RunnerError("Novamira returned an incomplete updates payload.")
        if not isinstance(update_checked_at, dict) or not all(
            name in update_checked_at and (update_checked_at[name] is None or isinstance(update_checked_at[name], int))
            for name in ("core", "plugins", "themes")
        ):
            raise RunnerError("Novamira returned an invalid cached-update timestamp.")
        normalized_plugins = [self._normalize_plugin(row) for row in plugins]
        normalized_themes = [self._normalize_theme(row) for row in themes]
        normalized_updates = {
            "core": list(updates["core"]),
            "plugins": [self._normalize_plugin_update(row) for row in updates["plugins"]],
            "themes": [self._normalize_theme_update(row) for row in updates["themes"]],
        }
        return {
            "plugins": normalized_plugins,
            "themes": normalized_themes,
            "updates": normalized_updates,
            "update_checked_at": update_checked_at,
            "update_source": "wordpress_cached_transients",
        }

    @staticmethod
    def _plugin_id(file: str | None, fallback: str | None = None) -> str:
        if isinstance(file, str) and file:
            return file.split("/", 1)[0].removesuffix(".php")
        if isinstance(fallback, str) and fallback:
            return fallback
        raise RunnerError("Novamira returned an inventory item without a stable plugin id.")

    @classmethod
    def _normalize_plugin(cls, row: Any) -> dict[str, Any]:
        if not isinstance(row, dict):
            raise RunnerError("Novamira returned an invalid plugin inventory item.")
        record = dict(row)
        record["id"] = cls._plugin_id(record.get("file"), record.get("id") or record.get("name"))
        record.setdefault("file", None)
        record.setdefault("name", record["id"])
        return record

    @staticmethod
    def _normalize_theme(row: Any) -> dict[str, Any]:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not row["name"]:
            raise RunnerError("Novamira returned an invalid theme inventory item.")
        record = dict(row)
        record["id"] = record.get("id") or record["name"]
        return record

    @classmethod
    def _normalize_plugin_update(cls, row: Any) -> dict[str, Any]:
        return cls._normalize_plugin(row)

    @staticmethod
    def _normalize_theme_update(row: Any) -> dict[str, Any]:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not row["name"]:
            raise RunnerError("Novamira returned an invalid theme update item.")
        record = dict(row)
        record["id"] = record.get("id") or record["name"]
        return record


def build_novamira_runner(site: SiteConfig) -> NovamiraMcpRunner:
    runner = NovamiraMcpRunner(site)
    if not runner.script_path.is_file():
        raise RunnerError("Novamira wp_ops.py owner path is unavailable.")
    issues = _novamira_local_readiness_issues(site)
    if issues:
        raise RunnerError(f"Novamira read transport is not ready; missing: {', '.join(issues)}")
    return NovamiraMcpRunner(site, mcp_runner_command=_load_pinned_novamira_runtime())


def ssh_local_readiness_issues(site: SiteConfig) -> list[str]:
    if site.adapter != "paramiko_wpcli":
        return ["adapter"]
    issues = list(site.ssh_missing)
    if "remote_path" not in issues and not _remote_path_is_safe(site.remote_path):
        issues.append("remote_path")
    if issues:
        return issues
    try:
        env = read_env_file(site.env_file)
    except (OSError, UnicodeError):
        return ["env_file"]
    required = ("SSH_HOST", "SSH_USER", "SSH_PORT", "SSH_CONFIRMED_PASS")
    issues.extend(name for name in required if not env.get(name))
    if env.get("SSH_PORT"):
        try:
            _parse_ssh_port(env["SSH_PORT"])
        except RunnerError:
            issues.append("SSH_PORT")
    return issues


def build_read_runner(site: SiteConfig, transport: str = "auto") -> tuple[str, Any]:
    if transport not in {"auto", "ssh", "novamira"}:
        raise RunnerError(f"Unsupported read transport: {transport}")
    if transport == "ssh":
        issues = ssh_local_readiness_issues(site)
        if issues:
            raise RunnerError(f"SSH read transport is not locally ready; missing: {', '.join(issues)}")
        return "ssh", build_runner(site)
    if transport == "novamira":
        return "novamira", build_novamira_runner(site)

    ssh_issues = ssh_local_readiness_issues(site)
    if not ssh_issues:
        return "ssh", build_runner(site)
    if not _novamira_local_readiness_issues(site):
        return "novamira", build_novamira_runner(site)
    raise RunnerError(
        "No read transport is locally ready; SSH pointers are incomplete and novamira_server is not configured."
    )


def build_runner(site: SiteConfig) -> ParamikoWpCliRunner:
    if site.adapter != "paramiko_wpcli":
        raise RunnerError(f"Unsupported adapter: {site.adapter}")
    if not site.ssh_ready:
        raise RunnerError(f"SSH transport is not ready; missing: {', '.join(site.ssh_missing)}")
    return ParamikoWpCliRunner(site)
