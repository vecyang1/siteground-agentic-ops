from __future__ import annotations

from pathlib import Path

from siteground_ops.novamira_update import (
    SUPPORTED_CLI_VERSION,
    InstalledPackage,
    NovamiraUpdater,
    RegistryRelease,
    controlled_bun_command,
)
from siteground_ops.novamira_update import UpdateInProgress


class FakeBackend:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.mutation_calls: list[str] = []
        self.baseline_refreshes: list[str] = []

    def registry_release(self) -> RegistryRelease:
        return RegistryRelease(
            version=SUPPORTED_CLI_VERSION,
            tarball_url=f"https://registry.npmjs.org/@novamira/cli/-/cli-{SUPPORTED_CLI_VERSION}.tgz",
            integrity="sha512-published",
            registry_integrity_ok=True,
            provenance_ok=True,
        )

    def installed_package(self) -> InstalledPackage:
        return InstalledPackage(
            version="1.0.0",
            package_dir=self.home / "node_modules" / "@novamira" / "cli",
            owner_manifest=self.home / "package.json",
            owner_lock=self.home / "bun.lock",
            integrity_clean=True,
        )

    def guidance_text(self) -> str:
        return "Novamira 1.11.1+ requires full access."

    def verify_candidate(self, _release: RegistryRelease):
        self.mutation_calls.append("verify_candidate")
        raise AssertionError("check must not stage a candidate")

    def profile_inventory(self):
        self.mutation_calls.append("profile_inventory")
        raise AssertionError("check must not inspect profile credentials")

    def snapshot_owner(self, _installed: InstalledPackage):
        self.mutation_calls.append("snapshot_owner")
        raise AssertionError("check must not snapshot files")

    def apply_bun(self, *_args):
        self.mutation_calls.append("apply_bun")
        raise AssertionError("check must not invoke Bun")

    def rollback(self, *_args):
        self.mutation_calls.append("rollback")
        raise AssertionError("check must not roll back")

    def refresh_owner_baseline(self, version: str) -> None:
        self.baseline_refreshes.append(version)


def test_check_reports_update_without_entering_mutation_lane(tmp_path: Path) -> None:
    backend = FakeBackend(tmp_path)

    result = NovamiraUpdater(backend=backend, home=tmp_path).check()

    assert result == {
        "ok": True,
        "mutation_state": "not_applicable",
        "current": "1.0.0",
        "latest": SUPPORTED_CLI_VERSION,
        "update_available": True,
        "auto_apply_ready": True,
        "blockers": [],
    }
    assert backend.mutation_calls == []


def test_check_is_healthy_when_latest_version_is_already_installed(tmp_path: Path) -> None:
    backend = FakeBackend(tmp_path)
    backend.installed_package = lambda: InstalledPackage(
        version=SUPPORTED_CLI_VERSION,
        package_dir=tmp_path / "node_modules" / "@novamira" / "cli",
        owner_manifest=tmp_path / "package.json",
        owner_lock=tmp_path / "bun.lock",
        integrity_clean=True,
    )

    result = NovamiraUpdater(backend=backend, home=tmp_path).check()

    assert result["ok"] is True
    assert result["update_available"] is False
    assert result["auto_apply_ready"] is True
    assert result["blockers"] == []


def test_initialize_baseline_is_explicit_and_readback_verified(tmp_path: Path) -> None:
    backend = MutationBackend(tmp_path)
    backend.version = SUPPORTED_CLI_VERSION

    result = NovamiraUpdater(backend=backend, home=tmp_path).initialize_baseline(
        confirmed=True
    )

    assert result == {
        "ok": True,
        "mutation_state": "applied",
        "current": SUPPORTED_CLI_VERSION,
        "latest": SUPPORTED_CLI_VERSION,
        "baseline_initialized": True,
    }
    assert backend.baseline_refreshes == [SUPPORTED_CLI_VERSION]


