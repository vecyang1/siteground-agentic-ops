from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
import tarfile

from siteground_ops.novamira_backend import (
    LocalNovamiraBackend,
    NovamiraPaths,
    normalize_cli_json,
    normalize_offline_doctor,
)
from siteground_ops.novamira_update import RegistryRelease, controlled_bun_command


def _package_tarball(version: str = "1.0.3") -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        manifest = json.dumps(
            {"name": "@novamira/cli", "version": version, "bin": {"novamira": "dist/index.js"}},
            separators=(",", ":"),
        ).encode()
        manifest_info = tarfile.TarInfo("package/package.json")
        manifest_info.size = len(manifest)
        archive.addfile(manifest_info, io.BytesIO(manifest))
        index = b"#!/usr/bin/env node\nconsole.log('novamira')\n"
        index_info = tarfile.TarInfo("package/dist/index.js")
        index_info.mode = 0o755
        index_info.size = len(index)
        archive.addfile(index_info, io.BytesIO(index))
    return stream.getvalue()


def _release(raw: bytes, version: str = "1.0.3") -> RegistryRelease:
    return RegistryRelease(
        version=version,
        tarball_url="https://registry.npmjs.org/@novamira/cli/-/cli-1.0.3.tgz",
        integrity="sha512-" + base64.b64encode(hashlib.sha512(raw).digest()).decode(),
        registry_integrity_ok=True,
        provenance_ok=True,
        tarball_bytes=raw,
        artifact_sha512=hashlib.sha512(raw).hexdigest(),
    )


def test_normalize_cli_json_ignores_update_warning_and_keeps_payload() -> None:
    assert normalize_cli_json(
        'Warning: update available\n{"ok":true,"data":[{"name":"site"}]}'
    ) == {"ok": True, "data": [{"name": "site"}]}


def test_offline_doctor_warn_without_failed_checks_is_usable() -> None:
    report = {
        "ok": True,
        "data": {
            "status": "warn",
            "checks": [
                {"id": "runtime.node", "status": "pass"},
                {"id": "profile.valid", "status": "warn"},
            ],
        },
    }

    assert normalize_offline_doctor(report) == {
        "status": "pass",
        "failed_checks": [],
        "warnings": ["profile.valid"],
    }


def test_offline_doctor_explicit_failure_and_unknown_status_fail_closed() -> None:
    assert normalize_offline_doctor(
        {"ok": True, "data": {"status": "fail", "checks": []}}
    )["status"] == "fail"
    assert normalize_offline_doctor(
        {
            "ok": True,
            "data": {"status": "pass", "checks": [{"id": "runtime", "status": "unknown"}]},
        }
    )["status"] == "fail"


def test_profile_inventory_is_identity_only_and_does_not_persist_tokens(tmp_path: Path) -> None:
    paths = NovamiraPaths(
        home=tmp_path,
        binary=tmp_path / "novamira",
        package_dir=tmp_path / "node_modules/@novamira/cli",
        manifest=tmp_path / "package.json",
        lock=tmp_path / "bun.lock",
        skill=tmp_path / "SKILL.md",
        bun=tmp_path / "bun",
    )
    paths.binary.write_text("#!/bin/sh\nprintf '%s' '{\"ok\":true,\"data\":[{\"name\":\"site\",\"siteUrl\":\"https://example.com\",\"token\":\"secret\"}]}'\n", encoding="utf-8")
    paths.binary.chmod(0o700)
    backend = LocalNovamiraBackend(paths=paths)

    assert backend.profile_inventory() == [
        {"name": "site", "siteUrl": "https://example.com"}
    ]


def test_snapshot_restore_is_atomic_and_scoped_to_owner_files(tmp_path: Path) -> None:
    paths = NovamiraPaths(
        home=tmp_path,
        binary=tmp_path / "novamira",
        package_dir=tmp_path / "node_modules/@novamira/cli",
        manifest=tmp_path / "package.json",
        lock=tmp_path / "bun.lock",
        skill=tmp_path / "SKILL.md",
        bun=tmp_path / "bun",
    )
    paths.manifest.write_text('{"dependencies":{"@novamira/cli":"1.0.0"}}\n', encoding="utf-8")
    paths.lock.write_text("lock-v1\n", encoding="utf-8")
    installed = type("Installed", (), {"version": "1.0.0"})()
    backend = LocalNovamiraBackend(paths=paths)

    snapshot = backend.snapshot_owner(installed)
    paths.manifest.write_text("changed\n", encoding="utf-8")
    paths.lock.write_text("changed\n", encoding="utf-8")
    backend.restore_owner_snapshot(snapshot)

    assert paths.manifest.read_text(encoding="utf-8") == '{"dependencies":{"@novamira/cli":"1.0.0"}}\n'
    assert paths.lock.read_text(encoding="utf-8") == "lock-v1\n"
    assert not (tmp_path / "SKILL.md.bak").exists()


