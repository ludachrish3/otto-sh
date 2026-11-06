"""The one registry that decides a monitor event's colour and dash style.

Every surface that draws, defaults, validates or offers an event style reads
it from here: the collector's write boundary and its lifecycle marks
(:data:`AUTO_EVENT_COLORS`), the HTTP body models and the
import records in :mod:`otto.models.monitor`, the SQLite DDL defaults, and the
dashboard, through ``web/src/api/eventStyle.gen.ts`` (written from this module
by ``scripts/gen_event_style_ts.py``; ``make web`` fails on drift).

The colour contract: an event colour is a hex string, ``#rrggbb`` or the
``#rgb`` short form, in any case. :func:`normalize_event_color` stores it as
lowercase ``#rrggbb``; anything else (a CSS name, ``rgb()``, an alpha byte) is
refused at every write boundary. A preset is a named hex, nothing more: any
other hex is a valid *custom* colour.

Leaf isolation: stdlib only. ``otto.models.monitor`` and
``otto.monitor.events`` both import this, so it can import neither.
"""

import re
from dataclasses import dataclass

__all__ = [
    "AUTO_EVENT_COLORS",
    "DEFAULT_EVENT_COLOR",
    "DEFAULT_EVENT_COLOR_ID",
    "DEFAULT_EVENT_DASH",
    "EVENT_COLOR_PATTERN",
    "EVENT_COLOR_PRESETS",
    "EVENT_DASH_STYLES",
    "LIFECYCLE_COLOR_IDS",
    "LIFECYCLE_DASHES",
    "EventColorPreset",
    "check_event_dash",
    "normalize_event_color",
    "preset_hex",
]


@dataclass(frozen=True)
class EventColorPreset:
    """One named colour the dashboard's editor offers as a swatch."""

    id: str
    """Stable identifier other code resolves the preset by (never the hex)."""

    name: str
    """The user-visible name, used as the swatch's accessible name and the editor's label."""

    hex: str
    """The colour, lowercase ``#rrggbb``."""


EVENT_COLOR_PRESETS: tuple[EventColorPreset, ...] = (
    EventColorPreset(id="neutral", name="Grey", hex="#888888"),
    EventColorPreset(id="annotation", name="Violet", hex="#7c5cff"),
    EventColorPreset(id="success", name="Green", hex="#2ca02c"),
    EventColorPreset(id="failure", name="Red", hex="#d62728"),
)
"""The editor's swatches, in display order."""

_PRESETS_BY_ID = {preset.id: preset for preset in EVENT_COLOR_PRESETS}


def preset_hex(preset_id: str) -> str:
    """Return the hex of the preset named *preset_id*; ``KeyError`` if there is none."""
    return _PRESETS_BY_ID[preset_id].hex


DEFAULT_EVENT_COLOR_ID = "neutral"
"""The preset a new event takes when no colour is given."""

DEFAULT_EVENT_COLOR = preset_hex(DEFAULT_EVENT_COLOR_ID)
"""The hex of :data:`DEFAULT_EVENT_COLOR_ID`, derived from the presets."""

LIFECYCLE_COLOR_IDS = {"start": "neutral", "pass": "success", "fail": "failure"}
"""The preset each automatic test-lifecycle mark takes, keyed by phase/outcome."""

AUTO_EVENT_COLORS = {phase: preset_hex(preset) for phase, preset in LIFECYCLE_COLOR_IDS.items()}
"""Colours of the automatic test-lifecycle marks (``start``/``pass``/``fail``),
resolved from :data:`LIFECYCLE_COLOR_IDS`. :mod:`otto.monitor.events`
re-exports this same object."""

EVENT_DASH_STYLES: tuple[str, ...] = (
    "solid",
    "dot",
    "dash",
    "longdash",
    "dashdot",
    "longdashdot",
)
"""The legal event dash styles (plotly names), in display order."""

DEFAULT_EVENT_DASH = "dash"
"""The dash a new event takes when none is given."""

LIFECYCLE_DASHES = {"start": "dash", "pass": "solid", "fail": "solid"}
"""The dash each automatic test-lifecycle mark takes, keyed like
:data:`LIFECYCLE_COLOR_IDS`: a dashed start, a solid pass or fail."""

EVENT_COLOR_PATTERN = r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})"
"""A legal event colour. Unanchored on purpose, because every user applies it as a
WHOLE-string match (``re.fullmatch`` here, ``^(?:…)$`` in the dashboard), so a
trailing newline never passes, which ``re.match`` with ``$`` would allow."""

_EVENT_COLOR_RE = re.compile(EVENT_COLOR_PATTERN)
_SHORT_HEX_DIGITS = 3  # "#rgb"


def normalize_event_color(value: str) -> str:
    """Return *value* as lowercase ``#rrggbb``; ``ValueError`` unless it is hex.

    Accepts ``#rrggbb`` or ``#rgb`` in any case. ``#ABC`` becomes ``#aabbcc``.
    """
    if not isinstance(value, str) or _EVENT_COLOR_RE.fullmatch(value) is None:
        raise ValueError(f"color must be #rrggbb, got {value!r}")
    digits = value[1:].lower()
    if len(digits) == _SHORT_HEX_DIGITS:
        digits = "".join(c * 2 for c in digits)
    return f"#{digits}"


def check_event_dash(value: str) -> str:
    """Return *value* unchanged; ``ValueError`` unless it is one of :data:`EVENT_DASH_STYLES`."""
    if value not in EVENT_DASH_STYLES:
        raise ValueError(f"dash must be one of {sorted(EVENT_DASH_STYLES)}, got {value!r}")
    return value
