from __future__ import annotations

from pathlib import Path

import pytest

from siteground_ops.config import SiteConfig
from siteground_ops.runner import ParamikoWpCliRunner, RunnerError, ssh_local_readiness_issues


def site(tmp_path: Path) -> SiteConfig:
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
        adapter="paramiko_wpcli",
        env_file=env_file,
        key_file=key_file,
        remote_path="~/www/example.com/public_html",
        credential_pointer="Project .env",
        recovery_pointer="Provider backup",
        public_url="https://example.com",
    )


def test_runner_uses_reject_policy_and_exact_read_commands(tmp_path: Path) -> None:
    calls: list[tuple[str, object]] = []

    class Stream:
        def __init__(self, value: str, status: int = 0) -> None:
            self.value = value
            self.channel = self
            self.status = status

        def read(self) -> bytes:
            return self.value.encode()

        def recv_exit_status(self) -> int:
            return self.status

    class Client:
        def load_system_host_keys(self) -> None:
            calls.append(("load_system_host_keys", True))

        def set_missing_host_key_policy(self, policy: object) -> None:
            calls.append(("policy", policy))

        def connect(self, **kwargs: object) -> None:
            calls.append(("connect", kwargs))

        def exec_command(self, command: str, timeout: int) -> tuple[None, Stream, Stream]:
            calls.append(("exec", command))
            outputs = {
                "wp --version": "WP-CLI 2.12.0",
                "wp core version": "6.9.1",
                "wp option get home": "https://example.com",
                "wp sg --help": "usage: wp sg purge",
            }
            for suffix, value in outputs.items():
                if command.endswith(suffix):
                    return None, Stream(value), Stream("")
            raise AssertionError(command)

        def close(self) -> None:
            calls.append(("close", True))

    class Key:
        @classmethod
        def from_private_key_file(cls, path: str, password: str) -> str:
            calls.append(("key", (path, password)))
            return "loaded-key"

    class Paramiko:
        SSHClient = Client
        Ed25519Key = Key

        class RejectPolicy:
            pass

    runner = ParamikoWpCliRunner(site(tmp_path), paramiko_module=Paramiko)

    evidence = runner.doctor()

    assert evidence == {
        "home_url": "https://example.com",
        "siteground_cache_cli": True,
        "wordpress_version": "6.9.1",
        "wp_cli_version": "WP-CLI 2.12.0",
    }
    assert calls[0] == ("load_system_host_keys", True)
    policy = next(value for name, value in calls if name == "policy")
    assert policy.__class__.__name__ == "RejectPolicy"
    connect = next(value for name, value in calls if name == "connect")
    assert connect["look_for_keys"] is False
    assert connect["allow_agent"] is False
    assert connect["hostname"] == "host.siteground.example"
    assert connect["port"] == 18765
    assert all("correct-horse" not in str(value) for _, value in calls)


def test_runner_rejects_unsafe_remote_path(tmp_path: Path) -> None:
    config = site(tmp_path)
    object.__setattr__(config, "remote_path", "~/www/example.com; rm -rf /")

    with pytest.raises(RunnerError, match="remote_path"):
        ParamikoWpCliRunner(config, paramiko_module=object())


def test_runner_rejects_dot_path_components(tmp_path: Path) -> None:
    config = site(tmp_path)
    object.__setattr__(config, "remote_path", "~/www/../etc")
    with pytest.raises(RunnerError, match="remote_path"):
        ParamikoWpCliRunner(config, paramiko_module=object())


@pytest.mark.parametrize("port", [None, "", "not-an-int", "0", "65536"])
def test_ssh_readiness_rejects_missing_or_invalid_port(tmp_path: Path, port: str | None) -> None:
    config = site(tmp_path)
    lines = [
        "SSH_HOST=host.siteground.example",
        "SSH_USER=u123",
        "SSH_CONFIRMED_PASS=secret",
    ]
    if port is not None:
        lines.append(f"SSH_PORT={port}")
    config.env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert "SSH_PORT" in ssh_local_readiness_issues(config)


@pytest.mark.parametrize("remote_path", [None, "", "../../unsafe", "~/www/../etc"])
def test_ssh_readiness_rejects_missing_or_unsafe_remote_path(
    tmp_path: Path, remote_path: str | None
) -> None:
    config = site(tmp_path)
    object.__setattr__(config, "remote_path", remote_path)

    assert "remote_path" in ssh_local_readiness_issues(config)


def test_runner_never_accepts_arbitrary_wpcli(tmp_path: Path) -> None:
    runner = ParamikoWpCliRunner(site(tmp_path), paramiko_module=object())
    assert not hasattr(runner, "run_arbitrary")
    assert not hasattr(runner, "shell")


