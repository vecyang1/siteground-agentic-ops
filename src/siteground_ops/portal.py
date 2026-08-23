from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode, urlsplit, urlunsplit

from .config import PORTAL_ID, PortalAccountConfig, SiteConfig
from .runner import RunnerError


class PortalError(RuntimeError):
    pass


class PortalUnknownOutcomeError(PortalError):
    """Raised when a portal write may or may not have taken effect.

    A WordPress autologin that times out mid-flight has already minted a
    single-use credential on the provider side. Retrying blindly mints another
    one, so the caller must read state back instead.
    """


PORTAL_READS: dict[str, tuple[str, bool]] = {
    "websites": ("websites", False),
    "hosting": ("hosting", False),
    "plan-sites": ("plan-sites", True),
    "statistics": ("statistics", True),
    "billing-methods": ("billing-methods", False),
    "payment-history": ("payment-history", False),
    "renewals": ("renewals", False),
    "wp-apps": ("wp-apps", False),
}
PORTAL_PLUGIN_NAME = "siteground"
WORDPRESS_LOGIN_COMMAND = "wp-login"
# Access is declared per command rather than assumed, so a command that silently
# changes from read to write cannot keep passing the readiness gate.
PORTAL_PLUGIN_ACCESS: dict[str, str] = {
    **{command: "read" for command, _requires_plan in PORTAL_READS.values()},
    WORDPRESS_LOGIN_COMMAND: "write",
}
PORTAL_PLUGIN_COMMANDS = frozenset(PORTAL_PLUGIN_ACCESS)
PORTAL_PLUGIN_SOURCE_SHA256 = {
    "_runtime.js": "81f5f4954c2e7797d23215cc981c736f6befc261eead64398283b1826b10c78d",
    "_schema.js": "d18ec76b1b94c988b5d555c8fdf2d937fb8d826ebf9f548948c9010a2ebfbae3",
    "_ui.js": "f92f4a8e23a846127ae5981ac67af1e8af13121712ab9c76f11db21391f7907e",
    "_wp.js": "113cc0fc5bf89fbac30ada9b0a1abe23b06ae1ef236318b23585a22b10a84ec6",
    "billing-methods.js": "ab9f4be6ac4106723c908f960ff4c7d17234f403701c86ff16735f0e6ce10f5f",
    "hosting.js": "38dda6d0171f53653cddd5b2f5490bfd55ab60bcc6fa69a71980fd6781c5d737",
    "payment-history.js": "db2820c1f16e0f9bafc4fd335d4c9b46dca06d452020e9015b8519e3ef0e0782",
    "plan-sites.js": "535f0a06fdfca283d2d83d21b40a128095fe0f4d8ce6efa84aa8eddb77983e41",
    "renewals.js": "e559e9cd62ac0116b033b8379f72264958092fbb61df51a3fcd427830e6c2357",
    "statistics.js": "29f164b0503ed91edde9137af3559a21dcde6ce84cc0fda4fa986f749ac4b592",
    "websites.js": "42fa4c794038fb35e4d59e9bef8fe62e741e57b2cc9845bd4486022bc1cb9a08",
    "wp-apps.js": "fdc7387d925da09b21d9c0d0a7b3de7ad873f66a2d26cc45300d8a15f0c18aca",
    "wp-login.js": "de48bc7873c507c5475c4e26670aa2d03600f5e11bfa9f9153490037ce64f7f4",
}