def test_owner_snapshot_readback_requires_exact_owned_bytes(tmp_path: Path) -> None:
    paths = NovamiraPaths(
        home=tmp_path,
        binary=tmp_path / "novamira",
        package_dir=tmp_path / "node_modules/@novamira/cli",
        manifest=tmp_path / "package.json",
        lock=tmp_path / "bun.lock",
        skill=tmp_path / "SKILL.md",
        bun=tmp_path / "bun",
    )
    paths.manifest.write_bytes(b"manifest-v1")
    paths.lock.write_bytes(b"lock-v1")
    backend = LocalNovamiraBackend(paths=paths)
    snapshot = backend.snapshot_owner(type("Installed", (), {"version": "1.0.0"})())

    assert backend.verify_owner_snapshot(snapshot) is True
    paths.lock.write_bytes(b"tampered")
    assert backend.verify_owner_snapshot(snapshot) is False


def test_owner_baseline_refreshes_after_successful_version_change(tmp_path: Path) -> None:
    paths = NovamiraPaths.from_home(tmp_path)
    paths.manifest.write_text('{"dependencies":{"@novamira/cli":"1.0.3"}}\n', encoding="utf-8")
    paths.lock.write_text(
        '{"workspaces":{"":{"dependencies":{"@novamira/cli":"1.0.3"}}},'
        '"packages":{}}\n',
        encoding="utf-8",
    )
    backend = LocalNovamiraBackend(paths=paths)

    backend.refresh_owner_baseline("1.0.3")

    baseline = json.loads(
        (tmp_path / ".cache/siteground-ops/novamira-owner-baseline.json").read_text()
    )
    assert baseline == {
        "version": "1.0.3",
        "manifest_sha256": hashlib.sha256(paths.manifest.read_bytes()).hexdigest(),
        "lock_sha256": hashlib.sha256(paths.lock.read_bytes()).hexdigest(),
    }


def test_missing_owner_baseline_fails_closed(tmp_path: Path) -> None:
    paths = NovamiraPaths.from_home(tmp_path)
    paths.manifest.write_text('{"dependencies":{"@novamira/cli":"1.0.3"}}\n', encoding="utf-8")
    paths.lock.write_text(
        '{"workspaces":{"":{"dependencies":{"@novamira/cli":"1.0.3"}}},'
        '"packages":{}}\n',
        encoding="utf-8",
    )
    backend = LocalNovamiraBackend(paths=paths)

    assert backend._owner_content_clean("1.0.3") is False


def test_owner_snapshot_restores_baseline_after_failed_update(tmp_path: Path) -> None:
    paths = NovamiraPaths.from_home(tmp_path)
    paths.manifest.write_text('{"dependencies":{"@novamira/cli":"1.0.0"}}\n', encoding="utf-8")
    paths.lock.write_text(
        '{"workspaces":{"":{"dependencies":{"@novamira/cli":"1.0.0"}}},'
        '"packages":{}}\n',
        encoding="utf-8",
    )
    backend = LocalNovamiraBackend(paths=paths)
    backend.refresh_owner_baseline("1.0.0")
    snapshot = backend.snapshot_owner(type("Installed", (), {"version": "1.0.0"})())
    paths.manifest.write_text('{"dependencies":{"@novamira/cli":"1.0.3"}}\n', encoding="utf-8")
    paths.lock.write_text("changed\n", encoding="utf-8")
    backend.restore_owner_snapshot(snapshot)

    assert backend.verify_owner_snapshot(snapshot) is True