def test_apply_refuses_when_guidance_or_installed_integrity_is_not_safe(
    tmp_path: Path,
) -> None:
    backend = FakeBackend(tmp_path)
    backend.guidance_text = lambda: "Novamira 1.11.0+ still supports --access read"
    backend.installed_package = lambda: InstalledPackage(
        version="1.0.0",
        package_dir=tmp_path / "node_modules" / "@novamira" / "cli",
        owner_manifest=tmp_path / "package.json",
        owner_lock=tmp_path / "bun.lock",
        integrity_clean=False,
    )

    result = NovamiraUpdater(backend=backend, home=tmp_path).apply(confirmed=True)

    assert result["ok"] is False
    assert result["mutation_state"] == "refused"
    assert result["blockers"] == [
        "installed_package_integrity_failed",
        "local_novamira_ops_guidance_incompatible",
    ]
    assert backend.mutation_calls == []


def test_apply_requires_explicit_confirmation_even_when_all_gates_are_ready(
    tmp_path: Path,
) -> None:
    backend = MutationBackend(tmp_path)

    result = NovamiraUpdater(backend=backend, home=tmp_path).apply(confirmed=False)

    assert result["ok"] is False
    assert result["mutation_state"] == "refused"
    assert result["blockers"] == ["explicit_confirmation_required"]
    assert backend.mutation_calls == []


def test_controlled_command_is_exact_bun_owner_operation() -> None:
    assert controlled_bun_command(SUPPORTED_CLI_VERSION) == (
        "bun",
        "add",
        "--exact",
        "--ignore-scripts",
        "--registry",
        "https://registry.npmjs.org",
        f"@novamira/cli@{SUPPORTED_CLI_VERSION}",
    )


def test_apply_verifies_candidate_and_preserves_profiles(tmp_path: Path) -> None:
    backend = MutationBackend(tmp_path)

    result = NovamiraUpdater(backend=backend, home=tmp_path).apply(confirmed=True)

    assert result["ok"] is True
    assert result["mutation_state"] == "applied"
    assert result["current"] == SUPPORTED_CLI_VERSION
    assert result["previous"] == "1.0.0"
    assert result["candidate_verified"] is True
    assert result["profiles_preserved"] is True
    assert result["bun_command"][-1] == f"@novamira/cli@{SUPPORTED_CLI_VERSION}"
    assert backend.mutation_calls == [
        "verify_candidate",
        "profile_inventory",
        "snapshot_owner",
        "apply_bun",
        "offline_doctor",
        "profile_inventory",
    ]
    assert backend.baseline_refreshes == [SUPPORTED_CLI_VERSION]


def test_apply_rolls_back_when_post_apply_doctor_fails(tmp_path: Path) -> None:
    backend = FailingDoctorBackend(tmp_path)

    result = NovamiraUpdater(backend=backend, home=tmp_path).apply(confirmed=True)

    assert result["ok"] is False
    assert result["mutation_state"] == "rolled_back"
    assert result["rollback_verified"] is True
    assert result["current"] == "1.0.0"
    assert result["blockers"] == ["offline_doctor_failed"]
    assert backend.version == "1.0.0"
    assert backend.mutation_calls[-3:] == [
        "profile_inventory",
        "rollback",
        "profile_inventory",
    ]


class MutationBackend(FakeBackend):
    def __init__(self, home: Path) -> None:
        super().__init__(home)
        self.version = "1.0.0"
        self.profiles = [{"name": "siteground-prod", "siteUrl": "https://example.com"}]
        self.owner_manifest = b'{"dependencies":{"@novamira/cli":"1.0.0"}}\n'
        self.owner_lock = b"lock-v1\n"
        self.applied_command: tuple[str, ...] | None = None

    def verify_candidate(self, _release: RegistryRelease) -> dict[str, bool]:
        self.mutation_calls.append("verify_candidate")
        return {"registry_integrity_ok": True, "offline_doctor_ok": True}

    def profile_inventory(self) -> list[dict[str, str]]:
        self.mutation_calls.append("profile_inventory")
        return list(self.profiles)

    def snapshot_owner(self, _installed: InstalledPackage) -> dict[str, object]:
        self.mutation_calls.append("snapshot_owner")
        return {
            "previous_version": self.version,
            "manifest": self.owner_manifest,
            "lock": self.owner_lock,
        }

    def apply_bun(self, _release: RegistryRelease, command: tuple[str, ...]) -> None:
        self.mutation_calls.append("apply_bun")
        self.applied_command = command
        self.version = SUPPORTED_CLI_VERSION

    def installed_package(self) -> InstalledPackage:
        return InstalledPackage(
            version=self.version,
            package_dir=self.home / "node_modules" / "@novamira" / "cli",
            owner_manifest=self.home / "package.json",
            owner_lock=self.home / "bun.lock",
            integrity_clean=True,
        )

    def offline_doctor(self) -> dict[str, object]:
        self.mutation_calls.append("offline_doctor")
        return {"status": "pass", "failed_checks": []}

    def rollback(self, snapshot: dict[str, object]) -> None:
        self.mutation_calls.append("rollback")
        self.version = "1.0.0"
        self.owner_manifest = snapshot["manifest"]  # type: ignore[assignment]
        self.owner_lock = snapshot["lock"]  # type: ignore[assignment]

    def verify_owner_snapshot(self, snapshot: dict[str, object]) -> bool:
        return (
            self.owner_manifest == snapshot["manifest"]
            and self.owner_lock == snapshot["lock"]
        )


