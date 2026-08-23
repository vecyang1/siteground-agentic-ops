from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from siteground_ops import portal
from siteground_ops.config import PortalAccountConfig, SiteConfig
from siteground_ops.portal import (
    PortalError,
    PortalOpenCliAdapter,
    PortalUnknownOutcomeError,
    site_tools_links,
)


def account() -> PortalAccountConfig:
    return PortalAccountConfig(
        account_id="primary",
        label="Primary",
        adapter="opencli",
        opencli_path=Path("/opt/local/bin/opencli"),
        opencli_profile="profile-alias",
        credential_pointer="OpenCLI Browser Bridge profile",
        expected_domains=("example.com",),
    )


def site() -> SiteConfig:
    return SiteConfig(
        site_id="prod",
        label="Production",
        environment="production",
        adapter="novamira_mcp",
        credential_pointer="novamira-ops exact server",
        recovery_pointer="SiteGround backup owner",
        public_url="https://example.com",
        novamira_server="novamira-example",
        portal_account="primary",
        portal_site_id="EXAMPLESITEID003",
        portal_plan_id="EXAMPLEPLANID005",
    )


def completed(command: list[str], payload: object, *, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout=json.dumps(payload), stderr="")


def test_portal_adapter_uses_only_fixed_opencli_read_commands() -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[3:5] == ["siteground", "websites"]:
            return completed(command, [{"domain": "example.com", "status": "Active"}])
        return completed(command, [{"metric": "web_space_used", "value": 13.64, "unit": "GB"}])

    adapter = PortalOpenCliAdapter(account(), run_command=run)
    evidence = adapter.read("statistics", provider_plan_id="EXAMPLEPLANID005")

    assert evidence["transport"] == "opencli"
    assert evidence["section"] == "statistics"
    assert evidence["rows"][0]["metric"] == "web_space_used"
    assert calls == [
        [
            "/opt/local/bin/opencli",
            "--profile",
            "profile-alias",
            "siteground",
            "websites",
            "--site-session=ephemeral",
            "--window=background",
            "--format=json",
        ],
        [
            "/opt/local/bin/opencli",
            "--profile",
            "profile-alias",
            "siteground",
            "statistics",
            "--plan-id",
            "EXAMPLEPLANID005",
            "--site-session=ephemeral",
            "--window=background",
            "--format=json",
        ],
    ]