def test_inventory_allowlist_contains_only_read_operations(tmp_path: Path) -> None:
    calls: list[str] = []

    class Stream:
        def __init__(self, value: str) -> None:
            self.value = value
            self.channel = self

        def read(self) -> bytes:
            return self.value.encode()

        def recv_exit_status(self) -> int:
            return 0

    class Client:
        def load_system_host_keys(self) -> None:
            pass

        def set_missing_host_key_policy(self, _policy: object) -> None:
            pass

        def connect(self, **_kwargs: object) -> None:
            pass

        def exec_command(self, command: str, timeout: int) -> tuple[None, Stream, Stream]:
            calls.append(command)
            if "plugin list" in command:
                return None, Stream('[{"name":"p","status":"active","version":"1.0"}]'), Stream("")
            if "theme list" in command:
                return None, Stream('[{"name":"t","status":"active","version":"1.0"}]'), Stream("")
            if "core check-update" in command:
                return None, Stream(""), Stream("")
            if "plugin update" in command or "theme update" in command:
                raise AssertionError("mutation reached inventory")
            raise AssertionError(command)

        def close(self) -> None:
            pass

    class Key:
        @classmethod
        def from_private_key_file(cls, path: str, password: str) -> str:
            return "loaded-key"

    class Paramiko:
        SSHClient = Client
        Ed25519Key = Key

        class RejectPolicy:
            pass

    result = ParamikoWpCliRunner(site(tmp_path), paramiko_module=Paramiko).inventory()

    assert result["plugins"][0]["id"] == "p"
    assert result["plugins"][0]["name"] == "p"
    assert result["themes"][0]["id"] == "t"
    assert result["themes"][0]["name"] == "t"
    assert result["update_source"] == "wp_cli_live_checks"
    assert all("plugin update " not in command and "theme update " not in command for command in calls)


def test_inventory_does_not_turn_update_check_errors_into_empty_lists(tmp_path: Path) -> None:
    config = site(tmp_path)

    class Stream:
        def __init__(self, value: str, status: int = 0) -> None:
            self.value = value
            self.channel = self
            self.status = status

        def read(self) -> bytes:
            return self.value.encode()

        def recv_exit_status(self) -> int:
            return self.status

    class Client:
        def load_system_host_keys(self): pass
        def set_missing_host_key_policy(self, _policy): pass
        def connect(self, **_kwargs): pass
        def close(self): pass
        def exec_command(self, command: str, timeout: int):
            if "plugin list --format=json" in command:
                return None, Stream("[]"), Stream("")
            if "theme list --format=json" in command:
                return None, Stream("[]"), Stream("")
            if "core check-update" in command:
                return None, Stream("permission denied", status=2), Stream("permission denied")
            if "plugin list --update=available" in command or "theme list --update=available" in command:
                return None, Stream("[]"), Stream("")
            raise AssertionError(command)

    class Key:
        @classmethod
        def from_private_key_file(cls, path: str, password: str): return "loaded-key"

    class Paramiko:
        SSHClient = Client
        Ed25519Key = Key
        class RejectPolicy: pass

    with pytest.raises(RunnerError, match="update checks unavailable"):
        ParamikoWpCliRunner(config, paramiko_module=Paramiko).inventory()


def test_inventory_does_not_treat_plugin_exit_one_as_no_updates(tmp_path: Path) -> None:
    config = site(tmp_path)

    class Stream:
        def __init__(self, value: str, status: int = 0) -> None:
            self.value = value
            self.channel = self
            self.status = status

        def read(self) -> bytes:
            return self.value.encode()

        def recv_exit_status(self) -> int:
            return self.status

    class Client:
        def load_system_host_keys(self): pass
        def set_missing_host_key_policy(self, _policy): pass
        def connect(self, **_kwargs): pass
        def close(self): pass
        def exec_command(self, command: str, timeout: int):
            if "plugin list --format=json" in command or "theme list --format=json" in command:
                return None, Stream("[]"), Stream("")
            if "core check-update" in command:
                return None, Stream("[]", status=1), Stream("")
            if "plugin list --update=available" in command:
                return None, Stream("permission denied", status=1), Stream("permission denied")
            if "theme list --update=available" in command:
                return None, Stream("[]"), Stream("")
            raise AssertionError(command)

    class Key:
        @classmethod
        def from_private_key_file(cls, path: str, password: str): return "loaded-key"

    class Paramiko:
        SSHClient = Client
        Ed25519Key = Key
        class RejectPolicy: pass

    with pytest.raises(RunnerError, match="update checks unavailable"):
        ParamikoWpCliRunner(config, paramiko_module=Paramiko).inventory()
