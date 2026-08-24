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


def _failing_adapter(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    class Adapter:
        def read(self, section: str, *, provider_plan_id: str | None = None) -> dict:
            raise error

    monkeypatch.setattr("siteground_ops.cli.build_portal_adapter", lambda _account: Adapter())


def test_disconnected_bridge_receipt_points_at_the_bridge_not_the_login(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from siteground_ops.portal import PortalError

    _failing_adapter(monkeypatch, PortalError("not connected", code="portal_browser_not_connected"))
    exit_code = main(["--config", str(write_portal_config(tmp_path)), "portal", "doctor", "primary"])

    payload = receipt(capsys)
    remedy = payload["safe_next_action"]
    assert exit_code == 1
    assert payload["diagnostics"]["code"] == "portal_browser_not_connected"
    assert "profile-alias" in remedy
    # The failure is a closed browser. Telling the operator to sign in again sends
    # them past the line that would have fixed it.
    assert "logged into" not in remedy and "signed into" not in remedy


def test_unclassified_portal_failure_does_not_assert_a_cause(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _failing_adapter(monkeypatch, RuntimeError("something unmapped"))
    exit_code = main(["--config", str(write_portal_config(tmp_path)), "portal", "doctor", "primary"])

    payload = receipt(capsys)
    assert exit_code == 1
    assert payload["diagnostics"]["code"] == "portal_read_failed"
    assert "logged into" not in payload["safe_next_action"]


def test_every_portal_remedy_renders_for_a_real_account(capsys: pytest.CaptureFixture[str]) -> None:
    """A remedy is read at the worst moment; a KeyError there is a second failure."""
    from siteground_ops.cli import PORTAL_FAILURE_REMEDIES, _portal_failure
    from siteground_ops.portal import PortalError

    from test_portal_adapter import account

    graded = 0
    for code in PORTAL_FAILURE_REMEDIES:
        resolved, remedy = _portal_failure(PortalError("x", code=code), account())
        assert resolved == code
        assert "{" not in remedy and "}" not in remedy
        graded += 1
    print(f"graded {graded} portal remedies")
    assert graded == len(PORTAL_FAILURE_REMEDIES) >= 5