def test_owner_snapshot_restores_exact_package_bytes(tmp_path: Path) -> None:
    package_dir = tmp_path / "node_modules/@novamira/cli"
    package_dir.mkdir(parents=True)
    (package_dir / "package.json").write_text('{"version":"1.0.0"}', encoding="utf-8")
    launcher = package_dir / "dist/index.js"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(0o755)
    paths = NovamiraPaths(
        home=tmp_path,
        binary=tmp_path / "novamira",
        package_dir=package_dir,
        manifest=tmp_path / "package.json",
        lock=tmp_path / "bun.lock",
        skill=tmp_path / "SKILL.md",
        bun=tmp_path / "bun",
    )
    paths.manifest.write_bytes(b"manifest")
    paths.lock.write_bytes(b"lock")
    backend = LocalNovamiraBackend(paths=paths)
    snapshot = backend.snapshot_owner(type("Installed", (), {"version": "1.0.0"})())
    (package_dir / "package.json").write_text("changed", encoding="utf-8")
    launcher.chmod(0o644)
    backend.restore_owner_snapshot(snapshot)
    assert (package_dir / "package.json").read_text(encoding="utf-8") == '{"version":"1.0.0"}'
    assert launcher.stat().st_mode & 0o777 == 0o755
    assert backend.verify_owner_snapshot(snapshot) is True


def test_apply_bun_rejects_commands_outside_controlled_allowlist(tmp_path: Path) -> None:
    paths = NovamiraPaths(
        home=tmp_path,
        binary=tmp_path / "novamira",
        package_dir=tmp_path / "node_modules/@novamira/cli",
        manifest=tmp_path / "package.json",
        lock=tmp_path / "bun.lock",
        skill=tmp_path / "SKILL.md",
        bun=tmp_path / "bun",
    )
    backend = LocalNovamiraBackend(paths=paths, command_runner=lambda *args, **kwargs: None)
    release = RegistryRelease(
        version="1.0.3",
        tarball_url="",
        integrity="",
        registry_integrity_ok=True,
        provenance_ok=True,
    )

    try:
        backend.apply_bun(release, (*controlled_bun_command("1.0.3")[:-1], "@novamira/cli@9.9.9"))
    except ValueError as exc:
        assert "controlled allowlist" in str(exc)
    else:
        raise AssertionError("malformed Bun command was accepted")


def test_apply_bun_uses_the_pinned_artifact_bytes(tmp_path: Path) -> None:
    paths = NovamiraPaths(
        home=tmp_path,
        binary=tmp_path / "novamira",
        package_dir=tmp_path / "node_modules/@novamira/cli",
        manifest=tmp_path / "package.json",
        lock=tmp_path / "bun.lock",
        skill=tmp_path / "SKILL.md",
        bun=tmp_path / "bun",
    )
    paths.bun.parent.mkdir(parents=True, exist_ok=True)
    observed: list[tuple[str, ...]] = []

    raw = _package_tarball()

    def run(args, **kwargs):
        observed.append(tuple(args))
        prefix = Path(kwargs["cwd"])
        candidate = prefix / "node_modules/@novamira/cli"
        LocalNovamiraBackend._extract_package(raw, candidate)
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    paths.manifest.write_text(
        '{"dependencies":{"@novamira/cli":"1.0.0"}}\n', encoding="utf-8"
    )
    paths.lock.write_text(
        '{"workspaces":{"":{"dependencies":{"@novamira/cli":"1.0.0"}}},'
        '"packages":{"@novamira/cli":["@novamira/cli@1.0.0","",{},"old"]}}\n',
        encoding="utf-8",
    )
    release = _release(raw)
    command = LocalNovamiraBackend(paths=paths, command_runner=run).apply_bun(
        release, controlled_bun_command("1.0.3")
    )
    assert command[0] == str(paths.bun)
    assert "--no-save" in command
    assert Path(command[-1]).name == "novamira-cli.tgz"
    assert json.loads(paths.manifest.read_text()) ["dependencies"]["@novamira/cli"] == "1.0.3"
    assert '"@novamira/cli":"1.0.3"' in paths.lock.read_text()
    assert str(tmp_path) not in paths.manifest.read_text()
    assert observed


def test_candidate_verification_refuses_unpinned_release(tmp_path: Path) -> None:
    paths = NovamiraPaths(
        home=tmp_path,
        binary=tmp_path / "novamira",
        package_dir=tmp_path / "node_modules/@novamira/cli",
        manifest=tmp_path / "package.json",
        lock=tmp_path / "bun.lock",
        skill=tmp_path / "SKILL.md",
        bun=tmp_path / "bun",
    )
    release = RegistryRelease(
        version="1.0.3",
        tarball_url="https://registry.npmjs.org/@novamira/cli/-/cli-1.0.3.tgz",
        integrity="sha512-unpinned",
        registry_integrity_ok=True,
        provenance_ok=True,
    )
    assert LocalNovamiraBackend(paths=paths).verify_candidate(release) == {
        "registry_integrity_ok": False,
        "offline_doctor_ok": False,
    }