def test_apply_stages_verifies_preserves_profiles_and_applies_exact_owner_command(
    tmp_path: Path,
) -> None:
    backend = MutationBackend(tmp_path)

    result = NovamiraUpdater(backend=backend, home=tmp_path).apply(confirmed=True)

    assert result["ok"] is True
    assert result["mutation_state"] == "applied"
    assert result["candidate_verified"] is True
    assert result["profiles_preserved"] is True
    assert backend.mutation_calls == [
        "verify_candidate",
        "profile_inventory",
        "snapshot_owner",
        "apply_bun",
        "offline_doctor",
        "profile_inventory",
    ]
    assert backend.baseline_refreshes == [SUPPORTED_CLI_VERSION]
    assert backend.applied_command == controlled_bun_command(SUPPORTED_CLI_VERSION)


def test_apply_rolls_back_when_post_install_doctor_fails(tmp_path: Path) -> None:
    backend = MutationBackend(tmp_path)
    backend.offline_doctor = lambda: {"status": "fail", "failed_checks": ["doctor"]}

    result = NovamiraUpdater(backend=backend, home=tmp_path).apply(confirmed=True)

    assert result["ok"] is False
    assert result["mutation_state"] == "rolled_back"
    assert result["rollback_verified"] is True
    assert "offline_doctor_failed" in result["blockers"]
    assert backend.mutation_calls[-3:] == ["profile_inventory", "rollback", "profile_inventory"]


def test_apply_stays_unknown_when_rollback_cannot_be_verified(tmp_path: Path) -> None:
    backend = MutationBackend(tmp_path)
    backend.offline_doctor = lambda: {"status": "fail", "failed_checks": ["doctor"]}
    backend.rollback = lambda _snapshot: (_ for _ in ()).throw(RuntimeError("rollback unavailable"))

    result = NovamiraUpdater(backend=backend, home=tmp_path).apply(confirmed=True)

    assert result["ok"] is False
    assert result["mutation_state"] == "unknown"
    assert result["rollback_verified"] is False


def test_update_lock_refuses_concurrent_mutation(tmp_path: Path) -> None:
    backend = MutationBackend(tmp_path)
    first = NovamiraUpdater(backend=backend, home=tmp_path)
    second = NovamiraUpdater(backend=backend, home=tmp_path)
    with first.mutation_lock():
        try:
            with second.mutation_lock():
                raise AssertionError("second updater acquired the mutation lock")
        except UpdateInProgress:
            pass


def test_apply_stays_unknown_when_owner_snapshot_cannot_be_verified(tmp_path: Path) -> None:
    backend = MutationBackend(tmp_path)
    backend.offline_doctor = lambda: {"status": "fail", "failed_checks": ["doctor"]}
    backend.verify_owner_snapshot = lambda _snapshot: False

    result = NovamiraUpdater(backend=backend, home=tmp_path).apply(confirmed=True)

    assert result["mutation_state"] == "unknown"
    assert result["rollback_verified"] is False


