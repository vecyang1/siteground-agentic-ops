from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from siteground_ops import runner
from siteground_ops.runner import RunnerError


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_terminate_process_group_kills_descendants_after_leader_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int]] = []

    class Process:
        pid = 4321

        def poll(self) -> int:
            return 0

        def wait(self, timeout: float | None = None) -> int:
            return 0

    def killpg(pid: int, sig: int) -> None:
        calls.append((pid, sig))

    monkeypatch.setattr(runner.os, "killpg", killpg)

    runner._terminate_process_group(Process())  # type: ignore[arg-type]

    assert calls == [
        (4321, signal.SIGTERM),
        (4321, 0),
        (4321, signal.SIGKILL),
    ]


def _write_nonzero_bridge(tmp_path: Path, *, redirect_child_output: bool) -> tuple[Path, Path, Path]:
    state_path = tmp_path / "child-state.json"
    ready_path = tmp_path / "child-ready"
    bridge = tmp_path / "bridge.py"
    child_streams = (
        "    stdout=subprocess.DEVNULL,\n"
        "    stderr=subprocess.DEVNULL,\n"
        if redirect_child_output
        else ""
    )
    bridge.write_text(
        "import json, os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "state_path, ready_path = map(Path, sys.argv[1:])\n"
        "child_code = (\n"
        "    'import signal, sys, time\\n'\n"
        "    'from pathlib import Path\\n'\n"
        "    'signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n'\n"
        "    'Path(sys.argv[1]).write_text(\\\"ready\\\")\\n'\n"
        "    'time.sleep(30)\\n'\n"
        ")\n"
        "child = subprocess.Popen(\n"
        "    [sys.executable, '-c', child_code, str(ready_path)],\n"
        "    stdin=subprocess.DEVNULL,\n"
        f"{child_streams}"
        ")\n"
        "deadline = time.monotonic() + 2\n"
        "while not ready_path.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n"
        "state_path.write_text(json.dumps({'pid': child.pid, 'pgid': os.getpgrp()}))\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    return bridge, state_path, ready_path


@pytest.mark.parametrize("redirect_child_output", [True, False])
def test_bounded_command_reaps_descendants_after_normal_nonzero_exit(
    tmp_path: Path, redirect_child_output: bool
) -> None:
    bridge, state_path, ready_path = _write_nonzero_bridge(
        tmp_path, redirect_child_output=redirect_child_output
    )

    result = runner._run_bounded_command(
        [sys.executable, str(bridge), str(state_path), str(ready_path)],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
        shell=False,
        env=None,
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    child_pid = int(state["pid"])
    process_group = int(state["pgid"])

    try:
        deadline = time.monotonic() + 3
        while _pid_exists(child_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert result.returncode == 1
        assert not _pid_exists(child_pid)
    finally:
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_runtime_hash_rejects_symlink_targets_outside_verified_tree(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    external = tmp_path / "external.js"
    external.write_text("first", encoding="utf-8")
    (runtime / "proxy.js").symlink_to(external)

    with pytest.raises(RunnerError, match="outside"):
        runner._runtime_tree_hash(runtime)


def _write_runtime_manifest(tmp_path: Path, *, proxy_symlink: bool, package_symlink: bool) -> Path:
    runtime = tmp_path / "runtime"
    package_root = runtime / "node_modules" / "@automattic" / "mcp-wordpress-remote"
    (package_root / "dist").mkdir(parents=True)

    package_bytes = json.dumps(
        {"name": "@automattic/mcp-wordpress-remote", "version": "0.3.5"}
    )
    package_json = package_root / "package.json"
    if package_symlink:
        package_target = runtime / "package-target.json"
        package_target.write_text(package_bytes, encoding="utf-8")
        package_json.symlink_to(package_target)
    else:
        package_json.write_text(package_bytes, encoding="utf-8")

    proxy = package_root / "dist" / "proxy.js"
    if proxy_symlink:
        proxy_target = runtime / "proxy-target.js"
        proxy_target.write_text("", encoding="utf-8")
        proxy.symlink_to(proxy_target)
    else:
        proxy.write_text("", encoding="utf-8")

    manifest = tmp_path / "runtime.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "package_name": "@automattic/mcp-wordpress-remote",
                "package_version": "0.3.5",
                "runtime_root": str(runtime),
                "command": [sys.executable, str(proxy)],
                "tree_sha256": runner._runtime_tree_hash(runtime),
            }
        ),
        encoding="utf-8",
    )
    return manifest


@pytest.mark.parametrize(
    ("proxy_symlink", "package_symlink"),
    [(True, False), (False, True)],
)
def test_pinned_runtime_requires_regular_proxy_and_package_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    proxy_symlink: bool,
    package_symlink: bool,
) -> None:
    manifest = _write_runtime_manifest(
        tmp_path,
        proxy_symlink=proxy_symlink,
        package_symlink=package_symlink,
    )
    monkeypatch.setenv("SITEGROUND_OPS_NOVAMIRA_RUNTIME", str(manifest))

    with pytest.raises(RunnerError, match="regular"):
        runner._load_pinned_novamira_runtime()