def test_portal_adapter_refuses_unknown_commands_before_subprocess() -> None:
    called = False

    def run(_command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        raise AssertionError("must not run")

    with pytest.raises(PortalError, match="Unsupported portal read"):
        PortalOpenCliAdapter(account(), run_command=run).read("dns-delete")
    assert called is False


def test_portal_adapter_rejects_account_identity_mismatch() -> None:
    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return completed(command, [{"domain": "other.example", "status": "Active"}])

    with pytest.raises(PortalError, match="account identity"):
        PortalOpenCliAdapter(account(), run_command=run).read("websites")


def test_site_tools_links_are_exact_allowlisted_https_routes() -> None:
    links = site_tools_links(site())

    assert links == {
        "dashboard": "https://tools.siteground.com/dashboard?siteId=EXAMPLESITEID003",
        "file_manager": "https://tools.siteground.com/filemanager?siteId=EXAMPLESITEID003",
        "backups": "https://tools.siteground.com/backup-restore-manage?siteId=EXAMPLESITEID003",
        "cache": "https://tools.siteground.com/cacher?siteId=EXAMPLESITEID003",
        "wordpress_management": "https://tools.siteground.com/wp-manage?siteId=EXAMPLESITEID003",
        "wordpress_admin": "https://example.com/wp-admin/",
    }


def test_site_tools_links_require_an_explicit_provider_site_id() -> None:
    mapped = site()
    object.__setattr__(mapped, "portal_site_id", None)
    with pytest.raises(PortalError, match="portal_site_id"):
        site_tools_links(mapped)


APP_ROW = {
    "domain": "example.com",
    "admin_host": "example.com",
    "site_id": "EXAMPLESITEID003",
    "app_id": "1",
    "cms": "wordpress",
    "admin_url": "https://example.com/wp-admin",
    "status": "active",
}
LOGIN_ROW = {
    "domain": "example.com",
    "admin_host": "example.com",
    "site_id": "EXAMPLESITEID003",
    "app_id": "1",
    "wp_admin_url": "https://example.com/wp-admin/",
    "page_title": "Dashboard ‹ Example — WordPress",
    "logged_in": True,
}


def test_wordpress_apps_retries_a_dropped_read_exactly_once() -> None:
    attempts: list[str] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        step = command[4]
        attempts.append(step)
        if step == "websites" and attempts.count("websites") == 1:
            return completed(command, {"error": {"message": "Browser Bridge extension not connected"}}, returncode=1)
        if step == "websites":
            return completed(command, [{"domain": "example.com", "status": "Active"}])
        return completed(command, [APP_ROW])

    adapter = PortalOpenCliAdapter(account(), run_command=run)
    evidence = adapter.wordpress_apps()

    assert attempts == ["websites", "websites", "wp-apps"]
    assert evidence["rows"] == [APP_ROW]


def test_wordpress_apps_gives_up_after_the_bounded_retry_and_names_the_cause() -> None:
    attempts: list[str] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        attempts.append(command[4])
        return completed(command, {"error": {"message": "Browser Bridge extension not connected"}}, returncode=1)

    adapter = PortalOpenCliAdapter(account(), run_command=run)
    with pytest.raises(PortalError, match="Browser Bridge extension not connected"):
        adapter.wordpress_apps()

    assert attempts == ["websites", "websites"]


def test_open_wordpress_admin_never_retries_because_a_repeat_mints_a_second_credential() -> None:
    attempts: list[str] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        attempts.append(command[4])
        raise subprocess.TimeoutExpired(command, 300.0)

    adapter = PortalOpenCliAdapter(account(), run_command=run)
    with pytest.raises(PortalUnknownOutcomeError, match="read state back before any retry"):
        adapter.open_wordpress_admin(site_id="EXAMPLESITEID003", app_id=None, expected_domain="example.com")

    assert attempts == ["wp-login"]


def test_open_wordpress_admin_passes_only_allowlisted_flags_and_stays_quiet_by_default() -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return completed(command, [LOGIN_ROW])

    adapter = PortalOpenCliAdapter(account(), run_command=run)
    evidence = adapter.open_wordpress_admin(
        site_id="EXAMPLESITEID003", app_id="1", expected_domain="example.com"
    )

    assert calls[0][4:] == [
        "wp-login", "--app", "1", "--site-id", "EXAMPLESITEID003",
        "--site-session=persistent", "--window=background", "--format=json",
    ]
    assert evidence["window"] == "background"
    assert evidence["wp_admin_url"] == "https://example.com/wp-admin/"
    assert evidence["credential_disclosed"] is False
    assert "autologin" not in json.dumps(evidence)

    adapter.open_wordpress_admin(
        site_id="EXAMPLESITEID003", app_id="1", expected_domain="example.com", foreground=True
    )
    assert "--window=foreground" in calls[1]


def test_open_wordpress_admin_refuses_an_unallowlisted_flag() -> None:
    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("no OpenCLI call may be made for an unsupported option")

    adapter = PortalOpenCliAdapter(account(), run_command=run)
    with pytest.raises(PortalError, match="Unsupported OpenCLI option"):
        adapter._command("wp-login", options={"exec": "rm -rf /"})


@pytest.mark.parametrize("bad", ["../../v1", "short", "EXAMPLESITEID003/9", ""])
def test_open_wordpress_admin_refuses_a_site_id_that_could_reshape_the_request(bad: str) -> None:
    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("no OpenCLI call may be made for an invalid site id")

    adapter = PortalOpenCliAdapter(account(), run_command=run)
    with pytest.raises(PortalError):
        adapter.open_wordpress_admin(site_id=bad, app_id=None, expected_domain="example.com")


def test_open_wordpress_admin_treats_a_mismatched_host_as_unknown_not_success() -> None:
    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return completed(command, [{**LOGIN_ROW, "admin_host": "staging2.example.com"}])

    adapter = PortalOpenCliAdapter(account(), run_command=run)
    with pytest.raises(PortalUnknownOutcomeError, match="different host"):
        adapter.open_wordpress_admin(site_id="EXAMPLESITEID003", app_id=None, expected_domain="example.com")


def timed_out(command: list[str]) -> subprocess.CompletedProcess[str]:
    """OpenCLI's own browser-command timeout: a clean non-zero exit, not a killed process."""
    envelope = {
        "ok": False,
        "error": {
            "code": "TIMEOUT",
            "message": "siteground/wp-login timed out after 60s",
            "hint": "Try again, or increase timeout with --timeout <seconds>",
        },
    }
    return subprocess.CompletedProcess(command, 75, stdout="", stderr=json.dumps(envelope))


def test_opencli_own_timeout_on_a_write_is_unknown_because_the_credential_may_exist() -> None:
    """OpenCLI aborts the browser command itself and exits non-zero.

    The autologin fetch may already have minted a single-use administrator
    credential before the abort, so this is an unknown outcome, not a clean
    failure. Reporting it as failed invites a retry that mints a second one.
    """

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return timed_out(command)

    adapter = PortalOpenCliAdapter(account(), run_command=run)
    with pytest.raises(PortalUnknownOutcomeError, match="timed out"):
        adapter.open_wordpress_admin(site_id="EXAMPLESITEID003", app_id="1", expected_domain="example.com")


def test_opencli_own_timeout_on_a_read_stays_a_plain_failure() -> None:
    """A read mints nothing, so a timeout there is safe to name as a failure."""
    attempts: list[str] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        attempts.append(command[4])
        return timed_out(command)

    adapter = PortalOpenCliAdapter(account(), run_command=run)
    with pytest.raises(PortalError) as caught:
        adapter.wordpress_apps()
    assert not isinstance(caught.value, PortalUnknownOutcomeError)


def test_wordpress_commands_give_the_browser_a_budget_below_the_subprocess_budget() -> None:
    """OpenCLI's default browser cap is 60s, under the ~40s a real login takes.

    The two budgets must not race: the subprocess budget has to leave room for
    OpenCLI's own browser connect allowance on top of the command budget, or the
    wrapper kills OpenCLI before it can name its own timeout.
    """
    seen: list[tuple[str, dict[str, str], float]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        env = kwargs["env"]
        assert isinstance(env, dict)
        seen.append((command[4], env, float(kwargs["timeout"])))  # type: ignore[arg-type]
        return completed(command, [LOGIN_ROW])

    adapter = PortalOpenCliAdapter(account(), run_command=run)
    adapter.open_wordpress_admin(site_id="EXAMPLESITEID003", app_id="1", expected_domain="example.com")

    name, env, subprocess_budget = seen[-1]
    assert name == "wp-login"
    browser_budget = float(env["OPENCLI_BROWSER_COMMAND_TIMEOUT"])
    assert browser_budget > 60.0, "must exceed the OpenCLI default that already timed out"
    assert subprocess_budget >= browser_budget + portal.OPENCLI_BROWSER_CONNECT_ALLOWANCE_SECONDS


def test_login_browser_budget_clears_the_hardcoded_per_action_ceiling() -> None:
    """A single browser action is capped at 120s inside OpenCLI, with no override.

    The command budget must clear that ceiling so a wedged action reports its own
    timeout instead of being cut short by a budget underneath it -- that report is
    what tells the operator the outcome is unknown rather than failed.
    """
    assert portal.WORDPRESS_LOGIN_BROWSER_SECONDS > portal.OPENCLI_BROWSER_ACTION_CEILING_SECONDS


def _budget_probe(rows: list[dict[str, object]]):
    """Capture the (browser budget, subprocess budget) pair of every OpenCLI call."""
    seen: list[tuple[str, str | None, float]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        env = kwargs["env"]
        assert isinstance(env, dict)
        seen.append((command[4], env.get("OPENCLI_BROWSER_COMMAND_TIMEOUT"), float(kwargs["timeout"])))  # type: ignore[arg-type]
        return completed(command, rows)

    return run, seen


def test_every_portal_command_gives_the_browser_a_budget_the_wrapper_cannot_cut_short() -> None:
    """The budget invariant has to hold for every command, not just the one that broke.

    OpenCLI's browser cap defaults to 60s while this wrapper's own default was
    45s, so for every ordinary read the wrapper killed OpenCLI first and an
    attributable timeout arrived as "did not complete". The subprocess budget
    must always be the outer one, and the browser budget must always be stated
    rather than inherited from a default that has nothing to do with this work.
    """
    identity = [{"domain": "example.com", "plan": "Hosting Plan 6"}]
    run, seen = _budget_probe(identity)
    adapter = PortalOpenCliAdapter(account(), run_command=run)

    for section, (_command, requires_plan) in sorted(portal.PORTAL_READS.items()):
        adapter.read(section, provider_plan_id="EXAMPLEPLANID005" if requires_plan else None)

    run_login, seen_login = _budget_probe([LOGIN_ROW])
    PortalOpenCliAdapter(account(), run_command=run_login).open_wordpress_admin(
        site_id="EXAMPLESITEID003", app_id="1", expected_domain="example.com"
    )

    observed = seen + seen_login
    assert {name for name, _browser, _sub in observed} >= portal.PORTAL_PLUGIN_COMMANDS, (
        "the probe must cover every registered command, or it grades a subset"
    )
    for name, browser, subprocess_budget in observed:
        assert browser is not None, f"{name} left the browser on OpenCLI's own default"
        assert subprocess_budget >= float(browser) + portal.OPENCLI_BROWSER_CONNECT_ALLOWANCE_SECONDS, (
            f"{name} lets the wrapper kill OpenCLI before it can name its own timeout"
        )
    print(f"graded {len(observed)} OpenCLI invocations across {len(portal.PORTAL_PLUGIN_COMMANDS)} commands")
