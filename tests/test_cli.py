from __future__ import annotations

import json
from pathlib import Path

import pytest

from siteground_ops.cli import main
from siteground_ops.config import ConfigError, load_config
from siteground_ops.receipts import redact, sanitize


def write_config(tmp_path: Path, site: dict | None = None) -> Path:
    config = {
        "schema_version": 1,
        "sites": {
            "prod": site
            or {
                "label": "Production",
                "environment": "production",
                "adapter": "paramiko_wpcli",
                "env_file": str(tmp_path / ".env"),
                "key_file": str(tmp_path / "id_ed25519"),
                "remote_path": "~/www/example.com/public_html",
                "credential_pointer": "Project .env",
                "recovery_pointer": "Provider backup receipt",
                "public_url": "https://example.com",
                "novamira_server": "novamira-example",
            }
        },
    }
    path = tmp_path / "sites.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def read_receipt(capsys: pytest.CaptureFixture[str]) -> dict:
    output = capsys.readouterr().out
    return json.loads(output)


def make_ssh_locally_ready(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "SSH_HOST=test\nSSH_USER=user\nSSH_PORT=18765\nSSH_CONFIRMED_PASS=passphrase\n",
        encoding="utf-8",
    )
    (tmp_path / "id_ed25519").write_text("test-key", encoding="utf-8")