def test_package_extractor_rejects_path_traversal(tmp_path: Path) -> None:
    import io
    import tarfile

    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        info = tarfile.TarInfo("package/../escape.txt")
        payload = b"unsafe"
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))

    try:
        LocalNovamiraBackend._extract_package(stream.getvalue(), tmp_path / "stage")
    except ValueError as exc:
        assert "unsafe path" in str(exc)
    else:
        raise AssertionError("unsafe tar path was extracted")


def test_installed_package_rejects_symlinked_package_topology(tmp_path: Path) -> None:
    real = tmp_path / "real-package"
    real.mkdir(parents=True)
    (real / "package.json").write_text('{"version":"1.0.0"}', encoding="utf-8")
    package_dir = tmp_path / "node_modules/@novamira/cli"
    package_dir.parent.mkdir(parents=True)
    package_dir.symlink_to(real, target_is_directory=True)
    paths = NovamiraPaths(
        home=tmp_path,
        binary=tmp_path / ".bun/bin/novamira",
        package_dir=package_dir,
        manifest=tmp_path / "package.json",
        lock=tmp_path / "bun.lock",
        skill=tmp_path / "SKILL.md",
        bun=tmp_path / ".bun/bin/bun",
    )
    assert LocalNovamiraBackend(paths=paths).installed_package().integrity_clean is False


def test_candidate_dependency_snapshot_rejects_traversal(tmp_path: Path) -> None:
    paths = NovamiraPaths.from_home(tmp_path)
    backend = LocalNovamiraBackend(paths=paths)

    try:
        backend._dependency_snapshot({"../outside"})
    except ValueError as exc:
        assert "unsafe package name" in str(exc)
    else:
        raise AssertionError("unsafe dependency name was accepted")


def test_owner_snapshot_restores_hoisted_dependency_tree(tmp_path: Path) -> None:
    package_dir = tmp_path / "node_modules/@novamira/cli"
    package_dir.mkdir(parents=True)
    (package_dir / "package.json").write_text(
        '{"version":"1.0.0","dependencies":{"commander":"14.0.0"}}',
        encoding="utf-8",
    )
    dependency = tmp_path / "node_modules/commander"
    dependency.mkdir(parents=True)
    (dependency / "package.json").write_text('{"name":"commander"}', encoding="utf-8")
    (dependency / "index.js").write_text("original\n", encoding="utf-8")
    paths = NovamiraPaths.from_home(tmp_path)
    backend = LocalNovamiraBackend(paths=paths)
    snapshot = backend.snapshot_owner(type("Installed", (), {"version": "1.0.0"})())

    (dependency / "index.js").write_text("changed\n", encoding="utf-8")
    backend.restore_owner_snapshot(snapshot)

    assert (dependency / "index.js").read_text(encoding="utf-8") == "original\n"
    assert backend.verify_owner_snapshot(snapshot) is True


def test_restricted_runner_scrubs_environment_and_denies_home_writes(tmp_path: Path) -> None:
    paths = NovamiraPaths.from_home(tmp_path)
    observed: dict[str, object] = {}

    def run(args, **kwargs):
        observed["args"] = tuple(args)
        observed["env"] = kwargs["env"]
        return type("Result", (), {"returncode": 0, "stdout": "{}", "stderr": ""})()

    backend = LocalNovamiraBackend(paths=paths, command_runner=run)
    backend._run([str(paths.bun), "--version"], cwd=tmp_path / "stage", restricted=True)

    args = observed["args"]
    env = observed["env"]
    assert isinstance(args, tuple)
    profile = args[2]
    assert "deny network" in profile
    assert "deny file-write*" in profile
    assert isinstance(env, dict)
    assert env["PATH"]
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert env["HOME"] != str(Path.home())


def test_npm_resolution_uses_absolute_configured_path(tmp_path: Path, monkeypatch) -> None:
    npm = tmp_path / "npm"
    npm.write_text("#!/bin/sh\n", encoding="utf-8")
    npm.chmod(0o755)
    monkeypatch.setenv("SITEGROUND_NPM_BIN", str(npm))
    backend = LocalNovamiraBackend(paths=NovamiraPaths.from_home(tmp_path))

    assert backend._resolve_npm() == str(npm)
