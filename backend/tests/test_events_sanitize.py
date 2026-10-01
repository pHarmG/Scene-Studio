import pytest

from scene_studio.domain.events import EventCategory, EventLevel, OperationalEvent
from scene_studio.domain.serde import ValidationError
from scene_studio.domain.sanitize import REDACTED, REDACTED_JWT, contains_secret_shape, sanitize_tree


def _event(**overrides) -> dict:
    data = {
        "timestamp": "2026-09-10T21:04:00Z",
        "level": "info",
        "category": "scene",
        "summary": "Twilight started",
        "detail": "14 fixtures • dynamic",
        "scene_id": "twilight",
    }
    data.update(overrides)
    return data


def test_event_round_trip():
    event = OperationalEvent.from_dict(_event())
    assert OperationalEvent.from_dict(event.to_dict()) == event


def test_event_validates_level_and_category():
    with pytest.raises(ValidationError, match="level"):
        OperationalEvent.from_dict(_event(level="catastrophic"))
    with pytest.raises(ValidationError, match="category"):
        OperationalEvent.from_dict(_event(category="gossip"))


def test_event_requires_timestamp_and_summary():
    with pytest.raises(ValidationError, match="timestamp"):
        OperationalEvent.from_dict(_event(timestamp="yesterday"))
    with pytest.raises(ValidationError, match="summary"):
        OperationalEvent.from_dict(_event(summary=""))


# ---------------------------------------------------------------------------
# sanitization (Phase 0 standing contract)
# ---------------------------------------------------------------------------

def test_jwt_shapes_are_redacted_anywhere():
    dirty = {
        "notes": "called with eyJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJibWFpbnRlbiIsImV4cCI6OTk5OTk5OTk5OX0.abc123def456 ok",  # secrets-scan:allow (fake test fixture)
        "nested": [{"config": "token-prefix eyJabc123456.def456ghi789 trailing"}],  # secrets-scan:allow (fake test fixture)
    }
    clean = sanitize_tree(dirty)
    assert not contains_secret_shape(str(clean))
    assert REDACTED_JWT in clean["notes"]


def test_sensitive_keys_are_redacted():
    dirty = {
        "token": "super-secret-value",
        "hue_username": "5ycelAAAAAAAAAAAAAAAAAA",
        "Authorization": "Bearer xyz",
        "endpoint_hint": "http://wled.local",
        "nested": {"api_key": "k" * 30, "color": "#ff8f00"},
    }
    clean = sanitize_tree(dirty)
    assert clean["token"] == REDACTED
    assert clean["hue_username"] == REDACTED
    assert clean["Authorization"] == REDACTED
    assert clean["nested"]["api_key"] == REDACTED
    assert clean["endpoint_hint"] == "http://wled.local"  # hints are not secrets
    assert clean["nested"]["color"] == "#ff8f00"


def test_non_string_scalars_pass_through():
    assert sanitize_tree({"bri": 128, "on": True, "x": None}) == {"bri": 128, "on": True, "x": None}


def test_levels_and_categories_enums_usable_in_events():
    assert EventLevel("warning") is EventLevel.WARNING
    assert EventCategory("discovery") is EventCategory.DISCOVERY
