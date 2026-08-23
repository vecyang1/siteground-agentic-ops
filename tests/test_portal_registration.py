from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from siteground_ops import portal
from siteground_ops.config import PortalAccountConfig, SiteConfig
from siteground_ops.portal import (
    PortalError,
    PortalOpenCliAdapter,
    opencli_registration_status,
    register_opencli_adapter,
    site_tools_links,
)
from siteground_ops.runner import RunnerError


ROOT = Path(__file__).resolve().parents[1]
ADAPTER_DIR = ROOT / "opencli" / "siteground"
REGISTRATION_SCRIPT = ROOT / "scripts" / "register-opencli-siteground.py"
PORTAL_COMMANDS = [
    "billing-methods",
    "hosting",
    "payment-history",
    "plan-sites",
    "renewals",
    "statistics",
    "websites",
    "wp-apps",
    "wp-login",
]


def portal_account(opencli_path: Path) -> PortalAccountConfig:
    return PortalAccountConfig(
        account_id="primary",
        label="Primary",
        adapter="opencli",
        opencli_path=opencli_path,
        opencli_profile="profile-alias",
        credential_pointer="OpenCLI Browser Bridge profile",
        expected_domains=("example.com",),
    )


def portal_site(public_url: str) -> SiteConfig:
    return SiteConfig(
        site_id="prod",
        label="Production",
        environment="production",
        adapter="novamira_mcp",
        credential_pointer="novamira-ops exact server",
        recovery_pointer="SiteGround backup owner",
        public_url=public_url,
        novamira_server="novamira-example",
        portal_account="primary",
        portal_site_id="EXAMPLESITEID003",
        portal_plan_id="EXAMPLEPLANID005",
    )


def test_portal_reads_pass_only_an_allowlisted_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, str] = {}

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs["env"])  # type: ignore[arg-type]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps([{"domain": "example.com", "status": "Active"}]),
            stderr="",
        )

    monkeypatch.setenv("HOME", "/tmp/hostile-home")
    monkeypatch.setenv("PATH", "/tmp/hostile-bin")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("LC_CTYPE", "en_US.UTF-8")
    monkeypatch.setenv("NODE_OPTIONS", "--require=/tmp/inject.js")
    monkeypatch.setenv("NODE_PATH", "/tmp/injected-modules")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-opencli")
    monkeypatch.setenv("SITEGROUND_PASSWORD", "must-not-reach-opencli")

    opencli_path = tmp_path / "bin" / "opencli"
    adapter = PortalOpenCliAdapter(
        portal_account(opencli_path),
        run_command=run,
        home=tmp_path,
    )

    adapter.read("websites")

    assert captured["HOME"] == str(tmp_path)
    assert captured["LANG"] == "en_US.UTF-8"
    assert captured["LC_CTYPE"] == "en_US.UTF-8"
    assert str(opencli_path.parent) in captured["PATH"].split(":")
    assert "/tmp/hostile-bin" not in captured["PATH"].split(":")
    # OPENCLI_BROWSER_COMMAND_TIMEOUT is admitted deliberately: it is the only
    # lever over OpenCLI's browser cap, since the adapter command has no
    # --timeout flag. Its value is asserted too, so it cannot drift back to a
    # default that is smaller than the work it is timing.
    assert captured["OPENCLI_BROWSER_COMMAND_TIMEOUT"] == str(portal.PORTAL_READ_BROWSER_SECONDS)
    assert set(captured) <= {
        "HOME",
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "OPENCLI_BROWSER_COMMAND_TIMEOUT",
    }


def test_portal_default_runner_isolated_from_novamira_process_group_logic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps([{"domain": "example.com", "status": "Active"}]),
            stderr="",
        )

    monkeypatch.setattr("siteground_ops.portal._run_opencli_command", run)
    opencli_path = tmp_path / "bin" / "opencli"

    PortalOpenCliAdapter(portal_account(opencli_path), home=tmp_path).read("websites")

    assert len(calls) == 1
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["check"] is False
    # Derived, never a literal: the wrapper's budget must stay outside OpenCLI's.
    assert calls[0][1]["timeout"] == (
        portal.PORTAL_READ_BROWSER_SECONDS + portal.OPENCLI_BROWSER_CONNECT_ALLOWANCE_SECONDS
    )


