from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "opencli" / "siteground"

READ_COMMANDS = {
    "billing-methods",
    "hosting",
    "payment-history",
    "plan-sites",
    "renewals",
    "statistics",
    "websites",
    "wp-apps",
}
# The only command in this plugin that is allowed to change provider state. It
# mints a single-use WordPress login, so it is enumerated here rather than
# inferred, and every other command must still prove it is read-only.
WRITE_COMMANDS = {"wp-login"}


def _command_sources() -> dict[str, str]:
    return {
        path.stem: path.read_text(encoding="utf-8")
        for path in ADAPTER.glob("*.js")
        if not path.name.startswith("_") and not path.name.endswith(".test.js")
    }


def test_portal_adapter_exposes_only_the_fixed_command_set() -> None:
    assert set(_command_sources()) == READ_COMMANDS | WRITE_COMMANDS


def test_portal_read_commands_have_no_mutation_or_credential_primitives() -> None:
    forbidden = ("access: 'write'", ".click(", ".fill(", ".type(", ".upload(", "fetch(")
    sources = _command_sources()
    for name in sorted(READ_COMMANDS):
        source = sources[name]
        assert "access: 'read'" in source, name
        assert "browser: true" in source, name
        assert all(token not in source for token in forbidden), name


def test_the_single_write_command_declares_itself_and_stays_browser_bound() -> None:
    sources = _command_sources()
    for name in sorted(WRITE_COMMANDS):
        source = sources[name]
        assert "access: 'write'" in source, name
        assert "browser: true" in source, name
        assert ".fill(" not in source and ".upload(" not in source, name


def test_portal_navigation_is_allowlisted_in_one_owner() -> None:
    source = (ADAPTER / "_ui.js").read_text(encoding="utf-8")
    for route in (
        "/websites/list",
        "/services/hosting",
        "/billing/details",
        "/billing/payment-history",
        "/billing/renew",
    ):
        assert route in source
    assert "new URL" not in source


def test_wordpress_routes_and_credential_handling_live_in_one_owner() -> None:
    owner = (ADAPTER / "_wp.js").read_text(encoding="utf-8")
    for route in ("/v1/sites/list", "/v1/auth/wordpress/autologin"):
        assert route in owner
    # The portal access token and the minted login must never leave the page.
    assert "ua_session" in owner
    assert "assertAutologinTarget" in owner
    for name in ("wp-apps", "wp-login"):
        source = (ADAPTER / f"{name}.js").read_text(encoding="utf-8")
        assert "uapi.siteground.com" not in source
        assert "ua_session" not in source


def test_no_command_returns_the_single_use_autologin_url() -> None:
    for path in ADAPTER.glob("*.js"):
        if path.name.endswith(".test.js"):
            continue
        source = path.read_text(encoding="utf-8")
        # `autologin_url` may be read inside the page and validated, but it must
        # never appear in a declared output column.
        if "columns:" in source:
            columns_line = next(line for line in source.splitlines() if "columns:" in line)
            assert "autologin" not in columns_line, path.name


def test_local_plugin_resolves_opencli_through_the_user_runtime() -> None:
    runtime = (ADAPTER / "_runtime.js").read_text(encoding="utf-8")
    assert "createRequire" in runtime
    assert ".opencli" in runtime
    assert "@jackwener/opencli/registry" in runtime
    assert "@jackwener/opencli/errors" in runtime

    for path in ADAPTER.glob("*.js"):
        if path.name.startswith("_") or path.name.endswith(".test.js"):
            continue
        source = path.read_text(encoding="utf-8")
        assert "from './_runtime.js'" in source
        assert "from '@jackwener/opencli/" not in source
