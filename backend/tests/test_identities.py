import pytest

from scene_studio.domain.identities import is_valid_id, normalize_name_to_id, validate_id
from scene_studio.domain.serde import ValidationError


@pytest.mark.parametrize("value", ["g_strip", "studio", "twilight", "a", "wled_seg_0", "x" * 64])
def test_valid_ids(value):
    assert is_valid_id(value)
    assert validate_id(value, "fixture_id", "test") == value


@pytest.mark.parametrize(
    "value",
    [
        "",                      # empty
        "G_Strip",               # uppercase
        "1abc",                  # starts with digit
        "_abc",                  # starts with underscore
        "has space",
        "has-dash",
        "has.dot",
        "x" * 65,                # too long
        None,
        123,
    ],
)
def test_invalid_ids(value):
    with pytest.raises(ValidationError):
        validate_id(value, "fixture_id", "test")


def test_error_message_names_the_id_kind():
    with pytest.raises(ValidationError, match="scene_id"):
        validate_id("Bad!", "scene_id", "scene.id")


def test_normalize_name_to_id():
    assert normalize_name_to_id("Hue G Strip") == "hue_g_strip"
    assert normalize_name_to_id("  Double   Strip ") == "double_strip"
    assert normalize_name_to_id("Twilight") == "twilight"
    assert normalize_name_to_id("!!!") == "unnamed"


def test_normalize_name_to_id_always_produces_valid_ids():
    for weird in ["Ünïcode Name", "!!", "123 start", "a" * 100]:
        assert is_valid_id(normalize_name_to_id(weird))
