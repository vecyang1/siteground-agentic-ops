from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import siteground_ops.runner as runner_module
from siteground_ops.config import SiteConfig
from siteground_ops.runner import RunnerError


def site(
    tmp_path: Path,
    *,
    adapter: str = "paramiko_wpcli",
    novamira_server: str | None = "novamira-example",
) -> SiteConfig:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SSH_HOST=host.siteground.example\n"
        "SSH_USER=u123\n"
        "SSH_PORT=18765\n"
        "SSH_CONFIRMED_PASS=secret\n",
        encoding="utf-8",
    )
    key_file = tmp_path / "id_ed25519"
    key_file.write_text("fake-key", encoding="utf-8")
    return SiteConfig(
        site_id="prod",
        label="Production",
        environment="production",
        adapter=adapter,
        env_file=env_file,
        key_file=key_file,
        remote_path="~/www/example.com/public_html",
        credential_pointer="Project .env",
        recovery_pointer="Provider backup",
        public_url="https://example.com",
        novamira_server=novamira_server,
    )


def ability_response(return_value: object) -> str:
    return json.dumps(
        {
            "success": True,
            "data": {
                "success": True,
                "return_value": return_value,
            },
        }
    )


def test_novamira_doctor_uses_fixed_read_only_php_and_exact_server(tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=ability_response(
                {
                    "home_url": "https://example.com/",
                    "wordpress_version": "7.0.3",
                    "siteground_optimizer_active": True,
                }
            ),
            stderr="[wp-ops] target: private endpoint omitted",
        )

    runner = runner_module.NovamiraMcpRunner(
        site(tmp_path),
        script_path=tmp_path / "wp_ops.py",
        run_command=run,
    )

    result = runner.doctor()

    command, kwargs = calls[0]
    assert command[:3] == [sys.executable, str(tmp_path / "wp_ops.py"), "php"]
    assert command[-2:] == ["--server", "novamira-example"]
    assert "home_url()" in command[3]
    assert kwargs["shell"] is False
    assert kwargs["timeout"] == 90
    assert result == {
        "home_url": "https://example.com/",
        "siteground_cache_cli": None,
        "siteground_optimizer_active": True,
        "wordpress_version": "7.0.3",
        "wp_cli_version": None,
    }


def test_novamira_pinned_runtime_receives_minimal_allowlisted_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, str] = {}

    def run(_command: list[str], **kwargs: object) -> SimpleNamespace:
        captured.update(kwargs["env"])  # type: ignore[arg-type]
        return SimpleNamespace(
            returncode=0,
            stdout=ability_response(
                {
                    "home_url": "https://example.com",
                    "wordpress_version": "7.0.3",
                    "siteground_optimizer_active": True,
                }
            ),
            stderr="",
        )

    monkeypatch.setenv("UNRELATED_CLOUD_TOKEN", "must-not-reach-third-party-runtime")
    runner_module.NovamiraMcpRunner(
        site(tmp_path),
        script_path=tmp_path / "wp_ops.py",
        run_command=run,
        mcp_runner_command=[sys.executable, str(tmp_path / "proxy.js")],
    ).doctor()

    assert "UNRELATED_CLOUD_TOKEN" not in captured
    assert captured["NOVAMIRA_MCP_RUNNER"]
    assert captured["HOME"]
    assert captured["PATH"]


def test_novamira_inventory_labels_cached_update_evidence(tmp_path: Path) -> None:
    def run(_command: list[str], **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=0,
            stdout=ability_response(
                {
                    "home_url": "https://example.com",
                    "plugins": [{"file": "plugin/plugin.php", "name": "Plugin", "status": "active", "version": "1"}],
                    "themes": [{"name": "theme", "status": "active", "version": "1"}],
                    "updates": {"core": [], "plugins": [], "themes": []},
                    "update_checked_at": {
                        "core": 1786200000,
                        "plugins": 1786200000,
                        "themes": 1786200000,
                    },
                }
            ),
            stderr="",
        )

    result = runner_module.NovamiraMcpRunner(
        site(tmp_path),
        script_path=tmp_path / "wp_ops.py",
        run_command=run,
    ).inventory()

    assert result["update_source"] == "wordpress_cached_transients"
    assert result["update_checked_at"] == {
        "core": 1786200000,
        "plugins": 1786200000,
        "themes": 1786200000,
    }
    assert result["plugins"][0]["id"] == "plugin"
    assert result["plugins"][0]["name"] == "Plugin"