# Exactly the flags this controller is allowed to pass through to the plugin.
# Values are validated before they reach argv so no caller can reshape a request.
FIXED_COMMAND_OPTIONS: dict[str, re.Pattern[str]] = {
    "site-id": re.compile(r"[A-Za-z0-9_-]{8,128}"),
    "app": re.compile(r"[0-9]{1,6}"),
}
MAX_PORTAL_REASON_CHARS = 300
# Measured: the flow is portal load -> autologin mint -> a cold wp-admin render
# on shared hosting. 120s produced a correct-but-useless `unknown` on a login
# that had in fact succeeded, so the budget covers the slow dashboard instead.
# Three independent OpenCLI timers sit under one command; a budget that ignores
# any of them turns an attributable failure into a mystery.
#
#   1. Browser connect      OPENCLI_BROWSER_CONNECT_TIMEOUT, default 45s.
#   2. Whole browser command OPENCLI_BROWSER_COMMAND_TIMEOUT, default 60s. The
#      adapter command has no --timeout flag -- OpenCLI's own timeout hint names
#      one that does not parse -- so this variable is the only lever.
#   3. One browser action    DEFAULT_COMMAND_TIMEOUT_SECONDS, 120s, hardcoded in
#      daemon-client.js with no environment override. A single page.evaluate
#      cannot be given longer from out here at all.
#
# Measured: a healthy wp-login takes 30-40s end to end, so the 60s default in (2)
# left almost no slack and aborted mid-login. The budget below has to clear (3),
# because a wedged single action burns 120s before it can report -- and that
# report is the useful one. Past that ceiling more budget buys nothing but a
# longer wait, since no single action may exceed 120s regardless.
#
# The subprocess budget must in turn clear the browser budget plus the connect
# allowance, or this wrapper kills OpenCLI before it can name its own timeout.
OPENCLI_BROWSER_CONNECT_ALLOWANCE_SECONDS = 60.0
OPENCLI_BROWSER_ACTION_CEILING_SECONDS = 120
WORDPRESS_LOGIN_BROWSER_SECONDS = 180
WORDPRESS_READ_BROWSER_SECONDS = 90
PORTAL_READ_BROWSER_SECONDS = 45


def _subprocess_budget(browser_seconds: int) -> float:
    """The wrapper's own budget, always outside OpenCLI's.

    Derived rather than configured: two independently chosen numbers drift, and
    when they cross, this wrapper kills OpenCLI first and an attributable
    timeout is reported as "did not complete".
    """
    return browser_seconds + OPENCLI_BROWSER_CONNECT_ALLOWANCE_SECONDS


WORDPRESS_LOGIN_TIMEOUT_SECONDS = _subprocess_budget(WORDPRESS_LOGIN_BROWSER_SECONDS)
WORDPRESS_READ_TIMEOUT_SECONDS = _subprocess_budget(WORDPRESS_READ_BROWSER_SECONDS)
PORTAL_READ_ATTEMPTS = 2

SITE_TOOLS_ROUTES = {
    "dashboard": "dashboard",
    "file_manager": "filemanager",
    "backups": "backup-restore-manage",
    "cache": "cacher",
    "wordpress_management": "wp-manage",
}

_SAFE_LOCALE = re.compile(r"[A-Za-z0-9_.@-]{1,64}")
_SAFE_LOCALE_KEYS = ("LANG", "LC_ALL", "LC_CTYPE")
MAX_OPENCLI_OUTPUT_BYTES = 2_000_000
MAX_OPENCLI_ERROR_BYTES = 256_000
_SYSTEM_PATHS = (
    Path("/opt/homebrew/bin"),
    Path("/usr/local/bin"),
    Path("/usr/bin"),
    Path("/bin"),
    Path("/usr/sbin"),
    Path("/sbin"),
)


def _observed_at() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _system_home() -> Path:
    try:
        import pwd

        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError, OSError):
        return Path.home()


def _opencli_environment(
    opencli_path: Path,
    *,
    home: Path,
    browser_command_timeout: int | None = None,
) -> dict[str, str]:
    path_entries = [opencli_path.parent]
    resolved_opencli = opencli_path.resolve(strict=False)
    for parent in resolved_opencli.parents:
        node_dir = parent / "bin"
        node_path = node_dir / "node"
        if node_path.is_file() and os.access(node_path, os.X_OK):
            path_entries.append(node_dir)
            break
    path_entries.extend(_SYSTEM_PATHS)

    seen: set[str] = set()
    controlled_path: list[str] = []
    for entry in path_entries:
        value = str(entry)
        if value not in seen:
            seen.add(value)
            controlled_path.append(value)

    environment = {
        "HOME": str(home),
        "PATH": os.pathsep.join(controlled_path),
    }
    for key in _SAFE_LOCALE_KEYS:
        value = os.environ.get(key)
        if value and _SAFE_LOCALE.fullmatch(value):
            environment[key] = value
    if browser_command_timeout is not None:
        # The adapter command has no --timeout flag; OpenCLI's own hint names one
        # that does not parse. The environment variable is the only lever.
        environment["OPENCLI_BROWSER_COMMAND_TIMEOUT"] = str(int(browser_command_timeout))
    return environment