def test_apply_refuses_if_installed_package_changes_during_preflight(
    tmp_path: Path,
) -> None:
    backend = MutationBackend(tmp_path)
    release_calls = 0
    installed_calls = 0

    def registry_release() -> RegistryRelease:
        nonlocal release_calls
        release_calls += 1
        return RegistryRelease(
            version=SUPPORTED_CLI_VERSION if release_calls == 1 else "9.9.9",
            tarball_url=f"https://registry.npmjs.org/@novamira/cli/-/cli-{SUPPORTED_CLI_VERSION}.tgz",
            integrity="sha512-published",
            registry_integrity_ok=True,
            provenance_ok=True,
        )

    def installed_package() -> InstalledPackage:
        nonlocal installed_calls
        installed_calls += 1
        value = backend.version if installed_calls == 1 else "9.9.9"
        return InstalledPackage(
            version=value,
            package_dir=tmp_path / "node_modules" / "@novamira" / "cli",
            owner_manifest=tmp_path / "package.json",
            owner_lock=tmp_path / "bun.lock",
            integrity_clean=True,
        )

    backend.registry_release = registry_release
    backend.installed_package = installed_package

    result = NovamiraUpdater(backend=backend, home=tmp_path).apply(confirmed=True)

    assert result["ok"] is False
    assert result["mutation_state"] == "refused"
    assert result["blockers"] == ["installed_package_changed_during_preflight"]
    assert release_calls == 1
    assert installed_calls == 2
    assert "apply_bun" not in backend.mutation_calls


def test_apply_refuses_dirty_owner_manifest_or_lock(tmp_path: Path) -> None:
    backend = MutationBackend(tmp_path)
    backend.installed_package = lambda: InstalledPackage(
        version="1.0.0",
        package_dir=tmp_path / "node_modules" / "@novamira" / "cli",
        owner_manifest=tmp_path / "package.json",
        owner_lock=tmp_path / "bun.lock",
        integrity_clean=True,
        owner_content_clean=False,
    )

    result = NovamiraUpdater(backend=backend, home=tmp_path).apply(confirmed=True)

    assert result["ok"] is False
    assert result["mutation_state"] == "refused"
    assert result["blockers"] == ["package_owner_files_dirty"]
    assert backend.mutation_calls == []


def test_apply_returns_structured_refusal_when_preapply_step_raises(
    tmp_path: Path,
) -> None:
    backend = MutationBackend(tmp_path)
    backend.verify_candidate = lambda _release: (_ for _ in ()).throw(
        RuntimeError("candidate unavailable")
    )

    result = NovamiraUpdater(backend=backend, home=tmp_path).apply(confirmed=True)

    assert result["ok"] is False
    assert result["mutation_state"] == "refused"
    assert result["blockers"] == ["candidate_verification_failed"]
    assert "apply_bun" not in backend.mutation_calls


class FailingDoctorBackend(MutationBackend):
    def offline_doctor(self) -> dict[str, object]:
        self.mutation_calls.append("offline_doctor")
        return {"status": "fail", "failed_checks": ["runtime.node"]}


def test_a_reviewed_installed_package_is_not_blamed_when_upstream_moves(tmp_path: Path) -> None:
    """Upstream shipping a newer release says nothing about the reviewed one here."""
    from siteground_ops.novamira_update import SUPPORTED_CLI_VERSION

    class UpstreamMoved(FakeBackend):
        def registry_release(self) -> RegistryRelease:
            return RegistryRelease(
                version="9.9.9",
                tarball_url="https://registry.npmjs.org/@novamira/cli/-/cli-9.9.9.tgz",
                integrity="sha512-published",
                registry_integrity_ok=True,
                provenance_ok=True,
            )

        def installed_package(self) -> InstalledPackage:
            return InstalledPackage(
                version=SUPPORTED_CLI_VERSION,
                package_dir=self.home / "node_modules" / "@novamira" / "cli",
                owner_manifest=self.home / "package.json",
                owner_lock=self.home / "bun.lock",
                integrity_clean=True,
            )

    result = NovamiraUpdater(backend=UpstreamMoved(tmp_path), home=tmp_path).check()

    assert result["auto_apply_ready"] is False
    assert result["blockers"] == ["candidate_version_requires_review:9.9.9"]
    assert not any("installed_version_requires_review" in b for b in result["blockers"])