def test_novamira_inventory_requires_home_url_identity_parity(tmp_path: Path) -> None:
    def run(_command: list[str], **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=0,
            stdout=ability_response(
                {
                    "home_url": "https://wrong.example",
                    "plugins": [],
                    "themes": [],
                    "updates": {"core": [], "plugins": [], "themes": []},
                }
            ),
            stderr="",
        )

    runner = runner_module.NovamiraMcpRunner(
        site(tmp_path),
        script_path=tmp_path / "wp_ops.py",
        run_command=run,
    )

    with pytest.raises(RunnerError, match="home_url"):
        runner.inventory()


@pytest.mark.parametrize(
    "stdout",
    [
        "not-json",
        json.dumps({"success": False, "data": {}}),
        json.dumps({"success": True, "data": {"success": True, "return_value": "wrong-shape"}}),
    ],
)
def test_novamira_rejects_ambiguous_or_malformed_payloads(tmp_path: Path, stdout: str) -> None:
    def run(_command: list[str], **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    runner = runner_module.NovamiraMcpRunner(
        site(tmp_path),
        script_path=tmp_path / "wp_ops.py",
        run_command=run,
    )

    with pytest.raises(RunnerError, match="Novamira"):
        runner.doctor()


def test_novamira_timeout_does_not_echo_bridge_stderr(tmp_path: Path) -> None:
    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        raise subprocess.TimeoutExpired(command, 90, stderr="token=do-not-echo")

    runner = runner_module.NovamiraMcpRunner(
        site(tmp_path),
        script_path=tmp_path / "wp_ops.py",
        run_command=run,
    )

    with pytest.raises(RunnerError, match="timed out") as exc_info:
        runner.doctor()
    assert "do-not-echo" not in str(exc_info.value)


@pytest.mark.parametrize("stream_name", ["stdout", "stderr"])
def test_default_novamira_runner_terminates_oversized_bridge_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stream_name: str,
) -> None:
    script = tmp_path / "wp_ops.py"
    script.write_text(
        "import sys, time\n"
        f"stream = sys.{stream_name}.buffer\n"
        "stream.write(b'x' * 1_100_000)\n"
        "stream.flush()\n"
        "time.sleep(5)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner_module, "NOVAMIRA_TIMEOUT_SECONDS", 0.5)
    runner = runner_module.NovamiraMcpRunner(site(tmp_path), script_path=script)

    with pytest.raises(RunnerError, match="output limit"):
        runner.doctor()


def test_auto_uses_novamira_only_when_ssh_is_locally_unready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = site(tmp_path)
    config.key_file.unlink()
    sentinel = object()
    monkeypatch.setattr("siteground_ops.runner.build_novamira_runner", lambda _site: sentinel)
    monkeypatch.setattr("siteground_ops.runner._novamira_local_readiness_issues", lambda _site: [])
    monkeypatch.setattr(
        "siteground_ops.runner.build_runner",
        lambda _site: (_ for _ in ()).throw(AssertionError("SSH builder must not run")),
    )

    transport, runner = runner_module.build_read_runner(config, "auto")

    assert transport == "novamira"
    assert runner is sentinel


def test_auto_treats_malformed_ssh_owner_as_unready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = site(tmp_path)
    config.env_file.write_bytes(b"SSH_HOST=host\xff\n")
    sentinel = object()
    monkeypatch.setattr("siteground_ops.runner.build_novamira_runner", lambda _site: sentinel)
    monkeypatch.setattr("siteground_ops.runner._novamira_local_readiness_issues", lambda _site: [])

    transport, runner = runner_module.build_read_runner(config, "auto")

    assert transport == "novamira"
    assert runner is sentinel


def test_novamira_primary_profile_does_not_advertise_or_auto_select_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = site(tmp_path, adapter="novamira_mcp")
    sentinel = object()
    monkeypatch.setattr("siteground_ops.runner.build_novamira_runner", lambda _site: sentinel)
    monkeypatch.setattr("siteground_ops.runner._novamira_local_readiness_issues", lambda _site: [])
    monkeypatch.setattr(
        "siteground_ops.runner.build_runner",
        lambda _site: (_ for _ in ()).throw(AssertionError("SSH builder must not run")),
    )

    assert config.read_transports == ["novamira"]
    transport, runner = runner_module.build_read_runner(config, "auto")

    assert transport == "novamira"
    assert runner is sentinel


def test_auto_does_not_switch_after_ssh_was_selected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = site(tmp_path)

    class FailingSshRunner:
        def doctor(self) -> dict:
            raise RunnerError("host key changed")

    monkeypatch.setattr("siteground_ops.runner.build_runner", lambda _site: FailingSshRunner())
    monkeypatch.setattr(
        "siteground_ops.runner.build_novamira_runner",
        lambda _site: (_ for _ in ()).throw(AssertionError("runtime fallback is forbidden")),
    )

    transport, runner = runner_module.build_read_runner(config, "auto")

    assert transport == "ssh"
    with pytest.raises(RunnerError, match="host key changed"):
        runner.doctor()


def test_explicit_novamira_requires_configured_exact_server(tmp_path: Path) -> None:
    with pytest.raises(RunnerError, match="novamira_server"):
        runner_module.build_read_runner(site(tmp_path, novamira_server=None), "novamira")


def test_explicit_ssh_requires_complete_local_credential_owner(tmp_path: Path) -> None:
    config = site(tmp_path)
    config.env_file.write_text("SSH_HOST=host\n", encoding="utf-8")

    with pytest.raises(RunnerError, match="locally ready"):
        runner_module.build_read_runner(config, "ssh")


def test_build_novamira_runner_requires_pinned_runtime_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = tmp_path / "wp_ops.py"
    bridge.write_text("", encoding="utf-8")
    monkeypatch.setenv("NOVAMIRA_WP_OPS", str(bridge))
    monkeypatch.setenv("SITEGROUND_OPS_NOVAMIRA_RUNTIME", str(tmp_path / "missing.json"))

    with pytest.raises(RunnerError, match="novamira_runtime"):
        runner_module.build_novamira_runner(site(tmp_path))


def test_build_novamira_runner_accepts_only_verified_pinned_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = tmp_path / "wp_ops.py"
    bridge.write_text("", encoding="utf-8")
    runtime_root = tmp_path / "runtime"
    package_root = runtime_root / "node_modules" / "@automattic" / "mcp-wordpress-remote"
    (package_root / "dist").mkdir(parents=True)
    (package_root / "package.json").write_text(
        json.dumps({"name": "@automattic/mcp-wordpress-remote", "version": "0.3.5"}),
        encoding="utf-8",
    )
    proxy = package_root / "dist" / "proxy.js"
    proxy.write_text("", encoding="utf-8")
    manifest = tmp_path / "runtime.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "package_name": "@automattic/mcp-wordpress-remote",
                "package_version": "0.3.5",
                "runtime_root": str(runtime_root),
                "command": [sys.executable, str(proxy)],
                "tree_sha256": runner_module._runtime_tree_hash(runtime_root),
            }
        ),
        encoding="utf-8",
    )
    mcp_config = tmp_path / "mcp.json"
    mcp_config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "novamira-example": {
                        "env": {
                            "WP_API_URL": "https://example.com",
                            "WP_API_USERNAME": "user",
                            "WP_API_PASSWORD": "password",
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("NOVAMIRA_WP_OPS", str(bridge))
    monkeypatch.setenv("SITEGROUND_OPS_NOVAMIRA_RUNTIME", str(manifest))
    monkeypatch.setenv("NOVAMIRA_MCP_CONFIG", str(mcp_config))

    runner = runner_module.build_novamira_runner(site(tmp_path))

    assert runner.mcp_runner_command == [sys.executable, str(proxy)]
    proxy.write_text("tampered", encoding="utf-8")
    with pytest.raises(RunnerError, match="novamira_runtime"):
        runner_module.build_novamira_runner(site(tmp_path))


def test_readiness_does_not_advertise_missing_novamira_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVAMIRA_WP_OPS", str(tmp_path / "missing-wp-ops.py"))

    status = runner_module.read_transport_status(site(tmp_path))

    assert status["available"] == ["ssh"]
    assert "novamira_bridge" in status["missing"]["novamira"]
