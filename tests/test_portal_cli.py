from __future__ import annotations

import json
from pathlib import Path

import pytest

from siteground_ops.cli import main

from test_portal_config import write_portal_config


def receipt(capsys: pytest.CaptureFixture[str]) -> dict:
    return json.loads(capsys.readouterr().out)


def test_portal_accounts_lists_non_secret_readiness(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["--config", str(write_portal_config(tmp_path)), "portal", "accounts"])

    payload = receipt(capsys)
    assert exit_code == 0
    assert payload["operation"] == "portal-accounts"
    assert payload["mutation_state"] == "not_applicable"
    assert payload["evidence"]["accounts"][0]["id"] == "primary"
    assert "token" not in json.dumps(payload).lower()


def test_portal_read_emits_account_scoped_receipt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    class Adapter:
        def read(self, section: str, *, provider_plan_id: str | None = None) -> dict:
            assert section == "statistics"
            assert provider_plan_id == "EXAMPLEPLANID005"
            return {"transport": "opencli", "section": section, "rows": [{"metric": "web_space_used"}]}

    monkeypatch.setattr("siteground_ops.cli.build_portal_adapter", lambda _account: Adapter())
    exit_code = main(
        [
            "--config",
            str(write_portal_config(tmp_path)),
            "portal",
            "read",
            "primary",
            "statistics",
            "--plan-id",
            "EXAMPLEPLANID005",
        ]
    )

    payload = receipt(capsys)
    assert exit_code == 0
    assert payload["operation"] == "portal-read"
    assert payload["target"] == "primary"
    assert payload["evidence"]["transport"] == "opencli"
    assert payload["mutation_state"] == "not_applicable"


def test_portal_links_do_not_invoke_a_browser(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "siteground_ops.cli.build_portal_adapter",
        lambda _account: (_ for _ in ()).throw(AssertionError("browser must not be invoked")),
    )
    exit_code = main(["--config", str(write_portal_config(tmp_path)), "portal", "links", "prod"])

    payload = receipt(capsys)
    assert exit_code == 0
    assert payload["operation"] == "portal-links"
    assert payload["evidence"]["links"]["dashboard"].startswith("https://tools.siteground.com/")


def test_portal_read_failure_is_isolated_and_structured(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    class Adapter:
        def read(self, _section: str, *, provider_plan_id: str | None = None) -> dict:
            raise RuntimeError("bridge unavailable")

    monkeypatch.setattr("siteground_ops.cli.build_portal_adapter", lambda _account: Adapter())
    exit_code = main(
        ["--config", str(write_portal_config(tmp_path)), "portal", "read", "primary", "websites"]
    )

    payload = receipt(capsys)
    assert exit_code == 1
    assert payload["diagnostics"]["code"] == "portal_read_failed"
    assert payload["mutation_state"] == "not_applicable"
