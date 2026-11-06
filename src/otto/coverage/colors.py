"""Coverage rendering palettes: per-tier and per-state default colors.

Values are CSS colors, validated at settings load by
:func:`otto.models.color.validate_color`; the renderer consumes them
verbatim.
"""

# Per-kind defaults when a tier declares no explicit color (spec §9).
DEFAULT_TIER_COLORS: dict[str, str] = {
    "e2e": "green",
    "unit": "yellow",
    "manual": "orange",
}


def default_tier_color(kind: str) -> str:
    """Return the colour a tier gets when it declares none, keyed by its *kind*.

    An unknown kind (the renderer only has a tier's name for an undeclared
    tier) falls back to the ``e2e`` colour.
    """
    return DEFAULT_TIER_COLORS.get(kind, DEFAULT_TIER_COLORS["e2e"])


# Non-tier line states (spec §9). "uncovered" is a light red.
STATE_COLORS: dict[str, str] = {
    "uncovered": "#f4a9a8",
    "excluded": "grey",
    "stale": "violet",
    "aging": "tan",
}