def _run_opencli_command(
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
        raise RunnerError("OpenCLI execution requires the fixed read-only subprocess contract.")

    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        env=env,
    )
    if process.stdout is None or process.stderr is None:
        process.kill()
        process.wait()
        raise RunnerError("OpenCLI output streams could not be opened.")

    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, ("stdout", MAX_OPENCLI_OUTPUT_BYTES))
    selector.register(process.stderr, selectors.EVENT_READ, ("stderr", MAX_OPENCLI_ERROR_BYTES))
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout

    def consume(key: selectors.SelectorKey) -> bool:
        name, limit = key.data
        try:
            chunk = os.read(key.fd, min(65_536, limit - len(buffers[name]) + 1))
        except BlockingIOError:
            return False
        if not chunk:
            try:
                selector.unregister(key.fileobj)
            except KeyError:
                pass
            return False
        buffers[name].extend(chunk)
        if len(buffers[name]) > limit:
            raise RunnerError("OpenCLI output limit exceeded.")
        return True

    try:
        while process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.wait()
                raise subprocess.TimeoutExpired(command, timeout)
            for key, _event in selector.select(min(remaining, 0.1)):
                consume(key)

        for key in list(selector.get_map().values()):
            os.set_blocking(key.fd, False)
        while selector.get_map():
            if not any(consume(key) for key in list(selector.get_map().values())):
                break

        return subprocess.CompletedProcess(
            command,
            process.returncode,
            stdout=buffers["stdout"].decode("utf-8", errors="replace"),
            stderr=buffers["stderr"].decode("utf-8", errors="replace"),
        )
    except BaseException:
        if process.poll() is None:
            process.kill()
            process.wait()
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()


def _error_envelope_message(stream: str) -> str | None:
    """Pull the message out of an OpenCLI JSON error envelope, if there is one."""
    text = (stream or "").strip()
    if not text.startswith("{"):
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict):
        for key in ("message", "hint", "code"):
            value = error.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    message = payload.get("message")
    return message.strip() if isinstance(message, str) and message.strip() else None


def _yaml_envelope_message(stream: str) -> str | None:
    """Pull the message out of OpenCLI's YAML error envelope.

    A failing adapter prints `ok: false` / `error:` / `  message: >-` regardless
    of --format, so the first non-empty line is the useless word "error:".
    """
    lines = (stream or "").splitlines()
    for index, line in enumerate(lines):
        if line.strip().startswith("message:"):
            inline = line.split("message:", 1)[1].strip().lstrip(">-|").strip()
            parts = [inline] if inline else []
            for continuation in lines[index + 1:]:
                if not continuation.startswith(" ") or continuation.strip().endswith(":"):
                    break
                if ":" in continuation and not continuation.strip().startswith("-"):
                    key = continuation.strip().split(":", 1)[0]
                    if key.isidentifier():
                        break
                parts.append(continuation.strip())
            joined = " ".join(part for part in parts if part).strip()
            if joined:
                return joined
    return None


_OPENCLI_TIMEOUT_TEXT = re.compile(r"timed out after\s+\d+\s*s", re.IGNORECASE)


def _is_opencli_timeout(result: subprocess.CompletedProcess[str]) -> bool:
    """Did OpenCLI abort the browser command on its own clock?

    This exits non-zero like any other failure, so without this check a timeout
    is indistinguishable from a refusal. The difference matters for a write: the
    browser may have completed the request that mints a credential before the
    abort, which makes the outcome unknown rather than failed.

    The structured code is the reliable witness; the message text is the fallback
    for an OpenCLI build that does not emit an envelope.
    """
    for stream in (result.stderr, result.stdout):
        text = (stream or "").strip()
        if text.startswith("{"):
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                error = payload.get("error")
                if isinstance(error, dict) and str(error.get("code", "")).upper() == "TIMEOUT":
                    return True
        if _OPENCLI_TIMEOUT_TEXT.search(text):
            return True
    return False


