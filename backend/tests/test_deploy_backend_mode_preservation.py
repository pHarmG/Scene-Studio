"""Static contract tests for the Scene Studio backend deployer.

The live deployer is PowerShell and cannot be executed here without HA writes.
These tests pin the package-only mode-preservation invariant against the
script text so a later edit cannot silently restore a hardcoded read_only
health check or drop the pre-activation mode capture.
"""

from pathlib import Path

DEPLOYER = (
    Path(__file__).resolve().parents[2]
    / "installer"
    / "deploy_scene_studio_backend.ps1"
)
KNOWN_MODES = (
    "normal",
    "read_only",
    "registry_admin",
    "r2_validation",
    "r5_validation",
)


def _text() -> str:
    return DEPLOYER.read_text(encoding="utf-8")


def test_deployer_script_is_present():
    assert DEPLOYER.is_file()


def test_deployer_does_not_hardcode_read_only_as_the_only_healthy_mode():
    text = _text()
    assert "read_only -ne $true" not in text
    assert "did not preserve the expected read-only" not in text
    assert "the read-only API is healthy" not in text


def test_deployer_validates_known_modes_and_allowed_commands():
    text = _text()
    for mode in KNOWN_MODES:
        assert f"'{mode}'" in text
    assert "runtime.allowed_commands" in text
    assert "has no runtime.allowed_commands" in text


def test_apply_captures_live_mode_before_activation_and_requires_it_after():
    text = _text()
    capture = text.index("$preDeployMode = [string]$preDeployRuntime.mode")
    activation = text.index("Activating staged Scene Studio backend")
    assert capture < activation
    assert text.count("Restart-AppDaemonAddon -ExpectedMode $preDeployMode") >= 2
    assert "runtime mode changed during package deploy" in text
    assert "package deploy must preserve this mode" in text


def test_verify_only_checks_health_without_preserving_a_captured_mode():
    text = _text()
    start = text.index("if ($VerifyOnly)")
    end = text.index("exit 0", start)
    block = text[start:end]
    assert "Test-SceneStudioService" in block
    assert "-ExpectedMode" not in block
    assert "Assert-RemotePackageMatchesLocal" in block
    assert "mode=$($runtime.mode)" in block