def test_opencli_runner_returns_when_a_detached_daemon_keeps_output_pipes_open(
    tmp_path: Path,
) -> None:
    daemon_pid_path = tmp_path / "daemon.pid"
    command_path = tmp_path / "opencli-client.py"
    command_path.write_text(
        "import json, subprocess, sys\n"
        "from pathlib import Path\n"
        "daemon = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(30)'],\n"
        "    start_new_session=True,\n"
        ")\n"
        "Path(sys.argv[1]).write_text(str(daemon.pid), encoding='utf-8')\n"
        "print(json.dumps([{'domain': 'example.com'}]))\n",
        encoding="utf-8",
    )

    started = time.monotonic()
    result = portal._run_opencli_command(
        [sys.executable, str(command_path), str(daemon_pid_path)],
        capture_output=True,
        text=True,
        timeout=2.0,
        check=False,
        shell=False,
        env=None,
    )
    elapsed = time.monotonic() - started
    daemon_pid = int(daemon_pid_path.read_text(encoding="utf-8"))

    try:
        assert result.returncode == 0
        assert json.loads(result.stdout) == [{"domain": "example.com"}]
        assert elapsed < 1.5
        os.kill(daemon_pid, 0)
    finally:
        try:
            os.kill(daemon_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_opencli_runner_rejects_output_above_its_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command_path = tmp_path / "noisy-opencli.py"
    command_path.write_text("print('x' * 128)\n", encoding="utf-8")
    monkeypatch.setattr(portal, "MAX_OPENCLI_OUTPUT_BYTES", 32)

    with pytest.raises(RunnerError, match="output limit"):
        portal._run_opencli_command(
            [sys.executable, str(command_path)],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
            shell=False,
            env=None,
        )


def executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def write_registration(home: Path, target: Path, *, lock_target: Path | None = None) -> None:
    opencli_home = home / ".opencli"
    plugins = opencli_home / "plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    (plugins / "siteground").symlink_to(target, target_is_directory=True)
    (opencli_home / "plugins.lock.json").write_text(
        json.dumps(
            {
                "siteground": {
                    "source": {
                        "kind": "local",
                        "path": str((lock_target or target).resolve()),
                    },
                    "commitHash": "local",
                    "installedAt": "2026-08-09T00:00:00.000Z",
                }
            }
        ),
        encoding="utf-8",
    )


def command_help(command: str) -> str:
    access = portal.PORTAL_PLUGIN_ACCESS[command]
    return (
        f"Usage: opencli siteground {command} [options]\n\n"
        f"Access: {access} | Browser: yes | Domain: siteground.com\n"
    )


def copy_adapter_source(destination: Path) -> None:
    destination.mkdir()
    for source in ADAPTER_DIR.glob("*.js"):
        shutil.copy2(source, destination / source.name)


def test_registration_installs_once_then_is_a_noop_without_touching_profiles(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    opencli_path = executable(tmp_path / "bin" / "opencli")
    opencli_home = home / ".opencli"
    profile_state = opencli_home / "browser-profiles.json"
    credential_state = opencli_home / "profiles" / "profile-alias" / "credential-sentinel"
    unrelated_adapter = opencli_home / "plugins" / "unrelated"
    credential_state.parent.mkdir(parents=True)
    unrelated_adapter.mkdir(parents=True)
    profile_state.write_text('{"active":"profile-alias"}', encoding="utf-8")
    credential_state.write_text("unchanged", encoding="utf-8")
    unrelated_adapter.joinpath("command.js").write_text("unchanged", encoding="utf-8")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        if command[1:3] == ["plugin", "install"]:
            assert command[3] == ADAPTER_DIR.resolve().as_uri()
            write_registration(home, ADAPTER_DIR)
            return subprocess.CompletedProcess(command, 0, stdout="Installed siteground\n", stderr="")
        assert command[1] == "siteground"
        assert command[2] in PORTAL_COMMANDS
        assert command[3:] == ["--help"]
        return subprocess.CompletedProcess(command, 0, stdout=command_help(command[2]), stderr="")

    first = register_opencli_adapter(
        opencli_path,
        ADAPTER_DIR,
        home=home,
        run_command=run,
    )
    second = register_opencli_adapter(
        opencli_path,
        ADAPTER_DIR,
        home=home,
        run_command=run,
    )

    assert first["status"] == "ready"
    assert first["changed"] is True
    assert second["status"] == "ready"
    assert second["changed"] is False
    assert sum(command[1:3] == ["plugin", "install"] for command, _kwargs in calls) == 1
    assert profile_state.read_text(encoding="utf-8") == '{"active":"profile-alias"}'
    assert credential_state.read_text(encoding="utf-8") == "unchanged"
    assert unrelated_adapter.joinpath("command.js").read_text(encoding="utf-8") == "unchanged"


def test_registration_refuses_an_existing_wrong_target_without_replacing_it(tmp_path: Path) -> None:
    home = tmp_path / "home"
    opencli_path = executable(tmp_path / "bin" / "opencli")
    wrong_target = tmp_path / "other-adapter"
    wrong_target.mkdir()
    write_registration(home, wrong_target)
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        raise AssertionError("wrong-target readiness must not invoke OpenCLI")

    status = register_opencli_adapter(
        opencli_path,
        ADAPTER_DIR,
        home=home,
        run_command=run,
    )

    assert status["status"] == "wrong_target"
    assert status["ready"] is False
    assert status["changed"] is False
    assert (home / ".opencli" / "plugins" / "siteground").resolve() == wrong_target.resolve()
    assert calls == []


def test_registration_reports_a_stale_lockfile_without_rewriting_it(tmp_path: Path) -> None:
    home = tmp_path / "home"
    opencli_path = executable(tmp_path / "bin" / "opencli")
    stale_target = tmp_path / "stale-adapter"
    stale_target.mkdir()
    write_registration(home, ADAPTER_DIR, lock_target=stale_target)
    lock_path = home / ".opencli" / "plugins.lock.json"
    before = lock_path.read_bytes()

    status = register_opencli_adapter(opencli_path, ADAPTER_DIR, home=home)

    assert status["status"] == "stale"
    assert status["correctly_linked"] is True
    assert status["ready"] is False
    assert status["changed"] is False
    assert lock_path.read_bytes() == before


def test_registration_treats_a_non_object_plugin_lock_entry_as_stale(tmp_path: Path) -> None:
    home = tmp_path / "home"
    opencli_path = executable(tmp_path / "bin" / "opencli")
    lock_path = home / ".opencli" / "plugins.lock.json"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(json.dumps({"siteground": "corrupt"}), encoding="utf-8")
    before = lock_path.read_bytes()
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        raise AssertionError("malformed registration metadata must not invoke OpenCLI")

    status = register_opencli_adapter(
        opencli_path,
        ADAPTER_DIR,
        home=home,
        run_command=run,
    )

    assert status["status"] == "stale"
    assert status["ready"] is False
    assert status["changed"] is False
    assert lock_path.read_bytes() == before
    assert calls == []


def test_missing_opencli_is_unavailable_without_creating_registration(tmp_path: Path) -> None:
    home = tmp_path / "home"
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        raise AssertionError("missing OpenCLI must not be invoked")

    status = register_opencli_adapter(
        tmp_path / "missing-opencli",
        ADAPTER_DIR,
        home=home,
        run_command=run,
    )

    assert status["status"] == "unavailable"
    assert status["available"] is False
    assert status["ready"] is False
    assert status["changed"] is False
    assert not (home / ".opencli").exists()
    assert calls == []


def test_readiness_requires_opencli_to_resolve_every_fixed_portal_command(tmp_path: Path) -> None:
    home = tmp_path / "home"
    opencli_path = executable(tmp_path / "bin" / "opencli")
    write_registration(home, ADAPTER_DIR)

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        returncode = 0 if command[2] != "statistics" else 2
        return subprocess.CompletedProcess(
            command,
            returncode,
            stdout=command_help(command[2]) if returncode == 0 else "",
            stderr="",
        )

    status = opencli_registration_status(
        opencli_path,
        ADAPTER_DIR,
        home=home,
        run_command=run,
    )

    assert status["status"] == "stale"
    assert status["registered"] is True
    assert status["correctly_linked"] is True
    assert status["resolved"] is False
    assert status["ready"] is False


def test_readiness_rejects_global_help_after_plugin_import_warnings(tmp_path: Path) -> None:
    home = tmp_path / "home"
    opencli_path = executable(tmp_path / "bin" / "opencli")
    write_registration(home, ADAPTER_DIR)

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="Usage: opencli [options] [command]\n",
            stderr="Failed to load siteground plugin: Cannot find package '@jackwener/opencli'\n",
        )

    status = opencli_registration_status(
        opencli_path,
        ADAPTER_DIR,
        home=home,
        run_command=run,
    )

    assert status["status"] == "stale"
    assert status["registered"] is True
    assert status["correctly_linked"] is True
    assert status["resolved"] is False
    assert status["ready"] is False


def test_readiness_rejects_a_source_command_with_write_access(tmp_path: Path) -> None:
    adapter_dir = tmp_path / "siteground"
    adapter_dir.mkdir()
    for command in PORTAL_COMMANDS:
        access = "write" if command == "renewals" else "read"
        adapter_dir.joinpath(f"{command}.js").write_text(
            f"access: '{access}',\nbrowser: true,\n",
            encoding="utf-8",
        )
    opencli_path = executable(tmp_path / "bin" / "opencli")
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        raise AssertionError("invalid source must not invoke OpenCLI")

    status = opencli_registration_status(
        opencli_path,
        adapter_dir,
        home=tmp_path / "home",
        run_command=run,
    )

    assert status["status"] == "source_invalid"
    assert status["resolved"] is False
    assert status["ready"] is False
    assert calls == []


def test_readiness_rejects_an_unexpected_helper_command(tmp_path: Path) -> None:
    adapter_dir = tmp_path / "siteground"
    copy_adapter_source(adapter_dir)
    adapter_dir.joinpath("_delete.js").write_text(
        "export const deleteCommand = { access: 'write', browser: true };\n",
        encoding="utf-8",
    )
    opencli_path = executable(tmp_path / "bin" / "opencli")

    status = opencli_registration_status(opencli_path, adapter_dir, home=tmp_path / "home")

    assert status["status"] == "source_invalid"
    assert status["ready"] is False


def test_readiness_rejects_a_symlinked_runtime_helper(tmp_path: Path) -> None:
    adapter_dir = tmp_path / "siteground"
    copy_adapter_source(adapter_dir)
    outside_helper = tmp_path / "outside-ui.js"
    shutil.copy2(adapter_dir / "_ui.js", outside_helper)
    adapter_dir.joinpath("_ui.js").unlink()
    adapter_dir.joinpath("_ui.js").symlink_to(outside_helper)
    opencli_path = executable(tmp_path / "bin" / "opencli")

    status = opencli_registration_status(opencli_path, adapter_dir, home=tmp_path / "home")

    assert status["status"] == "source_invalid"
    assert status["ready"] is False


def test_readiness_rejects_mutation_code_in_a_runtime_helper(tmp_path: Path) -> None:
    adapter_dir = tmp_path / "siteground"
    copy_adapter_source(adapter_dir)
    helper_path = adapter_dir / "_ui.js"
    helper_path.write_text(
        helper_path.read_text(encoding="utf-8") + "\nexport async function mutate(page) { await page.click('button'); }\n",
        encoding="utf-8",
    )
    opencli_path = executable(tmp_path / "bin" / "opencli")

    status = opencli_registration_status(opencli_path, adapter_dir, home=tmp_path / "home")

    assert status["status"] == "source_invalid"
    assert status["ready"] is False


@pytest.mark.parametrize(
    "public_url",
    [
        "http://example.com",
        "https://user@example.com",
        "https://example.com/untrusted-path",
        "https://example.com?redirect=https://other.example",
    ],
)
def test_site_tools_links_revalidate_an_exact_https_origin(public_url: str) -> None:
    with pytest.raises(PortalError, match="HTTPS origin"):
        site_tools_links(portal_site(public_url))


def test_registration_script_reports_unavailable_opencli_without_mutation(tmp_path: Path) -> None:
    home = tmp_path / "home"

    result = subprocess.run(
        [
            sys.executable,
            str(REGISTRATION_SCRIPT),
            "--opencli-path",
            str(tmp_path / "missing-opencli"),
            "--adapter-dir",
            str(ADAPTER_DIR),
            "--home",
            str(home),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        shell=False,
        env={
            "HOME": str(home),
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )

    assert result.returncode == 1
    assert json.loads(result.stdout) == {
        "available": False,
        "changed": False,
        "correctly_linked": False,
        "ready": False,
        "registered": False,
        "resolved": False,
        "status": "unavailable",
    }
    assert result.stderr == ""
    assert not home.exists()