def _stderr_reason(result: subprocess.CompletedProcess[str], command: str) -> str:
    """Carry the adapter's own refusal text out to the operator.

    OpenCLI already explains an ambiguous application id or an expired portal
    session precisely. Dropping that for a generic status turns an actionable
    refusal into a mystery. A JSON error envelope is read for its message first,
    because its own first line is `{` and says nothing.
    """
    for stream in (result.stderr, result.stdout):
        envelope = _error_envelope_message(stream) or _yaml_envelope_message(stream)
        if envelope:
            return f"OpenCLI {command} failed: {envelope[:MAX_PORTAL_REASON_CHARS]}"
    for stream in (result.stderr, result.stdout):
        for line in (stream or "").splitlines():
            cleaned = line.strip().lstrip("\u2716\u2717x{").strip()
            if cleaned and cleaned not in ("}", '"ok": false', "ok: false"):
                return f"OpenCLI {command} failed: {cleaned[:MAX_PORTAL_REASON_CHARS]}"
    return f"OpenCLI {command} returned a non-zero status."


def _registration_result(
    status: str,
    *,
    available: bool,
    registered: bool,
    correctly_linked: bool,
    resolved: bool,
) -> dict[str, Any]:
    return {
        "available": available,
        "correctly_linked": correctly_linked,
        "ready": status == "ready",
        "registered": registered,
        "resolved": resolved,
        "status": status,
    }


def _read_plugin_lock(lock_path: Path) -> tuple[dict[str, Any] | None, bool]:
    if not lock_path.is_file():
        return None, False
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, True
    if not isinstance(payload, dict):
        return None, True
    if PORTAL_PLUGIN_NAME not in payload:
        return None, False
    entry = payload[PORTAL_PLUGIN_NAME]
    if not isinstance(entry, dict):
        return None, True
    return entry, False


def _lock_targets_adapter(entry: dict[str, Any] | None, adapter_dir: Path) -> bool:
    if not entry:
        return False
    source = entry.get("source")
    if not isinstance(source, dict) or source.get("kind") != "local":
        return False
    source_path = source.get("path")
    if not isinstance(source_path, str):
        return False
    return Path(source_path).expanduser().resolve(strict=False) == adapter_dir


def _source_has_fixed_commands(adapter_dir: Path) -> bool:
    try:
        adapter_root = adapter_dir.resolve(strict=True)
    except OSError:
        return False
    if not adapter_root.is_dir():
        return False

    try:
        source_paths = {path.name: path for path in adapter_root.iterdir() if path.suffix == ".js"}
    except OSError:
        return False
    if source_paths.keys() != PORTAL_PLUGIN_SOURCE_SHA256.keys():
        return False

    for name, expected_digest in PORTAL_PLUGIN_SOURCE_SHA256.items():
        path = source_paths[name]
        if path.is_symlink():
            return False
        try:
            resolved_path = path.resolve(strict=True)
            source = resolved_path.read_bytes()
        except OSError:
            return False
        if resolved_path.parent != adapter_root or not resolved_path.is_file():
            return False
        if hashlib.sha256(source).hexdigest() != expected_digest:
            return False
    return True


def _help_resolves_portal_command(
    result: subprocess.CompletedProcess[str], portal_command: str, access: str
) -> bool:
    expected_usage = f"Usage: opencli {PORTAL_PLUGIN_NAME} {portal_command} [options]"
    return (
        result.returncode == 0
        and not result.stderr.strip()
        and result.stdout.startswith(expected_usage)
        and f"Access: {access} | Browser: yes | Domain: siteground.com" in result.stdout
    )


