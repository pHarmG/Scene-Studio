import pytest

from scene_studio.domain.commands import (
    COMMAND_CATALOG,
    CommandEnvelope,
    CommandResult,
    ErrorCode,
    ApplySceneParams,
    failure,
    parse_command,
    success,
    validation_error_result,
)
from scene_studio.domain.serde import ValidationError


# ---------------------------------------------------------------------------
# envelope
# ---------------------------------------------------------------------------

def test_envelope_uses_flattened_params_like_plan_example():
    envelope, params = parse_command(
        {"command": "scene.apply", "scene_id": "twilight", "target_id": "office", "dry_run": False}
    )
    assert envelope.command == "scene.apply"
    assert isinstance(params, ApplySceneParams)
    assert params.scene_id == "twilight"
    assert params.target_id == "office"


def test_envelope_rejects_unknown_command():
    with pytest.raises(ValidationError, match="unknown command"):
        CommandEnvelope.from_dict({"command": "lights.explode"})


def test_envelope_request_id_is_preserved():
    envelope, _ = parse_command({"command": "discovery.run", "request_id": "req-42"})
    assert envelope.request_id == "req-42"


def test_catalog_covers_plan_command_list():
    expected = {
        "scene.apply", "scene.rename", "scene.archive", "scene.restore", "scene.save",
        "scene.preview", "playback.start", "playback.pause", "playback.stop",
        "fixture.enable", "fixture.disable", "fixture.rebind", "discovery.run",
    }
    assert expected <= set(COMMAND_CATALOG)
    assert {"fixture.reconcile", "fixture.reconcile_preview", "registry.migrate", "registry.migration_preview"} <= set(COMMAND_CATALOG)


# ---------------------------------------------------------------------------
# params validation
# ---------------------------------------------------------------------------

def test_apply_params_defaults():
    params = ApplySceneParams.from_dict({"scene_id": "twilight"})
    assert params.dry_run is False
    assert params.target_id is None
    assert params.transition_ms is None


def test_apply_params_reject_unknown_keys():
    with pytest.raises(ValidationError, match="unknown key"):
        ApplySceneParams.from_dict({"scene_id": "t", "hue_ip": "hue-bridge.local"})


def test_rebind_requires_observation_id():
    with pytest.raises(ValidationError, match="observation_id"):
        parse_command({"command": "fixture.rebind", "fixture_id": "g_strip"})


def test_playback_stop_is_session_addressed():
    envelope, params = parse_command({"command": "playback.stop", "session_id": "sess-x"})
    assert params.session_id == "sess-x"
    with pytest.raises(ValidationError, match="session_id"):
        parse_command({"command": "playback.stop"})


def test_playback_resume_is_session_addressed():
    envelope, params = parse_command({"command": "playback.resume", "session_id": "sess-x"})
    assert envelope.command == "playback.resume"
    assert params.session_id == "sess-x"


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------

def test_result_success_round_trip():
    result = success("scene.apply", {"render_plan": {"scene_id": "twilight"}}, request_id="r1")
    data = result.to_dict()
    assert data["ok"] is True
    assert CommandResult.from_dict(data) == result


def test_result_failure_round_trip():
    result = failure("scene.apply", ErrorCode.NOT_FOUND, "scene 'nope' not found", request_id="r2")
    data = result.to_dict()
    assert data["ok"] is False
    assert data["error"]["code"] == "not_found"
    assert CommandResult.from_dict(data) == result


def test_validation_error_result_carries_path():
    result = validation_error_result("scene.apply", ValidationError("scene.motion.speed", "must be within 0.0..1.0"))
    assert result.error.code is ErrorCode.VALIDATION_ERROR
    assert result.error.details["path"] == "scene.motion.speed"


def test_result_requires_boolean_ok():
    with pytest.raises(ValidationError, match="ok"):
        CommandResult.from_dict({"command": "x", "ok": "yes"})


def test_diagnostics_export_params_parse():
    """Regression: ExportDiagnosticsParams used require_int without importing it
    (found by the integration wave); every diagnostics.export command hit a
    NameError at parse time."""
    envelope, params = parse_command({"command": "diagnostics.export", "recent_events": 25})
    assert envelope.command == "diagnostics.export"
    assert params.recent_events == 25
    assert params.redact is True
    with pytest.raises(ValidationError, match="recent_events"):
        parse_command({"command": "diagnostics.export", "recent_events": 0})
