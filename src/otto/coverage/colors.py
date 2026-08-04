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

# Non-tier line states (spec §9). "uncovered" is a light red.
STATE_COLORS: dict[str, str] = {
    "uncovered": "#f4a9a8",
    "excluded": "grey",
    "stale": "violet",
    "aging": "tan",
}
