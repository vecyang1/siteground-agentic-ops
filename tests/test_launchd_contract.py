from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_launchagent_uses_bash_for_bash_source_script() -> None:
    with (ROOT / "com.vec.siteground-novamira-cli-updater.plist").open("rb") as handle:
        payload = plistlib.load(handle)
    assert payload["ProgramArguments"][0] == "/bin/bash"
    assert payload["ProgramArguments"][1] == "__LANE_DIR__/update-novamira-cli-stable.sh"
    assert "__LANE_DIR__" in payload["StandardOutPath"]
    assert "__LANE_DIR__" in payload["StandardErrorPath"]
    assert "/opt/homebrew/bin" in payload["EnvironmentVariables"]["PATH"]


def test_updater_scripts_are_syntax_valid() -> None:
    scripts = [
        ROOT / "update-novamira-cli-stable.sh",
        ROOT / "check-status.sh",
        ROOT / "install.sh",
        ROOT / "uninstall.sh",
    ]
    result = subprocess.run(["/bin/bash", "-n", *map(str, scripts)], check=False)
    assert result.returncode == 0


def test_unattended_lane_is_pinned_to_reviewed_release() -> None:
    script = (ROOT / "update-novamira-cli-stable.sh").read_text(encoding="utf-8")
    assert 'evidence.get("latest") == "1.1.0"' in script
    assert "--confirm-version 1.1.0" in script
    assert "no approved update eligible; no mutation attempted" in script


def test_install_script_materializes_current_checkout_paths() -> None:
    script = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "plistlib" in script
    assert "LANE_DIR" in script
    assert "__LANE_DIR__" not in script.split("<<'PY'", 1)[0]


def test_status_script_emits_one_combined_json_receipt() -> None:
    script = (ROOT / "check-status.sh").read_text(encoding="utf-8")
    assert '"status": stored' in script
    assert '"live_check": live' in script


def test_lane_treats_an_unreachable_registry_as_no_verdict() -> None:
    """An alarm that fires on the network stops being read before it fires on the package."""
    script = (ROOT / "update-novamira-cli-stable.sh").read_text(encoding="utf-8")
    assert '"$check_rc" -eq 75' in script
    assert "no verdict: registry unreachable" in script
    assert "no_verdict_streak" in script
    # The no-verdict branch must exit before the mutation gate, never through it.
    assert script.index('"$check_rc" -eq 75') < script.index('"$check_rc" -ne 0')
    assert script.index('"$check_rc" -eq 75') < script.index("--confirm-version 1.1.0")


def test_lane_exit_code_for_no_verdict_matches_the_cli() -> None:
    """The shell's 75 and the CLI's EXIT_NO_VERDICT are one contract in two files."""
    import sys

    sys.path.insert(0, str(ROOT / "src"))
    from siteground_ops.cli import EXIT_NO_VERDICT

    script = (ROOT / "update-novamira-cli-stable.sh").read_text(encoding="utf-8")
    assert f'"$check_rc" -eq {EXIT_NO_VERDICT}' in script
