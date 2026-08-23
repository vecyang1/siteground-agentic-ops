from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
from pathlib import Path
from typing import Any, Protocol


SUPPORTED_CLI_VERSION = "1.0.3"
SUPPORTED_SERVER_VERSION = "1.11.1"
PACKAGE_NAME = "@novamira/cli"
DEFAULT_REGISTRY = "https://registry.npmjs.org"


@dataclass(frozen=True)
class RegistryRelease:
    version: str
    tarball_url: str
    integrity: str
    registry_integrity_ok: bool
    provenance_ok: bool
    tarball_bytes: bytes | None = None
    artifact_sha512: str | None = None


@dataclass(frozen=True)
class InstalledPackage:
    version: str
    package_dir: Path
    owner_manifest: Path
    owner_lock: Path
    integrity_clean: bool
    owner_content_clean: bool = True


class NovamiraBackend(Protocol):
    def registry_release(self) -> RegistryRelease: ...

    def installed_package(self) -> InstalledPackage: ...

    def guidance_text(self) -> str: ...

    def verify_candidate(self, release: RegistryRelease) -> dict[str, Any]: ...

    def profile_inventory(self) -> Any: ...

    def snapshot_owner(self, installed: InstalledPackage) -> Any: ...

    def apply_bun(
        self, release: RegistryRelease, command: tuple[str, ...]
    ) -> tuple[str, ...] | None: ...

    def offline_doctor(self) -> dict[str, Any]: ...

    def rollback(self, snapshot: Any) -> None: ...

    def verify_owner_snapshot(self, snapshot: Any) -> bool: ...

    def refresh_owner_baseline(self, version: str) -> None: ...


def controlled_bun_command(
    version: str,
    registry: str = DEFAULT_REGISTRY,
    artifact: Path | None = None,
) -> tuple[str, ...]:
    if artifact is not None:
        return (
            "bun",
            "add",
            "--exact",
            "--ignore-scripts",
            "--no-save",
            str(artifact),
        )
    return (
        "bun",
        "add",
        "--exact",
        "--ignore-scripts",
        "--registry",
        registry,
        f"{PACKAGE_NAME}@{version}",
    )


def _compatible_guidance(text: str) -> bool:
    normalized = text.casefold()
    return (
        SUPPORTED_SERVER_VERSION in normalized
        and "full access" in normalized
        and "--access read" not in normalized
    )


def _same_path(left: Path, right: Path) -> bool:
    return left.expanduser().resolve(strict=False) == right.expanduser().resolve(strict=False)


