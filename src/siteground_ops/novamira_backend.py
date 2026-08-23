from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shutil
import ssl
import stat
import subprocess
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Callable

from .novamira_update import (
    DEFAULT_REGISTRY,
    PACKAGE_NAME,
    InstalledPackage,
    RegistryRelease,
    controlled_bun_command,
)


def normalize_cli_json(output: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for index, char in enumerate(output):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(output[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("Novamira did not return a JSON object.")


def normalize_offline_doctor(report: dict[str, Any]) -> dict[str, Any]:
    data = report.get("data") if isinstance(report.get("data"), dict) else {}
    checks = data.get("checks") if isinstance(data.get("checks"), list) else []
    failed = [
        str(check.get("id", "unknown"))
        for check in checks
        if isinstance(check, dict)
        and str(check.get("status", "unknown")).casefold()
        not in {"pass", "warn"}
    ]
    warnings = [
        str(check.get("id", "unknown"))
        for check in checks
        if isinstance(check, dict) and check.get("status") == "warn"
    ]
    report_status = str(data.get("status", "pass")).casefold()
    report_failed = report_status not in {"pass", "warn"}
    return {
        "status": "pass" if report.get("ok") is True and not failed and not report_failed else "fail",
        "failed_checks": failed,
        "warnings": warnings,
    }


@dataclass(frozen=True)
class NovamiraPaths:
    home: Path
    binary: Path
    package_dir: Path
    manifest: Path
    lock: Path
    skill: Path
    bun: Path

    @classmethod
    def from_home(cls, home: Path | None = None) -> "NovamiraPaths":
        root = (home or Path.home()).expanduser().resolve(strict=False)
        return cls(
            home=root,
            binary=root / ".bun/bin/novamira",
            package_dir=root / "node_modules/@novamira/cli",
            manifest=root / "package.json",
            lock=root / "bun.lock",
            skill=root / ".gemini/antigravity/skills/novamira-ops/SKILL.md",
            bun=root / ".bun/bin/bun",
        )


class LocalNovamiraBackend:
    """Real Bun/npm-registry owner for the Novamira policy layer."""

    def __init__(
        self,
        *,
        paths: NovamiraPaths,
        fetcher: Callable[[str], bytes] | None = None,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.paths = paths
        self.fetcher = fetcher or self._fetch
        self.command_runner = command_runner or subprocess.run

    @staticmethod
    def _ssl_context() -> ssl.SSLContext:
        try:
            import certifi

            return ssl.create_default_context(cafile=certifi.where())
        except Exception:
            return ssl.create_default_context()

    @classmethod
    def _fetch(cls, url: str) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": "siteground-ops/0.1"})
        with urllib.request.urlopen(request, context=cls._ssl_context(), timeout=30) as response:
            return response.read()

    def _metadata(self, version: str | None = None) -> dict[str, Any]:
        payload = json.loads(
            self.fetcher(f"{DEFAULT_REGISTRY}/{PACKAGE_NAME.replace('/', '%2f')}")
        )
        if version is None:
            version = payload["dist-tags"]["latest"]
        return payload["versions"][version]

    @staticmethod
    def _dependency_name(name: object) -> bool:
        """Accept only npm package names that stay below node_modules."""
        return isinstance(name, str) and bool(
            re.fullmatch(r"(?:@[a-z0-9._-]+/)?[a-z0-9._-]+", name)
        )

    @staticmethod
    def _mode(path: Path) -> int:
        return stat.S_IMODE(path.stat().st_mode)

    @staticmethod
    def _canonical_child(root: Path, path: Path) -> bool:
        """Reject symlinked or escaping paths before copying package data."""
        root_absolute = Path(os.path.abspath(root))
        path_absolute = Path(os.path.abspath(path))
        root_resolved = root.resolve(strict=False)
        path_resolved = path.resolve(strict=False)
        try:
            path_resolved.relative_to(root_resolved)
        except ValueError:
            return False
        current = path_absolute
        while current != root_absolute:
            if current.is_symlink():
                return False
            current = current.parent
        return True

    def _resolve_npm(self) -> str | None:
        configured = [
            os.environ.get("SITEGROUND_NPM_BIN"),
            os.environ.get("NPM_BIN"),
        ]
        configured.extend(("/opt/homebrew/bin/npm", "/usr/local/bin/npm", "/usr/bin/npm"))
        for value in configured:
            if not value:
                continue
            candidate = Path(value).expanduser()
            if candidate.is_absolute() and candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate.resolve())
        return None

    @staticmethod
    def _sri_digest(raw: bytes, integrity: str) -> str:
        algorithm, expected = integrity.split("-", 1)
        if algorithm != "sha512":
            raise ValueError("Novamira registry integrity must use sha512.")
        actual = base64.b64encode(hashlib.sha512(raw).digest()).decode("ascii")
        if actual != expected:
            raise ValueError("Novamira tarball integrity mismatch.")
        return hashlib.sha512(raw).hexdigest()

    def _download_verified(
        self,
        version: str,
        metadata: dict[str, Any] | None = None,
        *,
        expected_url: str | None = None,
        expected_integrity: str | None = None,
    ) -> bytes:
        metadata = metadata or self._metadata(version)
        dist = metadata["dist"]
        if expected_url is not None and dist.get("tarball") != expected_url:
            raise ValueError("Novamira tarball URL changed during verification.")
        if expected_integrity is not None and dist.get("integrity") != expected_integrity:
            raise ValueError("Novamira tarball integrity changed during verification.")
        raw = self.fetcher(dist["tarball"])
        self._sri_digest(raw, dist["integrity"])
        return raw

    @staticmethod
    def _tar_files(raw: bytes) -> dict[str, bytes]:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
            files: dict[str, bytes] = {}
            for member in archive.getmembers():
                if not member.isfile() or not member.name.startswith("package/"):
                    continue
                handle = archive.extractfile(member)
                if handle is not None:
                    files[member.name.removeprefix("package/")] = handle.read()
            return files

    @staticmethod
    def _extract_package(raw: bytes, destination: Path) -> None:
        """Extract only regular files below the npm package directory.

        This keeps the package staging path safe on Python 3.11 as well as
        newer runtimes where ``tarfile`` has a built-in extraction filter.
        """
        destination.mkdir(parents=True, exist_ok=True)
        root = destination.resolve()
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
            for member in archive.getmembers():
                if not member.name.startswith("package/"):
                    continue
                relative = PurePosixPath(member.name.removeprefix("package/"))
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("Novamira tarball contains an unsafe path.")
                target = (destination / Path(*relative.parts)).resolve()
                if target != root and root not in target.parents:
                    raise ValueError("Novamira tarball escapes the staging directory.")
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise ValueError("Novamira tarball contains a non-regular member.")
                target.parent.mkdir(parents=True, exist_ok=True)
                handle = archive.extractfile(member)
                if handle is None:
                    raise ValueError("Novamira tarball member could not be read.")
                target.write_bytes(handle.read())
                target.chmod(member.mode & 0o7777 or 0o644)

    @staticmethod
    def _package_tree(path: Path) -> dict[str, bytes] | None:
        if path.is_symlink() or not path.is_dir():
            return None
        files: dict[str, bytes] = {}
        for item in path.rglob("*"):
            if item.is_symlink():
                return None
            if item.is_file():
                files[str(item.relative_to(path))] = item.read_bytes()
        return files

    @classmethod
    def _owned_tree_snapshot(cls, path: Path) -> dict[str, Any] | None:
        """Capture regular package files and modes for an exact rollback."""
        if path.is_symlink() or not path.is_dir():
            return None
        files: dict[str, bytes] = {}
        modes: dict[str, int] = {}
        directory_modes: dict[str, int] = {".": cls._mode(path)}
        for item in path.rglob("*"):
            if item.is_symlink():
                return None
            relative = str(item.relative_to(path))
            if item.is_dir():
                directory_modes[relative] = cls._mode(item)
            elif item.is_file():
                files[relative] = item.read_bytes()
                modes[relative] = cls._mode(item)
        return {"files": files, "modes": modes, "directory_modes": directory_modes}

    def _dependency_snapshot(self, package_names: set[str]) -> dict[str, Any]:
        """Snapshot the hoisted dependency closure Bun may touch."""
        snapshots: dict[str, Any] = {}
        pending = list(package_names)
        visited: set[str] = set()
        node_modules = self.paths.home / "node_modules"
        while pending:
            name = pending.pop()
            if name in visited:
                continue
            visited.add(name)
            if not self._dependency_name(name):
                raise ValueError("Novamira dependency metadata contains an unsafe package name.")
            path = node_modules / name
            if not self._canonical_child(node_modules, path) or path.is_symlink() or not path.is_dir():
                raise ValueError("Novamira dependency topology is not owned and regular.")
            snapshot = self._owned_tree_snapshot(path)
            if snapshot is None:
                raise ValueError("Novamira dependency tree contains a symlink.")
            key = str(path.relative_to(self.paths.home))
            snapshots[key] = snapshot
            manifest = path / "package.json"
            try:
                package = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for section in ("dependencies", "optionalDependencies", "peerDependencies"):
                values = package.get(section, {})
                if isinstance(values, dict):
                    pending.extend(str(dep) for dep in values)
        return snapshots

    def _installed_dependency_names(self) -> set[str]:
        try:
            package = json.loads(
                (self.paths.package_dir / "package.json").read_text(encoding="utf-8")
            )
        except FileNotFoundError:
            return set()
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("Installed Novamira package manifest is not valid JSON.") from exc
        names: set[str] = set()
        for section in ("dependencies", "optionalDependencies", "peerDependencies"):
            values = package.get(section, {})
            if not isinstance(values, dict):
                raise ValueError("Novamira package dependency metadata is not an object.")
            for name, spec in values.items():
                if not self._dependency_name(name) or not isinstance(spec, str):
                    raise ValueError("Novamira package dependency metadata contains unsafe data.")
                names.add(name)
        return names

    def _package_integrity(self, version: str) -> bool:
        if self.paths.package_dir.is_symlink() or not self.paths.package_dir.is_dir():
            return False
        try:
            raw = self._download_verified(version)
            expected = self._tar_files(raw)
        except (OSError, KeyError, ValueError, tarfile.TarError):
            return False
        actual = self._package_tree(self.paths.package_dir)
        if actual is not None and not any(key.startswith("node_modules/") for key in expected):
            actual = {
                key: value
                for key, value in actual.items()
                if not key.startswith("node_modules/")
            }
        return actual is not None and actual == expected

    def _owner_dependency_spec(self) -> str | None:
        try:
            manifest = json.loads(self.paths.manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        for section in ("dependencies", "optionalDependencies", "devDependencies"):
            values = manifest.get(section)
            if isinstance(values, dict) and PACKAGE_NAME in values:
                value = values[PACKAGE_NAME]
                return value if isinstance(value, str) else None
        return None

    def _lock_dependency_spec(self) -> str | None:
        try:
            text = self.paths.lock.read_text(encoding="utf-8")
        except OSError:
            return None
        header = text.split('"packages":', 1)[0]
        match = re.search(
            rf'"{re.escape(PACKAGE_NAME)}"\s*:\s*"([^"]+)"', header
        )
        return match.group(1) if match else None

    def _owner_dependency_consistent(self, version: str) -> bool:
        return self._owner_dependency_spec() == version and self._lock_dependency_spec() == version

    def _owner_content_clean(self, version: str) -> bool:
        """Bind future automatic writes to a baseline established from a clean owner."""
        if not self._owner_dependency_consistent(version):
            return False
        try:
            manifest = self.paths.manifest.read_bytes()
            lock = self.paths.lock.read_bytes()
        except OSError:
            return False
        baseline = self._owner_baseline_path()
        if self._canonical_child(self.paths.home, baseline) is False:
            return False
        if not baseline.is_file() or baseline.is_symlink():
            return False
        digest = {
            "version": version,
            "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
            "lock_sha256": hashlib.sha256(lock).hexdigest(),
        }
        try:
            return json.loads(baseline.read_text(encoding="utf-8")) == digest
        except (OSError, json.JSONDecodeError):
            return False

    def _owner_baseline_path(self) -> Path:
        return self.paths.home / ".cache/siteground-ops/novamira-owner-baseline.json"

    def refresh_owner_baseline(self, version: str) -> None:
        """Record the exact owner bytes after a verified successful update."""
        if not self._owner_dependency_consistent(version):
            raise ValueError("Novamira owner files do not match the updated version.")
        baseline = self._owner_baseline_path()
        if not self._canonical_child(self.paths.home, baseline):
            raise ValueError("Novamira owner baseline path is not a canonical child.")
        digest = {
            "version": version,
            "manifest_sha256": hashlib.sha256(self.paths.manifest.read_bytes()).hexdigest(),
            "lock_sha256": hashlib.sha256(self.paths.lock.read_bytes()).hexdigest(),
        }
        baseline.parent.mkdir(parents=True, exist_ok=True)
        temporary = baseline.with_name(baseline.name + f".tmp.{os.getpid()}")
        temporary.write_text(json.dumps(digest, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, baseline)

    def _topology_ok(self) -> bool:
        package_dir = self.paths.home / "node_modules/@novamira/cli"
        manifest = self.paths.home / "package.json"
        lock = self.paths.home / "bun.lock"
        if self.paths.package_dir != package_dir or self.paths.manifest != manifest or self.paths.lock != lock:
            return False
        if any(
            not self._canonical_child(self.paths.home, path)
            for path in (
                package_dir,
                manifest,
                lock,
                self.paths.bun,
                self.paths.home / ".cache/siteground-ops",
            )
        ):
            return False
        if self.paths.binary.parent.resolve(strict=False) != self.paths.binary.parent:
            return False
        if self.paths.package_dir.is_symlink() or not self.paths.package_dir.is_dir():
            return False
        if any(item.is_symlink() for item in self.paths.package_dir.rglob("*")):
            return False
        if any(path.is_symlink() or not path.is_file() for path in (manifest, lock, self.paths.bun)):
            return False
        if not os.access(self.paths.bun, os.X_OK):
            return False
        if not self.paths.binary.exists():
            return False
        try:
            return (
                self.paths.binary.resolve(strict=True) == package_dir / "dist/index.js"
                and self._owner_dependency_consistent(
                    json.loads((package_dir / "package.json").read_text(encoding="utf-8"))["version"]
                )
            )
        except (OSError, KeyError, json.JSONDecodeError):
            return False

    def _verify_npm_signatures(self, version: str, integrity: str, raw: bytes) -> bool:
        npm = self._resolve_npm()
        if npm is None:
            return False
        with tempfile.TemporaryDirectory(prefix="siteground-ops-npm-verify-") as temp:
            root = Path(temp).resolve()
            (root / "package.json").write_text(
                '{"name":"siteground-ops-verifier","version":"0.0.0","private":true}\n',
                encoding="utf-8",
            )
            env = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(root / "home"),
                "NPM_CONFIG_UPDATE_NOTIFIER": "false",
            }
            (root / "home").mkdir()
            install = subprocess.run(
                [npm, "install", "--ignore-scripts", "--no-audit", "--no-fund", "--prefix", str(root), f"{PACKAGE_NAME}@{version}"],
                cwd=str(root), env=env, text=True, capture_output=True, check=False, timeout=120,
            )
            if install.returncode != 0:
                return False
            try:
                lock = json.loads((root / "package-lock.json").read_text(encoding="utf-8"))
                installed = lock["packages"][f"node_modules/{PACKAGE_NAME}"]
                if installed.get("integrity") != integrity:
                    return False
                installed_tree = self._package_tree(root / "node_modules" / "@novamira" / "cli")
                if installed_tree != self._tar_files(raw):
                    return False
            except (OSError, KeyError, json.JSONDecodeError, tarfile.TarError):
                return False
            audit = subprocess.run(
                [npm, "audit", "signatures", "--json", "--include-attestations", "--prefix", str(root)],
                cwd=str(root), env=env, text=True, capture_output=True, check=False, timeout=120,
            )
            try:
                report = normalize_cli_json(audit.stdout)
            except ValueError:
                return False
            verified = [
                item for item in report.get("verified", [])
                if isinstance(item, dict)
                and item.get("name") == PACKAGE_NAME
                and item.get("version") == version
            ]
            if audit.returncode != 0 or not verified or report.get("invalid") or report.get("missing"):
                return False
            expected_hex = hashlib.sha512(raw).hexdigest()
            for item in verified:
                for bundle in item.get("attestationBundles", []):
                    envelope = bundle.get("bundle", {}).get("dsseEnvelope", {})
                    try:
                        payload = json.loads(base64.b64decode(envelope["payload"]).decode("utf-8"))
                    except (KeyError, ValueError, json.JSONDecodeError):
                        continue
                    for subject in payload.get("subject", []):
                        if subject.get("name") == f"pkg:npm/%40novamira/cli@{version}" and subject.get("digest", {}).get("sha512") == expected_hex:
                            return True
            return False

    def registry_release(self) -> RegistryRelease:
        payload = json.loads(self.fetcher(f"{DEFAULT_REGISTRY}/{PACKAGE_NAME.replace('/', '%2f')}"))
        version = payload["dist-tags"]["latest"]
        metadata = payload["versions"][version]
        dist = metadata["dist"]
        raw = self._download_verified(version, metadata)
        artifact_sha512 = self._sri_digest(raw, dist["integrity"])
        provenance_ok = self._verify_npm_signatures(version, dist["integrity"], raw)
        return RegistryRelease(
            version=version,
            tarball_url=dist["tarball"],
            integrity=dist["integrity"],
            registry_integrity_ok=True,
            provenance_ok=provenance_ok,
            tarball_bytes=raw,
            artifact_sha512=artifact_sha512,
        )

    def installed_package(self) -> InstalledPackage:
        package_json = self.paths.package_dir / "package.json"
        try:
            version = json.loads(package_json.read_text(encoding="utf-8"))["version"]
        except (OSError, KeyError, json.JSONDecodeError):
            version = "unknown"
        return InstalledPackage(
            version=str(version),
            package_dir=self.paths.package_dir,
            owner_manifest=self.paths.manifest,
            owner_lock=self.paths.lock,
            integrity_clean=version != "unknown" and self._topology_ok() and self._package_integrity(str(version)),
            owner_content_clean=version != "unknown" and self._topology_ok() and self._owner_content_clean(str(version)),
        )

    def guidance_text(self) -> str:
        return self.paths.skill.read_text(encoding="utf-8")

    def _run(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        restricted: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        command = list(args)
        if restricted:
            sandbox = shutil.which("sandbox-exec")
            if sandbox is None:
                raise RuntimeError("Candidate sandbox is unavailable; refusing staged execution.")
            run_root = (cwd or self.paths.home).resolve(strict=False)
            candidate_home = run_root / ".candidate-home"
            candidate_home.mkdir(parents=True, exist_ok=True)
            env = {
                "PATH": self._safe_path(),
                "HOME": str(candidate_home),
                "TMPDIR": str(candidate_home),
                "NOVAMIRA_HOME": str(candidate_home / "novamira"),
                "NOVAMIRA_UPDATE_CHECK": "0",
                "NO_COLOR": "1",
                "LANG": "C",
                "LC_ALL": "C",
            }
            profile = self._sandbox_profile(run_root, candidate_home)
            command = [sandbox, "-p", profile, *command]
        else:
            env = os.environ.copy()
            env["NOVAMIRA_UPDATE_CHECK"] = "0"
        return self.command_runner(
            command,
            cwd=str(cwd or self.paths.home),
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def _safe_path(self) -> str:
        directories = [
            str(self.paths.bun.parent),
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        ]
        seen: set[str] = set()
        available: list[str] = []
        for path in directories:
            if path in seen:
                continue
            seen.add(path)
            if Path(path).is_dir():
                available.append(path)
        return os.pathsep.join(available)

    @staticmethod
    def _sbpl_path(path: Path) -> str:
        return str(path.resolve(strict=False)).replace("\\", "\\\\").replace('"', '\\"')

    def _sandbox_profile(self, run_root: Path, candidate_home: Path) -> str:
        # Deny network and writes into the real home.  The CLI and Bun are read
        # from their exact owner paths; all writable state is under run_root.
        sensitive = (
            self.paths.home / ".ssh",
            self.paths.home / ".gnupg",
            self.paths.home / ".config",
            self.paths.home / ".local",
            self.paths.home / "Library/Application Support",
            self.paths.home / "Library/Keychains",
            self.paths.home / "Library/LaunchAgents",
            self.paths.home / "Documents",
            self.paths.home / "Downloads",
            self.paths.home / "Desktop",
        )
        rules = ["(version 1)", "(allow default)", "(deny network*)"]
        rules.append(f'(deny file-read* (subpath "{self._sbpl_path(self.paths.home)}"))')
        rules.append(f'(deny file-write* (subpath "{self._sbpl_path(self.paths.home)}"))')
        for path in sensitive:
            rules.append(f'(deny file-read* (subpath "{self._sbpl_path(path)}"))')
        rules.extend(
            [
                f'(allow file-read* (subpath "{self._sbpl_path(run_root)}"))',
                f'(allow file-write* (subpath "{self._sbpl_path(run_root)}"))',
                f'(allow file-read* (literal "{self._sbpl_path(self.paths.bun)}"))',
                f'(allow process-exec (literal "{self._sbpl_path(self.paths.bun)}"))',
                f'(allow file-read* (literal "{self._sbpl_path(self.paths.binary)}"))',
                f'(allow file-read* (subpath "{self._sbpl_path(self.paths.package_dir)}"))',
            ]
        )
        return "".join(rules)

    def _run_json(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        restricted: bool = False,
    ) -> dict[str, Any]:
        result = self._run(args, cwd=cwd, restricted=restricted)
        if result.returncode != 0:
            raise RuntimeError(f"Novamira command failed with exit {result.returncode}.")
        return normalize_cli_json(result.stdout)

    def profile_inventory(self) -> list[dict[str, Any]]:
        with tempfile.TemporaryDirectory(prefix="siteground-ops-profile-") as temp:
            root = Path(temp)
            self._copy_profile_config(root)
            report = self._run_json(
                [str(self.paths.binary), "sites", "list", "--json"],
                cwd=root,
                restricted=True,
            )
        data = report.get("data")
        if not isinstance(data, list):
            raise ValueError("Novamira profile inventory is not a list.")
        allowed = ("name", "siteUrl", "serverVersion", "restContract")
        return [
            {key: item[key] for key in allowed if key in item}
            for item in data
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        ]

    def _copy_profile_config(self, root: Path) -> None:
        """Copy only non-secret profile identities into the scrubbed HOME."""
        destination = root / ".candidate-home" / "novamira" / "config.json"
        candidates = (
            self.paths.home / "Library/Application Support/Novamira/config.json",
            self.paths.home / ".config/novamira/config.json",
        )
        for source in candidates:
            if not source.is_file() or not self._canonical_child(self.paths.home, source):
                continue
            try:
                raw = json.loads(source.read_text(encoding="utf-8"))
                profiles = raw.get("profiles", {})
                safe_profiles = {
                    name: {
                        key: profile[key]
                        for key in ("name", "siteUrl", "serverVersion", "restContract")
                        if key in profile
                    }
                    for name, profile in profiles.items()
                    if isinstance(name, str) and isinstance(profile, dict)
                }
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(
                    json.dumps({"version": raw.get("version", 1), "profiles": safe_profiles}) + "\n",
                    encoding="utf-8",
                )
                destination.chmod(0o600)
                return
            except (OSError, json.JSONDecodeError):
                return

    def snapshot_owner(self, installed: InstalledPackage) -> dict[str, Any]:
        package_snapshot = self._owned_tree_snapshot(self.paths.package_dir)
        if self.paths.package_dir.exists() and package_snapshot is None:
            raise ValueError("Installed Novamira package topology is not owned and regular.")
        dependency_names = self._installed_dependency_names()
        snapshot = {
            "previous_version": installed.version,
            "manifest": self.paths.manifest.read_bytes() if self.paths.manifest.exists() else None,
            "manifest_mode": self._mode(self.paths.manifest) if self.paths.manifest.exists() else None,
            "lock": self.paths.lock.read_bytes() if self.paths.lock.exists() else None,
            "lock_mode": self._mode(self.paths.lock) if self.paths.lock.exists() else None,
        }
        baseline = self._owner_baseline_path()
        snapshot["owner_baseline"] = baseline.read_bytes() if baseline.exists() else None
        snapshot["owner_baseline_mode"] = self._mode(baseline) if baseline.exists() else None
        if package_snapshot is not None:
            snapshot.update(
                {
                    "package_files": package_snapshot["files"],
                    "package_modes": package_snapshot["modes"],
                    "package_directory_modes": package_snapshot["directory_modes"],
                }
            )
        snapshot["dependency_trees"] = self._dependency_snapshot(dependency_names)
        return snapshot

    @staticmethod
    def _atomic_write(path: Path, value: bytes | None, mode: int | None = None) -> None:
        if value is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_bytes(value)
        if mode is not None:
            temporary.chmod(mode)
        os.replace(temporary, path)

    def restore_owner_snapshot(self, snapshot: dict[str, Any]) -> None:
        dependency_trees = snapshot.get("dependency_trees")
        if isinstance(dependency_trees, dict):
            for relative, tree in dependency_trees.items():
                if not isinstance(relative, str) or not isinstance(tree, dict):
                    raise ValueError("Novamira dependency snapshot is malformed.")
                self._restore_owned_tree(self.paths.home / relative, tree)
        package_files = snapshot.get("package_files")
        if isinstance(package_files, dict):
            self._restore_package_files(
                package_files,
                snapshot.get("package_modes"),
                snapshot.get("package_directory_modes"),
            )
        self._atomic_write(self.paths.manifest, snapshot.get("manifest"), snapshot.get("manifest_mode"))
        self._atomic_write(self.paths.lock, snapshot.get("lock"), snapshot.get("lock_mode"))
        self._atomic_write(
            self._owner_baseline_path(),
            snapshot.get("owner_baseline"),
            snapshot.get("owner_baseline_mode"),
        )

    def _restore_owned_tree(self, path: Path, tree: dict[str, Any]) -> None:
        files = tree.get("files")
        if not isinstance(files, dict) or not self._canonical_child(self.paths.home / "node_modules", path):
            raise ValueError("Novamira owned tree snapshot is unsafe.")
        temporary = path.parent / f".restore-{path.name}-{os.getpid()}"
        backup = path.parent / f".restore-backup-{path.name}-{os.getpid()}"
        if temporary.exists() or backup.exists() or temporary.is_symlink() or backup.is_symlink():
            raise RuntimeError("Novamira restore staging path already exists.")
        temporary.mkdir(parents=True)
        try:
            for relative, content in files.items():
                target = (temporary / relative).resolve()
                if temporary.resolve() not in target.parents or not isinstance(content, bytes):
                    raise ValueError("Novamira snapshot contains an unsafe file.")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                modes = tree.get("modes", {})
                if isinstance(modes, dict) and isinstance(modes.get(relative), int):
                    target.chmod(modes[relative])
            directory_modes = tree.get("directory_modes", {})
            if isinstance(directory_modes, dict):
                for relative, mode in directory_modes.items():
                    target = temporary if relative == "." else (temporary / relative).resolve()
                    if temporary.resolve() not in target.parents and target != temporary.resolve():
                        raise ValueError("Novamira snapshot contains an unsafe directory.")
                    if target.is_dir() and isinstance(mode, int):
                        target.chmod(mode)
            if path.exists() or path.is_symlink():
                os.replace(path, backup)
            os.replace(temporary, path)
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=False)
        except Exception:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
            if backup.exists() and not path.exists():
                os.replace(backup, path)
            raise

    def _restore_package_files(
        self,
        package_files: dict[str, bytes],
        package_modes: object = None,
        package_directory_modes: object = None,
    ) -> None:
        parent = self.paths.package_dir.parent
        temporary = parent / f".cli-restore-{os.getpid()}"
        if temporary.exists() or temporary.is_symlink():
            raise RuntimeError("Novamira restore staging path already exists.")
        temporary.mkdir(parents=True)
        try:
            for relative, content in package_files.items():
                target = (temporary / relative).resolve()
                if temporary.resolve() not in target.parents:
                    raise ValueError("Novamira snapshot contains an unsafe path.")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                if isinstance(package_modes, dict) and isinstance(package_modes.get(relative), int):
                    target.chmod(package_modes[relative])
            if isinstance(package_directory_modes, dict):
                for relative, mode in package_directory_modes.items():
                    if relative == "." or not isinstance(mode, int):
                        continue
                    directory = (temporary / relative).resolve()
                    if temporary.resolve() not in directory.parents:
                        raise ValueError("Novamira snapshot contains an unsafe directory path.")
                    if directory.is_dir():
                        directory.chmod(mode)
            if isinstance(package_directory_modes, dict) and isinstance(package_directory_modes.get("."), int):
                temporary.chmod(package_directory_modes["."])
            backup = parent / f".cli-restore-backup-{os.getpid()}"
            if backup.exists() or backup.is_symlink():
                raise RuntimeError("Novamira restore backup path already exists.")
            if self.paths.package_dir.exists() or self.paths.package_dir.is_symlink():
                os.replace(self.paths.package_dir, backup)
            os.replace(temporary, self.paths.package_dir)
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=False)
        except Exception:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
            raise

    def verify_owner_snapshot(self, snapshot: dict[str, Any]) -> bool:
        """Confirm that rollback restored the exact files we own."""
        if not isinstance(snapshot, dict):
            return False
        try:
            manifest = snapshot["manifest"]
            lock = snapshot["lock"]
        except KeyError:
            return False
        package_ok = True
        if "package_files" in snapshot:
            current = self._owned_tree_snapshot(self.paths.package_dir)
            package_ok = (
                current is not None
                and current["files"] == snapshot["package_files"]
                and current["modes"] == snapshot.get("package_modes", current["modes"])
                and current["directory_modes"]
                == snapshot.get("package_directory_modes", current["directory_modes"])
            )
        dependency_ok = True
        dependency_trees = snapshot.get("dependency_trees")
        if isinstance(dependency_trees, dict):
            for relative, expected in dependency_trees.items():
                current = self._owned_tree_snapshot(self.paths.home / relative)
                dependency_ok = dependency_ok and current == expected
        return package_ok and (
            (self.paths.manifest.read_bytes() if self.paths.manifest.exists() else None)
            == manifest
            and (self._mode(self.paths.manifest) if self.paths.manifest.exists() else None)
            == snapshot.get("manifest_mode")
            and (self.paths.lock.read_bytes() if self.paths.lock.exists() else None)
            == lock
            and (self._mode(self.paths.lock) if self.paths.lock.exists() else None)
            == snapshot.get("lock_mode")
            and (
                self._owner_baseline_path().read_bytes()
                if self._owner_baseline_path().exists()
                else None
            )
            == snapshot.get("owner_baseline")
            and (
                self._mode(self._owner_baseline_path())
                if self._owner_baseline_path().exists()
                else None
            )
            == snapshot.get("owner_baseline_mode")
        ) and dependency_ok

    def _write_exact_manifest(self, version: str) -> None:
        try:
            payload = json.loads(self.paths.manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("Novamira owner manifest is not valid JSON.") from exc
        sections = ("dependencies", "optionalDependencies", "devDependencies")
        section_name = next(
            (
                section
                for section in sections
                if isinstance(payload.get(section), dict) and PACKAGE_NAME in payload[section]
            ),
            None,
        )
        if section_name is None:
            raise ValueError("Novamira owner manifest has no package dependency entry.")
        payload[section_name][PACKAGE_NAME] = version
        data = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        self._atomic_write(self.paths.manifest, data, self._mode(self.paths.manifest))

    @staticmethod
    def _lock_package_entry(package_json: dict[str, Any], integrity: str, version: str) -> str:
        metadata = {
            key: package_json[key]
            for key in (
                "dependencies",
                "optionalDependencies",
                "peerDependencies",
                "peerDependenciesMeta",
                "bin",
                "engines",
            )
            if key in package_json
        }
        return (
            f'    {json.dumps(PACKAGE_NAME)}: '
            f'[{json.dumps(f"{PACKAGE_NAME}@{version}")}, "", '
            f'{json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))}, '
            f'{json.dumps(integrity)}],'
        )

    def _write_exact_lock(self, package_json: dict[str, Any], integrity: str, version: str) -> None:
        try:
            original = self.paths.lock.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError("Novamira owner lockfile is missing.") from exc
        if '"packages":' not in original:
            raise ValueError("Novamira owner lockfile has an unsupported format.")
        header, packages = original.split('"packages":', 1)
        dependency_pattern = re.compile(
            rf'("{re.escape(PACKAGE_NAME)}"\s*:\s*)"[^"]+"'
        )
        header, replaced = dependency_pattern.subn(rf'\g<1>{json.dumps(version)}', header, count=1)
        if replaced != 1:
            raise ValueError("Novamira owner lockfile has no package dependency entry.")
        entry = self._lock_package_entry(package_json, integrity, version)
        entry_pattern = re.compile(rf'^\s*"{re.escape(PACKAGE_NAME)}"\s*:\s*\[.*$', re.MULTILINE)
        packages, package_replaced = entry_pattern.subn(entry, packages, count=1)
        if package_replaced != 1:
            # A valid lock may have been produced before this package was added.
            packages = " {\n" + entry + "\n" + packages.lstrip(" \n")
        data = (header + '"packages":' + packages).encode("utf-8")
        self._atomic_write(self.paths.lock, data, self._mode(self.paths.lock))

    def _replace_installed_package(self, source: Path) -> None:
        if not source.is_dir() or source.is_symlink():
            raise RuntimeError("Bun did not produce an owned Novamira package.")
        parent = self.paths.package_dir.parent
        if not self._canonical_child(self.paths.home, self.paths.package_dir):
            raise ValueError("Novamira package path is not a canonical child of the owner home.")
        temporary = parent / f".cli-install-{os.getpid()}"
        backup = parent / f".cli-install-backup-{os.getpid()}"
        if temporary.exists() or temporary.is_symlink() or backup.exists() or backup.is_symlink():
            raise RuntimeError("Novamira install staging path already exists.")
        shutil.copytree(source, temporary, symlinks=False)
        try:
            if self.paths.package_dir.exists() or self.paths.package_dir.is_symlink():
                os.replace(self.paths.package_dir, backup)
            os.replace(temporary, self.paths.package_dir)
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=False)
        except Exception:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
            if backup.exists() and not self.paths.package_dir.exists():
                os.replace(backup, self.paths.package_dir)
            raise

    def verify_candidate(self, release: RegistryRelease) -> dict[str, Any]:
        raw = release.tarball_bytes
        if raw is None or release.artifact_sha512 != hashlib.sha512(raw).hexdigest():
            return {"registry_integrity_ok": False, "offline_doctor_ok": False}
        try:
            self._sri_digest(raw, release.integrity)
        except (ValueError, TypeError):
            return {"registry_integrity_ok": False, "offline_doctor_ok": False}
        files = self._tar_files(raw)
        try:
            package_json = json.loads(files["package.json"].decode("utf-8"))
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Novamira tarball has no valid package manifest.") from exc
        if package_json.get("version") != release.version:
            return {"registry_integrity_ok": False, "offline_doctor_ok": False}
        dependencies = package_json.get("dependencies", {})
        if not isinstance(dependencies, dict) or any(
            not self._dependency_name(name) or not isinstance(spec, str)
            for name, spec in dependencies.items()
        ):
            return {"registry_integrity_ok": True, "offline_doctor_ok": False}
        with tempfile.TemporaryDirectory(prefix="siteground-ops-novamira-") as temp:
            stage = Path(temp) / "package"
            stage.mkdir()
            self._extract_package(raw, stage)
            node_modules = stage / "node_modules"
            node_modules.mkdir()
            package_json = json.loads((stage / "package.json").read_text(encoding="utf-8"))
            for dependency in dependencies:
                source = self.paths.home / "node_modules" / dependency
                if (
                    not self._canonical_child(self.paths.home / "node_modules", source)
                    or source.is_symlink()
                    or not source.is_dir()
                    or self._package_tree(source) is None
                ):
                    return {"registry_integrity_ok": True, "offline_doctor_ok": False}
                destination = node_modules / dependency
                if not self._canonical_child(node_modules, destination):
                    return {"registry_integrity_ok": True, "offline_doctor_ok": False}
                shutil.copytree(source, destination, symlinks=False)
            version_result = self._run(
                [str(self.paths.bun), str(stage / "dist/index.js"), "--version"],
                cwd=stage,
                restricted=True,
            )
            doctor_result = self._run(
                [
                    str(self.paths.bun),
                    str(stage / "dist/index.js"),
                    "doctor",
                    "--offline",
                    "--json",
                ],
                cwd=stage,
                restricted=True,
            )
            try:
                doctor_report = normalize_offline_doctor(
                    normalize_cli_json(doctor_result.stdout)
                )
            except ValueError:
                doctor_report = {"status": "fail", "failed_checks": []}
            offline_ok = (
                version_result.returncode == 0
                and release.version in version_result.stdout
                and doctor_result.returncode == 0
                and doctor_report["status"] == "pass"
                and not doctor_report["failed_checks"]
            )
        return {"registry_integrity_ok": True, "offline_doctor_ok": offline_ok}

    def apply_bun(self, release: RegistryRelease, command: tuple[str, ...]) -> tuple[str, ...]:
        expected = controlled_bun_command(release.version)
        if command != expected:
            raise ValueError("Novamira update command is outside the controlled allowlist.")
        if release.tarball_bytes is None or release.artifact_sha512 != hashlib.sha512(release.tarball_bytes).hexdigest():
            raise ValueError("Novamira update artifact is not pinned.")
        self._sri_digest(release.tarball_bytes, release.integrity)
        package_files = self._tar_files(release.tarball_bytes)
        try:
            package_json = json.loads(package_files["package.json"].decode("utf-8"))
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Novamira update artifact has no valid package manifest.") from exc
        dependencies = package_json.get("dependencies", {})
        if not isinstance(dependencies, dict) or any(
            not self._dependency_name(name) or not isinstance(spec, str)
            for name, spec in dependencies.items()
        ):
            raise ValueError("Novamira update artifact contains unsafe dependency names.")
        cache_dir = self.paths.home / ".cache/siteground-ops"
        cache_dir.mkdir(parents=True, exist_ok=True)
        # Bun is intentionally run in an owned prefix.  The shared home root
        # is updated only after Bun succeeds, so sibling dependencies cannot be
        # rewritten behind the updater's rollback boundary.
        with tempfile.TemporaryDirectory(prefix="novamira-install-", dir=cache_dir) as temp:
            prefix = Path(temp)
            (prefix / "package.json").write_text(
                json.dumps(
                    {"name": "siteground-ops-novamira-install", "private": True},
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            artifact = prefix / "novamira-cli.tgz"
            artifact.write_bytes(release.tarball_bytes)
            actual_command = [
                str(self.paths.bun),
                "add",
                "--exact",
                "--ignore-scripts",
                "--no-save",
                str(artifact),
            ]
            result = self._run(actual_command, cwd=prefix)
            if result.returncode != 0:
                raise RuntimeError("Controlled Bun package install failed.")
            candidate = prefix / "node_modules" / "@novamira" / "cli"
            candidate_tree = self._package_tree(candidate)
            expected_tree = self._tar_files(release.tarball_bytes)
            if candidate_tree != expected_tree:
                raise RuntimeError("Bun installed bytes differ from the verified artifact.")
            candidate_dependencies = candidate / "node_modules"
            candidate_dependencies.mkdir(parents=True, exist_ok=True)
            installed_root = prefix / "node_modules"
            dependency_roots = []
            for dependency_root in installed_root.iterdir():
                if dependency_root.name == ".bin":
                    continue
                if dependency_root.name == "@novamira":
                    dependency_roots.extend(
                        child for child in dependency_root.iterdir() if child.name != "cli"
                    )
                else:
                    dependency_roots.append(dependency_root)
            for dependency_root in dependency_roots:
                if dependency_root.is_symlink() or not dependency_root.is_dir():
                    raise RuntimeError("Bun produced an unsafe dependency tree.")
                relative = dependency_root.relative_to(installed_root)
                destination = candidate_dependencies / relative
                if self._package_tree(dependency_root) is None or not self._canonical_child(
                    candidate_dependencies, destination
                ):
                    raise RuntimeError("Bun produced an unsafe dependency tree.")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(dependency_root, destination, symlinks=False)
            self._replace_installed_package(candidate)
            self._write_exact_manifest(release.version)
            self._write_exact_lock(package_json, release.integrity, release.version)
            return tuple(actual_command)

    def offline_doctor(self) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="siteground-ops-doctor-") as temp:
            root = Path(temp)
            self._copy_profile_config(root)
            report = self._run_json(
                [str(self.paths.binary), "doctor", "--offline", "--json"],
                cwd=root,
                restricted=True,
            )
        return normalize_offline_doctor(report)

    def rollback(self, snapshot: dict[str, Any]) -> None:
        self.restore_owner_snapshot(snapshot)
