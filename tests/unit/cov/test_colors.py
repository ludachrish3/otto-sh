"""Color validation for coverage tier config."""

from pathlib import Path

import pytest

from otto.coverage.colors import DEFAULT_TIER_COLORS, STATE_COLORS, default_tier_color
from otto.coverage.renderer import spa_data
from otto.coverage.store.model import CoverageStore
from otto.coverage.tiers import load_tiers
from otto.models.color import CSS_COLOR_NAMES, validate_color


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


def test_default_tier_color_known_kinds() -> None:
    for kind, color in DEFAULT_TIER_COLORS.items():
        assert default_tier_color(kind) == color


def test_default_tier_color_unknown_kind_falls_back_to_e2e() -> None:
    assert default_tier_color("no-such-kind") == DEFAULT_TIER_COLORS["e2e"]


@pytest.mark.parametrize("kind", sorted(DEFAULT_TIER_COLORS))
def test_declared_and_renderer_default_agree(kind: str) -> None:
    """A declared colourless tier (tiers.py) and an undeclared tier named for
    its kind (the renderer) resolve through the same decision."""
    (declared,) = load_tiers({"tiers": {kind: {"kind": kind, "precedence": 1}}}, root=Path("/proj"))
    rendered = spa_data._resolve_tier_colors(CoverageStore(tier_order=[kind]), [kind])
    assert declared.color == rendered[kind] == DEFAULT_TIER_COLORS[kind]


def test_renderer_default_goes_through_default_tier_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(spa_data, "default_tier_color", lambda kind: "#123456")
    resolved = spa_data._resolve_tier_colors(CoverageStore(tier_order=["mystery"]), ["mystery"])
    assert resolved == {"mystery": "#123456"}


def test_hex_color_with_trailing_newline_rejected() -> None:
    with pytest.raises(ValueError, match="color"):
        validate_color("#aabbcc\n")