class NovamiraUpdater:
    def __init__(self, *, backend: NovamiraBackend, home: Path | None = None) -> None:
        self.backend = backend
        self.home = (home or Path.home()).expanduser().resolve(strict=False)

    def check(self) -> dict[str, Any]:
        release = self.backend.registry_release()
        installed = self.backend.installed_package()
        return self._readiness(release, installed)

    def initialize_baseline(self, *, confirmed: bool = False) -> dict[str, Any]:
        """Explicitly trust the current verified owner files for this machine."""
        release = self.backend.registry_release()
        installed = self.backend.installed_package()
        if release.version != SUPPORTED_CLI_VERSION:
            return self._refusal(
                self._readiness(release, installed),
                [f"candidate_version_requires_review:{release.version}"],
            )
        if installed.version != release.version or not installed.integrity_clean:
            return self._refusal(
                self._readiness(release, installed),
                ["verified_current_package_required"],
            )
        if not confirmed:
            return self._refusal(
                self._readiness(release, installed),
                ["explicit_confirmation_required"],
            )
        try:
            self.backend.refresh_owner_baseline(installed.version)
            after = self.backend.installed_package()
        except Exception:
            return self._refusal(
                self._readiness(release, installed),
                ["baseline_write_failed"],
            )
        if not after.owner_content_clean:
            return self._refusal(
                self._readiness(release, after),
                ["baseline_readback_failed"],
            )
        return {
            "ok": True,
            "mutation_state": "applied",
            "current": installed.version,
            "latest": release.version,
            "baseline_initialized": True,
        }

    @contextmanager
    def mutation_lock(self):
        lock_path = self.home / ".cache/siteground-ops/novamira-update.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise UpdateInProgress("Novamira update is already running.") from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _readiness(
        self, release: RegistryRelease, installed: InstalledPackage
    ) -> dict[str, Any]:
        blockers: list[str] = []

        if release.version != SUPPORTED_CLI_VERSION:
            blockers.append(f"candidate_version_requires_review:{release.version}")
        if not release.registry_integrity_ok:
            blockers.append("candidate_registry_integrity_failed")
        if not release.provenance_ok:
            blockers.append("candidate_provenance_missing")
        if installed.version not in {"1.0.0", release.version}:
            blockers.append(f"installed_version_requires_review:{installed.version}")
        if not installed.integrity_clean:
            blockers.append("installed_package_integrity_failed")
        if not installed.owner_content_clean:
            blockers.append("package_owner_files_dirty")
        if not _same_path(installed.owner_manifest, self.home / "package.json"):
            blockers.append("package_owner_manifest_mismatch")
        if not _same_path(installed.owner_lock, self.home / "bun.lock"):
            blockers.append("package_owner_lock_mismatch")
        if not _same_path(installed.package_dir, self.home / "node_modules/@novamira/cli"):
            blockers.append("package_directory_owner_mismatch")
        if not _compatible_guidance(self.backend.guidance_text()):
            blockers.append("local_novamira_ops_guidance_incompatible")

        return {
            "ok": True,
            "mutation_state": "not_applicable",
            "current": installed.version,
            "latest": release.version,
            "update_available": release.version != installed.version,
            "auto_apply_ready": not blockers,
            "blockers": blockers,
        }

    def apply(self, *, confirmed: bool = False) -> dict[str, Any]:
        try:
            with self.mutation_lock():
                return self._apply_unlocked(confirmed=confirmed)
        except UpdateInProgress:
            return {
                "ok": False,
                "mutation_state": "refused",
                "auto_apply_ready": False,
                "blockers": ["update_in_progress"],
            }

    def _apply_unlocked(self, *, confirmed: bool = False) -> dict[str, Any]:
        # Keep the objects used for readiness and mutation identical. A second
        # registry/package read here would create a TOCTOU window.
        release = self.backend.registry_release()
        installed = self.backend.installed_package()
        readiness = self._readiness(release, installed)
        if not readiness["auto_apply_ready"]:
            return self._refusal(readiness, readiness["blockers"])
        if not readiness["update_available"]:
            return {
                **readiness,
                "ok": True,
                "mutation_state": "not_applicable",
                "result": "up_to_date",
            }
        if not confirmed:
            return {
                **readiness,
                "ok": False,
                "mutation_state": "refused",
                "auto_apply_ready": False,
                "blockers": ["explicit_confirmation_required"],
            }

        try:
            candidate = self.backend.verify_candidate(release)
            candidate_ok = (
                isinstance(candidate, dict)
                and candidate.get("registry_integrity_ok") is True
                and candidate.get("offline_doctor_ok") is True
            )
        except Exception:
            return self._refusal(readiness, ["candidate_verification_failed"])
        if not candidate_ok:
            return self._refusal(readiness, ["candidate_verification_failed"])

        try:
            profiles_before = self.backend.profile_inventory()
        except Exception:
            return self._refusal(readiness, ["profile_inventory_failed"])
        try:
            snapshot = self.backend.snapshot_owner(installed)
        except Exception:
            return self._refusal(readiness, ["snapshot_failed"])
        try:
            pre_mutation = self.backend.installed_package()
        except Exception:
            return self._refusal(readiness, ["installed_package_recheck_failed"])
        if pre_mutation != installed:
            return self._refusal(readiness, ["installed_package_changed_during_preflight"])
        command = controlled_bun_command(release.version)
        rollback_error: str | None = None
        try:
            applied_command = self.backend.apply_bun(release, command)
            after = self.backend.installed_package()
            doctor = self.backend.offline_doctor()
            profiles_after = self.backend.profile_inventory()
            failures: list[str] = []
            if after.version != release.version:
                failures.append("installed_version_readback_failed")
            if not after.integrity_clean:
                failures.append("installed_package_integrity_failed")
            if doctor.get("status") != "pass" or doctor.get("failed_checks"):
                failures.append("offline_doctor_failed")
            if profiles_after != profiles_before:
                failures.append("profile_inventory_changed")
            if failures:
                raise _PostApplyVerificationError(failures)
            self.backend.refresh_owner_baseline(release.version)
        except Exception as exc:
            try:
                self.backend.rollback(snapshot)
                rollback_after = self.backend.installed_package()
                rollback_profiles = self.backend.profile_inventory()
                owner_snapshot_verified = self.backend.verify_owner_snapshot(snapshot)
                rollback_ok = (
                    rollback_after.version == installed.version
                    and rollback_after.integrity_clean
                    and rollback_profiles == profiles_before
                    and owner_snapshot_verified is True
                )
            except Exception:
                rollback_error = "rollback_failed"
                rollback_ok = False
            rollback_blockers = (
                []
                if rollback_ok
                else [rollback_error or "rollback_verification_failed"]
            )
            return {
                **readiness,
                "ok": False,
                "mutation_state": "rolled_back" if rollback_ok else "unknown",
                "current": installed.version if rollback_ok else None,
                "latest": release.version,
                "auto_apply_ready": False,
                "blockers": (
                    exc.failures
                    if isinstance(exc, _PostApplyVerificationError)
                    else ["update_failed"]
                )
                + rollback_blockers,
                "rollback_verified": rollback_ok,
                "recovery_error": rollback_error,
            }

        return {
            **readiness,
            "ok": True,
            "mutation_state": "applied",
            "current": release.version,
            "previous": installed.version,
            "candidate_verified": True,
            "rollback_verified": False,
            "profiles_preserved": True,
            "bun_command": list(applied_command or command),
        }

    @staticmethod
    def _refusal(readiness: dict[str, Any], blockers: list[str]) -> dict[str, Any]:
        return {
            **readiness,
            "ok": False,
            "mutation_state": "refused",
            "auto_apply_ready": False,
            "blockers": blockers,
        }


class _PostApplyVerificationError(RuntimeError):
    def __init__(self, failures: list[str]) -> None:
        super().__init__("post-apply verification failed")
        self.failures = failures


class UpdateInProgress(RuntimeError):
    """Another process owns the global Bun/package mutation transaction."""
