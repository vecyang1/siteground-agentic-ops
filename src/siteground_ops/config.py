from __future__ import annotations

import json
import re
from urllib.parse import urlsplit
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    pass


SECRET_KEY = re.compile(
    r"(?:^|_)(?:password|passphrase|passwd|secret|token|private_key|api_key|authorization)(?:$|_)",
    re.IGNORECASE,
)
ALLOWED_SITE_FIELDS = {
    "label",
    "environment",
    "adapter",
    "env_file",
    "key_file",
    "remote_path",
    "credential_pointer",
    "recovery_pointer",
    "public_url",
    "novamira_server",
    "portal_account",
    "portal_site_id",
    "portal_plan_id",
}
ALLOWED_PORTAL_ACCOUNT_FIELDS = {
    "label",
    "adapter",
    "opencli_path",
    "opencli_profile",
    "credential_pointer",
    "expected_domains",
}
PORTAL_ID = re.compile(r"[A-Za-z0-9_-]{8,128}")


@dataclass(frozen=True)
class SiteConfig:
    site_id: str
    label: str
    environment: str
    adapter: str
    credential_pointer: str
    recovery_pointer: str
    public_url: str
    env_file: Path | None = None
    key_file: Path | None = None
    remote_path: str | None = None
    novamira_server: str | None = None
    portal_account: str | None = None
    portal_site_id: str | None = None
    portal_plan_id: str | None = None

    @property
    def ssh_missing(self) -> list[str]:
        missing: list[str] = []
        if self.env_file is None or not self.env_file.is_file():
            missing.append("env_file")
        if self.key_file is None or not self.key_file.is_file():
            missing.append("key_file")
        if not self.remote_path:
            missing.append("remote_path")
        return missing

    @property
    def ssh_ready(self) -> bool:
        return not self.ssh_missing

    @property
    def ssh_read_ready(self) -> bool:
        return self.adapter == "paramiko_wpcli" and self.ssh_ready

    @property
    def novamira_ready(self) -> bool:
        return self.novamira_server is not None

    @property
    def read_ready(self) -> bool:
        return self.ssh_read_ready or self.novamira_ready

    @property
    def missing(self) -> list[str]:
        if self.adapter == "portal_only":
            return []
        if self.adapter == "novamira_mcp":
            return [] if self.novamira_ready else ["novamira_server"]
        return self.ssh_missing

    @property
    def ready(self) -> bool:
        if self.adapter == "portal_only":
            return False
        if self.adapter == "novamira_mcp":
            return self.novamira_ready
        return self.ssh_ready

    @property
    def read_transports(self) -> list[str]:
        transports: list[str] = []
        if self.ssh_read_ready:
            transports.append("ssh")
        if self.novamira_ready:
            transports.append("novamira")
        return transports

    def public_summary(
        self,
        *,
        read_transports: list[str] | None = None,
        read_missing: dict[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        available = list(self.read_transports if read_transports is None else read_transports)
        primary_transport = {
            "paramiko_wpcli": "ssh",
            "novamira_mcp": "novamira",
        }.get(self.adapter)
        missing = list(self.missing)
        if primary_transport is not None and primary_transport not in available and read_missing:
            for issue in read_missing.get(primary_transport, []):
                if issue not in missing:
                    missing.append(issue)
        return {
            "adapter": self.adapter,
            "environment": self.environment,
            "id": self.site_id,
            "label": self.label,
            "public_url": self.public_url,
            "ready": primary_transport is not None and primary_transport in available,
            "missing": missing,
            "read_ready": bool(available),
            "read_transports": available,
            "portal_mapped": bool(self.portal_account and self.portal_site_id),
        }


@dataclass(frozen=True)
class PortalAccountConfig:
    account_id: str
    label: str
    adapter: str
    opencli_path: Path
    opencli_profile: str
    credential_pointer: str
    expected_domains: tuple[str, ...]

    def public_summary(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "credential_pointer": self.credential_pointer,
            "expected_domains": list(self.expected_domains),
            "id": self.account_id,
            "label": self.label,
            "opencli_path": str(self.opencli_path),
            "opencli_profile": self.opencli_profile,
            "ready": self.opencli_path.is_file(),
        }


@dataclass(frozen=True)
class OpsConfig:
    schema_version: int
    sites: dict[str, SiteConfig]
    portal_accounts: dict[str, PortalAccountConfig]


def _required_text(data: dict[str, Any], name: str, site_id: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"Site {site_id!r} requires non-empty {name!r}.")
    return value.strip()


def _absolute_path(data: dict[str, Any], name: str, site_id: str) -> Path:
    value = Path(_required_text(data, name, site_id)).expanduser()
    if not value.is_absolute():
        raise ConfigError(f"Site {site_id!r} field {name!r} must be an absolute path.")
    return value


def _optional_absolute_path(data: dict[str, Any], name: str, site_id: str) -> Path | None:
    if data.get(name) is None:
        return None
    return _absolute_path(data, name, site_id)


def _public_url(data: dict[str, Any], site_id: str) -> str:
    value = _required_text(data, "public_url", site_id).rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ConfigError(f"Site {site_id!r} field 'public_url' must be an HTTPS origin without credentials.")
    if parsed.query or parsed.fragment:
        raise ConfigError(f"Site {site_id!r} field 'public_url' must not contain query or fragment data.")
    return value


def _novamira_server(data: dict[str, Any], site_id: str) -> str | None:
    value = data.get("novamira_server")
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,127}", value):
        raise ConfigError(
            f"Site {site_id!r} field 'novamira_server' must be an exact non-secret MCP server name."
        )
    return value


def _optional_portal_id(data: dict[str, Any], name: str, site_id: str) -> str | None:
    value = data.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not PORTAL_ID.fullmatch(value):
        raise ConfigError(f"Site {site_id!r} field {name!r} must be an exact non-secret provider id.")
    return value


def _load_portal_accounts(raw: dict[str, Any], schema_version: int) -> dict[str, PortalAccountConfig]:
    if schema_version == 1:
        if raw.get("portal_accounts") is not None:
            raise ConfigError("Configuration schema_version 1 cannot contain 'portal_accounts'.")
        return {}
    raw_accounts = raw.get("portal_accounts")
    if not isinstance(raw_accounts, dict):
        raise ConfigError("Configuration schema_version 2 requires object field 'portal_accounts'.")

    accounts: dict[str, PortalAccountConfig] = {}
    for account_id, data in raw_accounts.items():
        if not isinstance(account_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", account_id):
            raise ConfigError(f"Invalid portal account id: {account_id!r}.")
        if not isinstance(data, dict):
            raise ConfigError(f"Portal account {account_id!r} must be an object.")
        for key in data:
            if SECRET_KEY.search(key):
                raise ConfigError(
                    f"Portal account {account_id!r} contains secret-like field {key!r}; store a pointer instead."
                )
            if key not in ALLOWED_PORTAL_ACCOUNT_FIELDS:
                raise ConfigError(f"Portal account {account_id!r} contains unsupported field {key!r}.")
        adapter = _required_text(data, "adapter", account_id)
        if adapter != "opencli":
            raise ConfigError(f"Portal account {account_id!r} uses unsupported adapter {adapter!r}.")
        profile = _required_text(data, "opencli_profile", account_id)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", profile):
            raise ConfigError(f"Portal account {account_id!r} has invalid 'opencli_profile'.")
        domains = data.get("expected_domains")
        if not isinstance(domains, list) or not domains:
            raise ConfigError(f"Portal account {account_id!r} requires non-empty 'expected_domains'.")
        normalized_domains: list[str] = []
        for domain in domains:
            if not isinstance(domain, str) or not re.fullmatch(
                r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", domain
            ):
                raise ConfigError(f"Portal account {account_id!r} contains an invalid expected domain.")
            normalized_domains.append(domain.lower())
        accounts[account_id] = PortalAccountConfig(
            account_id=account_id,
            label=_required_text(data, "label", account_id),
            adapter=adapter,
            opencli_path=_absolute_path(data, "opencli_path", account_id),
            opencli_profile=profile,
            credential_pointer=_required_text(data, "credential_pointer", account_id),
            expected_domains=tuple(normalized_domains),
        )
    return accounts


def load_config(path: Path) -> OpsConfig:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Configuration file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Configuration is not valid JSON: {exc.msg}") from exc

    if not isinstance(raw, dict) or raw.get("schema_version") not in {1, 2}:
        raise ConfigError("Configuration requires schema_version 1 or 2.")
    schema_version = int(raw["schema_version"])
    portal_accounts = _load_portal_accounts(raw, schema_version)
    raw_sites = raw.get("sites")
    if not isinstance(raw_sites, dict):
        raise ConfigError("Configuration field 'sites' must be an object.")

    sites: dict[str, SiteConfig] = {}
    for site_id, data in raw_sites.items():
        if not isinstance(site_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", site_id):
            raise ConfigError(f"Invalid site id: {site_id!r}.")
        if not isinstance(data, dict):
            raise ConfigError(f"Site {site_id!r} must be an object.")
        for key in data:
            if SECRET_KEY.search(key):
                raise ConfigError(
                    f"Site {site_id!r} contains secret-like field {key!r}; store a pointer instead."
                )
            if key not in ALLOWED_SITE_FIELDS:
                raise ConfigError(f"Site {site_id!r} contains unsupported field {key!r}.")

        adapter = _required_text(data, "adapter", site_id)
        if adapter not in {"paramiko_wpcli", "novamira_mcp", "portal_only"}:
            raise ConfigError(f"Site {site_id!r} uses unsupported adapter {adapter!r}.")
        environment = _required_text(data, "environment", site_id)
        if environment not in {"production", "staging", "development"}:
            raise ConfigError(f"Site {site_id!r} has unsupported environment {environment!r}.")

        novamira_server = _novamira_server(data, site_id)
        if adapter == "novamira_mcp" and not novamira_server:
            raise ConfigError(f"Site {site_id!r} adapter 'novamira_mcp' requires 'novamira_server'.")
        if adapter == "portal_only" and (
            schema_version != 2 or not data.get("portal_account") or not data.get("portal_site_id")
        ):
            raise ConfigError(
                f"Site {site_id!r} adapter 'portal_only' requires schema_version 2, "
                "'portal_account', and 'portal_site_id'."
            )
        sites[site_id] = SiteConfig(
            site_id=site_id,
            label=_required_text(data, "label", site_id),
            environment=environment,
            adapter=adapter,
            credential_pointer=_required_text(data, "credential_pointer", site_id),
            recovery_pointer=_required_text(data, "recovery_pointer", site_id),
            public_url=_public_url(data, site_id),
            env_file=(
                _absolute_path(data, "env_file", site_id)
                if adapter == "paramiko_wpcli"
                else _optional_absolute_path(data, "env_file", site_id)
            ),
            key_file=(
                _absolute_path(data, "key_file", site_id)
                if adapter == "paramiko_wpcli"
                else _optional_absolute_path(data, "key_file", site_id)
            ),
            remote_path=(
                _required_text(data, "remote_path", site_id)
                if adapter == "paramiko_wpcli"
                else data.get("remote_path")
            ),
            novamira_server=novamira_server,
            portal_account=data.get("portal_account"),
            portal_site_id=_optional_portal_id(data, "portal_site_id", site_id),
            portal_plan_id=_optional_portal_id(data, "portal_plan_id", site_id),
        )
    for site in sites.values():
        if site.portal_account is not None:
            if not isinstance(site.portal_account, str) or site.portal_account not in portal_accounts:
                raise ConfigError(
                    f"Site {site.site_id!r} references unknown portal account {site.portal_account!r}."
                )
    return OpsConfig(schema_version=schema_version, sites=sites, portal_accounts=portal_accounts)