def opencli_registration_status(
    opencli_path: Path,
    adapter_dir: Path,
    *,
    home: Path | None = None,
    run_command: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    opencli_path = opencli_path.expanduser()
    home = (home or _system_home()).expanduser().resolve()
    plugin_path = home / ".opencli" / "plugins" / PORTAL_PLUGIN_NAME
    lock_entry, malformed_lock = _read_plugin_lock(home / ".opencli" / "plugins.lock.json")
    link_present = os.path.lexists(plugin_path)
    registered = link_present or lock_entry is not None

    if not opencli_path.is_file() or not os.access(opencli_path, os.X_OK):
        return _registration_result(
            "unavailable",
            available=False,
            registered=registered,
            correctly_linked=False,
            resolved=False,
        )

    adapter_dir = adapter_dir.expanduser().resolve(strict=False)
    if not _source_has_fixed_commands(adapter_dir):
        return _registration_result(
            "source_invalid",
            available=True,
            registered=registered,
            correctly_linked=False,
            resolved=False,
        )

    if not link_present:
        status = "stale" if lock_entry is not None or malformed_lock else "unregistered"
        return _registration_result(
            status,
            available=True,
            registered=registered,
            correctly_linked=False,
            resolved=False,
        )
    if not plugin_path.is_symlink():
        return _registration_result(
            "wrong_target",
            available=True,
            registered=True,
            correctly_linked=False,
            resolved=False,
        )
    try:
        actual_target = plugin_path.resolve(strict=True)
    except OSError:
        return _registration_result(
            "stale",
            available=True,
            registered=True,
            correctly_linked=False,
            resolved=False,
        )
    if actual_target != adapter_dir:
        return _registration_result(
            "wrong_target",
            available=True,
            registered=True,
            correctly_linked=False,
            resolved=False,
        )
    if malformed_lock or not _lock_targets_adapter(lock_entry, adapter_dir):
        return _registration_result(
            "stale",
            available=True,
            registered=True,
            correctly_linked=True,
            resolved=False,
        )

    runner = run_command or _run_opencli_command
    environment = _opencli_environment(opencli_path, home=home)
    for portal_command in sorted(PORTAL_PLUGIN_COMMANDS):
        access = PORTAL_PLUGIN_ACCESS[portal_command]
        command = [str(opencli_path), PORTAL_PLUGIN_NAME, portal_command, "--help"]
        try:
            result = runner(
                command,
                capture_output=True,
                text=True,
                timeout=30.0,
                check=False,
                shell=False,
                env=environment,
            )
        except (OSError, RunnerError, subprocess.TimeoutExpired):
            return _registration_result(
                "unavailable",
                available=False,
                registered=True,
                correctly_linked=True,
                resolved=False,
            )
        if not _help_resolves_portal_command(result, portal_command, access):
            return _registration_result(
                "stale",
                available=True,
                registered=True,
                correctly_linked=True,
                resolved=False,
            )
    return _registration_result(
        "ready",
        available=True,
        registered=True,
        correctly_linked=True,
        resolved=True,
    )


def register_opencli_adapter(
    opencli_path: Path,
    adapter_dir: Path,
    *,
    home: Path | None = None,
    run_command: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    home = (home or _system_home()).expanduser().resolve()
    status = opencli_registration_status(
        opencli_path,
        adapter_dir,
        home=home,
        run_command=run_command,
    )
    if status["status"] != "unregistered":
        return {**status, "changed": False}

    runner = run_command or _run_opencli_command
    adapter_dir = adapter_dir.expanduser().resolve(strict=False)
    command = [str(opencli_path), "plugin", "install", adapter_dir.as_uri()]
    try:
        result = runner(
            command,
            capture_output=True,
            text=True,
            timeout=30.0,
            check=False,
            shell=False,
            env=_opencli_environment(opencli_path, home=home),
        )
    except (OSError, RunnerError, subprocess.TimeoutExpired):
        return {
            **_registration_result(
                "install_failed",
                available=True,
                registered=False,
                correctly_linked=False,
                resolved=False,
            ),
            "changed": False,
        }
    if result.returncode != 0:
        return {
            **_registration_result(
                "install_failed",
                available=True,
                registered=False,
                correctly_linked=False,
                resolved=False,
            ),
            "changed": False,
        }
    verified = opencli_registration_status(
        opencli_path,
        adapter_dir,
        home=home,
        run_command=run_command,
    )
    return {**verified, "changed": True}


class PortalOpenCliAdapter:
    def __init__(
        self,
        account: PortalAccountConfig,
        *,
        run_command: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        home: Path | None = None,
    ) -> None:
        self.account = account
        self.run_command = run_command or _run_opencli_command
        self.home = (home or _system_home()).expanduser().resolve()

    def _command(
        self,
        command: str,
        *,
        provider_plan_id: str | None = None,
        options: dict[str, str] | None = None,
        window: str = "background",
        site_session: str = "ephemeral",
    ) -> list[str]:
        if window not in ("background", "foreground"):
            raise PortalError("Invalid OpenCLI browser window mode.")
        if site_session not in ("ephemeral", "persistent"):
            raise PortalError("Invalid OpenCLI site session mode.")
        argv = [
            str(self.account.opencli_path),
            "--profile",
            self.account.opencli_profile,
            "siteground",
            command,
        ]
        if provider_plan_id is not None:
            if not PORTAL_ID.fullmatch(provider_plan_id):
                raise PortalError("Invalid provider plan id.")
            argv.extend(["--plan-id", provider_plan_id])
        for name, value in sorted((options or {}).items()):
            if name not in FIXED_COMMAND_OPTIONS:
                raise PortalError(f"Unsupported OpenCLI option {name!r}.")
            if not FIXED_COMMAND_OPTIONS[name].fullmatch(value):
                raise PortalError(f"Invalid value for OpenCLI option {name!r}.")
            argv.extend([f"--{name}", value])
        argv.extend([f"--site-session={site_session}", f"--window={window}", "--format=json"])
        return argv

    def _run(
        self,
        command: str,
        *,
        provider_plan_id: str | None = None,
        options: dict[str, str] | None = None,
        window: str = "background",
        site_session: str = "ephemeral",
        browser_timeout: int = PORTAL_READ_BROWSER_SECONDS,
        unknown_on_timeout: bool = False,
    ) -> list[dict[str, Any]]:
        argv = self._command(
            command,
            provider_plan_id=provider_plan_id,
            options=options,
            window=window,
            site_session=site_session,
        )
        timeout = _subprocess_budget(browser_timeout)
        try:
            result = self.run_command(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                shell=False,
                env=_opencli_environment(
                    self.account.opencli_path,
                    home=self.home,
                    browser_command_timeout=browser_timeout,
                ),
            )
        except subprocess.TimeoutExpired as exc:
            if unknown_on_timeout:
                raise PortalUnknownOutcomeError(
                    "OpenCLI portal write timed out; read state back before any retry."
                ) from exc
            raise PortalError("OpenCLI portal read did not complete.") from exc
        except (OSError, RunnerError) as exc:
            raise PortalError("OpenCLI portal read did not complete.") from exc
        if result.returncode != 0:
            reason = _stderr_reason(result, command)
            if unknown_on_timeout and _is_opencli_timeout(result):
                raise PortalUnknownOutcomeError(
                    f"{reason}. The browser may have completed the request before the "
                    "abort, so the outcome is unknown; read state back before any retry."
                )
            raise PortalError(reason)
        try:
            payload: Any = json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise PortalError("OpenCLI portal read returned malformed JSON.") from exc
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            payload = payload["data"]
        if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
            raise PortalError("OpenCLI portal read returned an invalid row contract.")
        return payload

    def _verify_account_identity(self, rows: list[dict[str, Any]]) -> None:
        observed = {
            str(row.get("domain", "")).strip().lower()
            for row in rows
            if isinstance(row.get("domain"), str)
        }
        missing = [domain for domain in self.account.expected_domains if domain not in observed]
        if missing:
            raise PortalError("SiteGround portal account identity did not match configured sentinels.")

    def read(self, section: str, *, provider_plan_id: str | None = None) -> dict[str, Any]:
        definition = PORTAL_READS.get(section)
        if definition is None:
            raise PortalError(f"Unsupported portal read: {section!r}.")
        command, requires_plan = definition
        if requires_plan and provider_plan_id is None:
            raise PortalError(f"Portal read {section!r} requires an exact provider plan id.")
        if not requires_plan and provider_plan_id is not None:
            raise PortalError(f"Portal read {section!r} does not accept a provider plan id.")

        if section == "websites":
            rows = self._run(command)
            self._verify_account_identity(rows)
        else:
            identity_rows = self._run("websites")
            self._verify_account_identity(identity_rows)
            rows = self._run(command, provider_plan_id=provider_plan_id)
        return {
            "account_id": self.account.account_id,
            "observed_at": _observed_at(),
            "rows": rows,
            "section": section,
            "transport": "opencli",
        }


    def _run_read_with_retry(self, command: str) -> list[dict[str, Any]]:
        """Run one read, retrying once when the browser bridge drops.

        Reads are idempotent, and the bridge intermittently loses its extension
        connection while a portal tab is warming up. One retry is bounded and
        safe here; it is deliberately not offered to `wp-login`, where a repeat
        mints a second single-use credential rather than repeating a query.
        """
        last: PortalError | None = None
        for _attempt in range(PORTAL_READ_ATTEMPTS):
            try:
                return self._run(command, browser_timeout=WORDPRESS_READ_BROWSER_SECONDS)
            except PortalError as exc:
                last = exc
        raise last if last is not None else PortalError(f"OpenCLI {command} did not run.")

    def wordpress_apps(self) -> dict[str, Any]:
        """Read the exact site and application ids, with account identity proven.

        Mirrors `read("wp-apps")` but with its own budget: this is the first leg
        of an interactive command, and the shared 45s read budget is not enough
        when the browser bridge is warming up a portal tab.
        """
        identity_rows = self._run_read_with_retry("websites")
        self._verify_account_identity(identity_rows)
        rows = self._run_read_with_retry("wp-apps")
        return {
            "account_id": self.account.account_id,
            "observed_at": _observed_at(),
            "rows": rows,
            "section": "wp-apps",
            "transport": "opencli",
        }

    def open_wordpress_admin(
        self,
        *,
        site_id: str,
        app_id: str | None = None,
        expected_domain: str,
        foreground: bool = False,
    ) -> dict[str, Any]:
        """Open one exact WordPress application in wp-admin.

        The provider mints a single-use administrator login. It is consumed
        inside the browser by the adapter, so it never reaches this process,
        stdout, or a receipt. Only the resulting wp-admin location is returned.

        The tab opens in the background by default so running this never steals
        the window the operator is using; pass foreground=True to raise it.
        """
        if not FIXED_COMMAND_OPTIONS["site-id"].fullmatch(site_id):
            raise PortalError("An exact non-secret SiteGround site id is required.")
        options = {"site-id": site_id}
        if app_id is not None:
            if not FIXED_COMMAND_OPTIONS["app"].fullmatch(app_id):
                raise PortalError("An exact numeric WordPress application id is required.")
            options["app"] = app_id

        rows = self._run(
            WORDPRESS_LOGIN_COMMAND,
            options=options,
            window="foreground" if foreground else "background",
            site_session="persistent",
            browser_timeout=WORDPRESS_LOGIN_BROWSER_SECONDS,
            unknown_on_timeout=True,
        )
        if len(rows) != 1:
            raise PortalUnknownOutcomeError(
                "OpenCLI wp-login returned an unexpected row count; read wp-admin state back before any retry."
            )
        row = rows[0]
        # `admin_host` is the host the login landed on. `domain` is the site
        # label the provider attaches to every application of that site, so it
        # cannot distinguish a production copy from its staging sibling.
        observed_host = str(row.get("admin_host", "")).strip().lower()
        if not observed_host or observed_host != expected_domain.lower():
            raise PortalUnknownOutcomeError(
                "OpenCLI wp-login reported a different host than the requested target."
            )
        if row.get("logged_in") is not True:
            raise PortalError("OpenCLI wp-login did not confirm a signed-in wp-admin document.")
        return {
            "account_id": self.account.account_id,
            "admin_host": observed_host,
            "app_id": str(row.get("app_id", "")),
            "credential_disclosed": False,
            "domain": str(row.get("domain", "")).strip().lower(),
            "logged_in": True,
            "observed_at": _observed_at(),
            "page_title": str(row.get("page_title", "")),
            "site_id": str(row.get("site_id", "")),
            "transport": "opencli",
            "window": "foreground" if foreground else "background",
            "wp_admin_url": str(row.get("wp_admin_url", "")),
        }


def build_portal_adapter(account: PortalAccountConfig) -> PortalOpenCliAdapter:
    return PortalOpenCliAdapter(account)


def site_tools_links(site: SiteConfig) -> dict[str, str]:
    if not site.portal_site_id or not PORTAL_ID.fullmatch(site.portal_site_id):
        raise PortalError("Site profile requires an exact portal_site_id.")
    parsed_public_url = urlsplit(site.public_url)
    if (
        parsed_public_url.scheme != "https"
        or not parsed_public_url.hostname
        or parsed_public_url.username
        or parsed_public_url.password
        or parsed_public_url.path not in ("", "/")
        or parsed_public_url.query
        or parsed_public_url.fragment
    ):
        raise PortalError("Site profile requires an exact HTTPS origin for public_url.")
    public_origin = urlunsplit(("https", parsed_public_url.netloc, "", "", ""))
    query = urlencode({"siteId": site.portal_site_id})
    links = {
        name: f"https://tools.siteground.com/{path}?{query}"
        for name, path in SITE_TOOLS_ROUTES.items()
    }
    links["wordpress_admin"] = f"{public_origin}/wp-admin/"
    return links