def test_sites_lists_non_secret_readiness(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(tmp_path)
    monkeypatch.setattr(
        "siteground_ops.cli.read_transport_status",
        lambda _site: {"available": ["novamira"], "missing": {"ssh": ["env_file", "key_file"]}},
    )

    exit_code = main(["--config", str(path), "sites"])

    receipt = read_receipt(capsys)
    assert exit_code == 0
    assert receipt["ok"] is True
    assert receipt["mutation_state"] == "not_applicable"
    assert receipt["evidence"]["sites"] == [
        {
            "adapter": "paramiko_wpcli",
            "environment": "production",
            "id": "prod",
            "label": "Production",
            "public_url": "https://example.com",
            "ready": False,
            "missing": ["env_file", "key_file"],
            "portal_mapped": False,
            "read_ready": True,
            "read_transports": ["novamira"],
        }
    ]
    assert "SSH_CONFIRMED_PASS" not in json.dumps(receipt)


def test_config_rejects_inline_secret_fields(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["sites"]["prod"]["ssh_password"] = "do-not-store-this"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ConfigError, match="secret-like field"):
        load_config(path)


def test_config_requires_https_public_url(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["sites"]["prod"]["public_url"] = "http://example.com"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ConfigError, match="public_url"):
        load_config(path)


def test_config_supports_novamira_only_read_profile(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(
        tmp_path,
        {
            "label": "Novamira-only production",
            "environment": "production",
            "adapter": "novamira_mcp",
            "credential_pointer": "novamira-ops exact MCP server",
            "recovery_pointer": "Provider backup owner before any future mutation",
            "public_url": "https://example.com",
            "novamira_server": "novamira-example",
        },
    )
    monkeypatch.setattr(
        "siteground_ops.cli.read_transport_status",
        lambda _site: {"available": ["novamira"], "missing": {}},
    )

    exit_code = main(["--config", str(path), "sites"])

    receipt = read_receipt(capsys)
    assert exit_code == 0
    assert receipt["evidence"]["sites"] == [
        {
            "adapter": "novamira_mcp",
            "environment": "production",
            "id": "prod",
            "label": "Novamira-only production",
            "public_url": "https://example.com",
            "ready": True,
            "missing": [],
            "portal_mapped": False,
            "read_ready": True,
            "read_transports": ["novamira"],
        }
    ]


def test_doctor_requires_explicit_known_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_config(tmp_path)
    (tmp_path / ".env").write_text("SSH_HOST=test\n", encoding="utf-8")
    (tmp_path / "id_ed25519").write_text("test", encoding="utf-8")

    exit_code = main(["--config", str(path), "doctor", "missing"])

    receipt = read_receipt(capsys)
    assert exit_code == 2
    assert receipt["ok"] is False
    assert receipt["mutation_state"] == "refused"
    assert receipt["target"] == "missing"
    assert receipt["safe_next_action"] == "Choose an exact site from `siteground-ops sites`."


def test_doctor_normalizes_read_only_wpcli_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(tmp_path)
    (tmp_path / ".env").write_text("SSH_HOST=test\n", encoding="utf-8")
    (tmp_path / "id_ed25519").write_text("test", encoding="utf-8")

    class FakeRunner:
        def doctor(self) -> dict:
            return {
                "wp_cli_version": "WP-CLI 2.12.0",
                "wordpress_version": "6.9.1",
                "home_url": "https://example.com",
                "siteground_cache_cli": True,
            }

    monkeypatch.setattr("siteground_ops.cli.build_read_runner", lambda _site, _transport: ("ssh", FakeRunner()))

    exit_code = main(["--config", str(path), "doctor", "prod"])

    receipt = read_receipt(capsys)
    assert exit_code == 0
    assert receipt["ok"] is True
    assert receipt["operation"] == "doctor"
    assert receipt["target"] == "prod"
    assert receipt["mutation_state"] == "not_applicable"
    assert receipt["evidence"]["wp_cli_version"] == "WP-CLI 2.12.0"
    assert receipt["evidence"]["home_url"] == "https://example.com"
    assert receipt["evidence"]["transport"] == "ssh"


def test_inventory_reports_plugins_themes_and_updates_read_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(tmp_path)
    (tmp_path / ".env").write_text("SSH_HOST=test\n", encoding="utf-8")
    (tmp_path / "id_ed25519").write_text("test", encoding="utf-8")

    class FakeRunner:
        def inventory(self) -> dict:
            return {
                "plugins": [{"name": "siteground-optimizer", "status": "active", "version": "7.7.1"}],
                "themes": [{"name": "blocksy", "status": "active", "version": "2.1.0"}],
                "updates": {"core": None, "plugins": [], "themes": []},
            }

    monkeypatch.setattr("siteground_ops.cli.build_read_runner", lambda _site, _transport: ("ssh", FakeRunner()))

    exit_code = main(["--config", str(path), "inventory", "prod"])

    receipt = read_receipt(capsys)
    assert exit_code == 0
    assert receipt["operation"] == "inventory"
    assert receipt["mutation_state"] == "not_applicable"
    assert receipt["evidence"]["plugins"][0]["name"] == "siteground-optimizer"
    assert receipt["evidence"]["updates"]["plugins"] == []
    assert receipt["evidence"]["transport"] == "ssh"


def test_doctor_uses_novamira_when_ssh_profile_is_unready(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_config(tmp_path)
    (tmp_path / "id_ed25519").unlink(missing_ok=True)

    class FakeRunner:
        def doctor(self) -> dict:
            return {
                "home_url": "https://example.com",
                "siteground_cache_cli": False,
                "siteground_optimizer_active": True,
                "wordpress_version": "7.0.3",
                "wp_cli_version": None,
            }

    from unittest.mock import patch

    with patch(
        "siteground_ops.cli.build_read_runner",
        return_value=("novamira", FakeRunner()),
    ):
        exit_code = main(["--config", str(path), "doctor", "prod"])

    receipt = read_receipt(capsys)
    assert exit_code == 0
    assert receipt["evidence"]["transport"] == "novamira"


def test_doctor_allows_explicit_read_transport(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(tmp_path)
    selected: list[str] = []

    class FakeRunner:
        def doctor(self) -> dict:
            return {"home_url": "https://example.com"}

    def build(_site: object, transport: str):
        selected.append(transport)
        return "novamira", FakeRunner()

    monkeypatch.setattr("siteground_ops.cli.build_read_runner", build)

    exit_code = main(["--config", str(path), "doctor", "prod", "--transport", "novamira"])

    receipt = read_receipt(capsys)
    assert exit_code == 0
    assert selected == ["novamira"]
    assert receipt["evidence"]["transport"] == "novamira"


def test_explicit_unavailable_ssh_is_a_structured_selection_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_config(tmp_path)
    (tmp_path / ".env").write_text("SSH_HOST=test\n", encoding="utf-8")
    (tmp_path / "id_ed25519").write_text("test-key", encoding="utf-8")

    exit_code = main(["--config", str(path), "doctor", "prod", "--transport", "ssh"])

    receipt = read_receipt(capsys)
    assert exit_code == 2
    assert receipt["mutation_state"] == "refused"
    assert receipt["diagnostics"]["code"] == "read_transport_unavailable"


def test_cache_purge_refuses_without_target_confirmation_and_recovery(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(tmp_path)
    make_ssh_locally_ready(tmp_path)
    called = False

    class FakeRunner:
        def purge_cache(self, request_id: str) -> dict:
            nonlocal called
            called = True
            return {"request_id": request_id}

    monkeypatch.setattr("siteground_ops.cli.build_runner", lambda _site: FakeRunner())

    exit_code = main(["--config", str(path), "cache-purge", "prod"])

    receipt = read_receipt(capsys)
    assert exit_code == 2
    assert called is False
    assert receipt["mutation_state"] == "refused"
    assert "--confirm-target prod" in receipt["safe_next_action"]
    assert "--recovery-receipt" in receipt["safe_next_action"]


def test_cache_purge_refuses_when_ssh_owner_lacks_required_names(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(tmp_path)
    (tmp_path / ".env").write_text("SSH_HOST=test\n", encoding="utf-8")
    (tmp_path / "id_ed25519").write_text("test-key", encoding="utf-8")
    monkeypatch.setattr(
        "siteground_ops.cli.build_runner",
        lambda _site: (_ for _ in ()).throw(AssertionError("mutation runner must not be built")),
    )

    exit_code = main(
        [
            "--config",
            str(path),
            "cache-purge",
            "prod",
            "--confirm-target",
            "prod",
            "--recovery-receipt",
            "backup-1",
        ]
    )

    receipt = read_receipt(capsys)
    assert exit_code == 2
    assert receipt["diagnostics"]["code"] == "ssh_mutation_transport_required"
    assert receipt["mutation_state"] == "refused"


def test_cache_purge_refuses_novamira_only_profile_before_runner_build(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(
        tmp_path,
        {
            "label": "Novamira-only production",
            "environment": "production",
            "adapter": "novamira_mcp",
            "credential_pointer": "novamira-ops exact MCP server",
            "recovery_pointer": "Provider backup owner",
            "public_url": "https://example.com",
            "novamira_server": "novamira-example",
        },
    )
    monkeypatch.setattr(
        "siteground_ops.cli.build_runner",
        lambda _site: (_ for _ in ()).throw(AssertionError("mutation runner must not be built")),
    )

    exit_code = main(
        [
            "--config",
            str(path),
            "cache-purge",
            "prod",
            "--confirm-target",
            "prod",
            "--recovery-receipt",
            "backup-1",
        ]
    )

    receipt = read_receipt(capsys)
    assert exit_code == 2
    assert receipt["mutation_state"] == "refused"
    assert receipt["diagnostics"]["code"] == "ssh_mutation_transport_required"


def test_cache_purge_applies_only_after_readback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(tmp_path)
    make_ssh_locally_ready(tmp_path)
    calls: list[str] = []

    class FakeRunner:
        def purge_cache(self, request_id: str) -> dict:
            calls.append(request_id)
            return {
                "command": "wp sg purge",
                "readback": {"home_url": "https://example.com", "http_status": 200},
            }

    monkeypatch.setattr("siteground_ops.cli.build_runner", lambda _site: FakeRunner())

    exit_code = main(
        [
            "--config",
            str(path),
            "cache-purge",
            "prod",
            "--confirm-target",
            "prod",
            "--recovery-receipt",
            "provider-backup:2026-08-09",
        ]
    )

    receipt = read_receipt(capsys)
    assert exit_code == 0
    assert len(calls) == 1
    assert receipt["mutation_state"] == "applied"
    assert receipt["evidence"]["readback"]["http_status"] == 200
    assert receipt["evidence"]["recovery_receipt"] == "provider-backup:2026-08-09"


def test_ambiguous_cache_purge_is_unknown_not_retried(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(tmp_path)
    make_ssh_locally_ready(tmp_path)
    calls = 0

    class FakeRunner:
        def purge_cache(self, request_id: str) -> dict:
            nonlocal calls
            calls += 1
            raise TimeoutError("connection dropped after send")

    monkeypatch.setattr("siteground_ops.cli.build_runner", lambda _site: FakeRunner())

    exit_code = main(
        [
            "--config",
            str(path),
            "cache-purge",
            "prod",
            "--confirm-target",
            "prod",
            "--recovery-receipt",
            "backup-1",
        ]
    )

    receipt = read_receipt(capsys)
    assert exit_code == 3
    assert calls == 1
    assert receipt["mutation_state"] == "unknown"
    assert "read back" in receipt["safe_next_action"].lower()


def test_redaction_covers_credentials_and_tokens() -> None:
    raw = (
        "SSH_CONFIRMED_PASS=correct-horse "
        "Authorization: Basic abc123 "
        "https://user:secret@example.com/path "
        "token=sk_live_1234567890"
    )
    cleaned = redact(raw)
    assert "correct-horse" not in cleaned
    assert "abc123" not in cleaned
    assert "user:secret" not in cleaned
    assert "sk_live" not in cleaned
    assert cleaned.count("[REDACTED]") >= 4


def test_redaction_covers_password_assignments_and_sensitive_dict_keys() -> None:
    cleaned = redact("SSH_PASSWORD=supersecret password=also-secret passphrase=phrase")
    assert "supersecret" not in cleaned
    assert "also-secret" not in cleaned
    assert "=phrase" not in cleaned
    cleaned_dict = sanitize({"token": "token-secret", "password": "password-secret", "safe": "ok"})
    assert cleaned_dict == {"token": "[REDACTED]", "password": "[REDACTED]", "safe": "ok"}
    assert sanitize({"SSH_CONFIRMED_PASS": "correct-horse"}) == {"SSH_CONFIRMED_PASS": "[REDACTED]"}


def test_novamira_update_check_is_a_read_only_structured_receipt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeUpdater:
        def check(self) -> dict:
            return {
                "ok": True,
                "mutation_state": "not_applicable",
                "current": "1.0.0",
                "latest": "1.0.3",
                "auto_apply_ready": False,
                "blockers": ["local_novamira_ops_guidance_incompatible"],
            }

    monkeypatch.setattr("siteground_ops.cli.build_novamira_updater", lambda: FakeUpdater())

    exit_code = main(["--config", str(tmp_path / "missing.json"), "novamira-update", "check"])

    receipt = read_receipt(capsys)
    assert exit_code == 0
    assert receipt["operation"] == "novamira-update"
    assert receipt["mutation_state"] == "not_applicable"
    assert receipt["evidence"]["auto_apply_ready"] is False


def test_novamira_apply_requires_exact_confirmed_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeUpdater:
        def apply(self, *, confirmed: bool) -> dict:
            raise AssertionError("must refuse before updater mutation lane")

    monkeypatch.setattr("siteground_ops.cli.build_novamira_updater", lambda: FakeUpdater())

    exit_code = main(
        [
            "--config",
            str(tmp_path / "missing.json"),
            "novamira-update",
            "apply",
            "--confirm-version",
            "1.0.2",
        ]
    )

    receipt = read_receipt(capsys)
    assert exit_code == 2
    assert receipt["mutation_state"] == "refused"
    assert receipt["diagnostics"]["code"] == "version_confirmation_required"


def test_novamira_check_failure_is_not_reported_as_unknown_mutation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailingUpdater:
        def check(self) -> dict:
            raise RuntimeError("registry unavailable")

    monkeypatch.setattr("siteground_ops.cli.build_novamira_updater", lambda: FailingUpdater())
    exit_code = main(["--config", str(tmp_path / "missing.json"), "novamira-update", "check"])
    receipt = read_receipt(capsys)
    assert exit_code == 1
    assert receipt["mutation_state"] == "not_applicable"
    assert receipt["diagnostics"]["code"] == "novamira_check_failed"


def test_novamira_apply_preflight_failure_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailingUpdater:
        def apply(self, *, confirmed: bool) -> dict:
            raise RuntimeError("candidate unavailable")

    monkeypatch.setattr("siteground_ops.cli.build_novamira_updater", lambda: FailingUpdater())
    exit_code = main(
        ["--config", str(tmp_path / "missing.json"), "novamira-update", "apply", "--confirm-version", "1.0.3"]
    )
    receipt = read_receipt(capsys)
    assert exit_code == 2
    assert receipt["mutation_state"] == "refused"
    assert receipt["diagnostics"]["code"] == "novamira_preflight_failed"
