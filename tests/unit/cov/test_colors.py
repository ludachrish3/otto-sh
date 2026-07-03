"""Color validation for coverage tier config."""

import pytest

from otto.coverage.colors import (
    CSS_COLOR_NAMES,
    DEFAULT_TIER_COLORS,
    STATE_COLORS,
    validate_color,
)


def test_hex_colors_accepted() -> None:
    assert validate_color("#22c55e") == "#22c55e"
    assert validate_color("#ABC123") == "#ABC123"


def test_named_colors_accepted() -> None:
    assert validate_color("green") == "green"
    assert validate_color("tan") == "tan"
    assert validate_color("Violet") == "Violet"  # case-insensitive lookup


@pytest.mark.parametrize("bad", ["#22c55", "#GGGGGG", "notacolor", "", "22c55e"])
def test_bad_colors_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="color"):
        validate_color(bad)


def test_default_palette_is_valid() -> None:
    for value in (*DEFAULT_TIER_COLORS.values(), *STATE_COLORS.values()):
        assert validate_color(value) == value


def test_css_names_include_basics() -> None:
    assert {"green", "yellow", "orange", "grey", "violet", "tan"} <= CSS_COLOR_NAMES
