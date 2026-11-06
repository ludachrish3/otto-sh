"""Write ``web/src/api/eventStyle.gen.ts`` from :mod:`otto.models.event_style`.

The dashboard's event colours and dashes are the Python registry's values,
mirrored into TypeScript so neither side restates them (#857). Values only:
the generator emits constants and the types that name them, never component
code; the authored helpers over them live in ``web/src/data/eventStyle.ts``.

Run by ``scripts/gen_web_types.sh`` (and so by ``make web``, which then diffs
the committed file against the index and fails on drift). Deterministic:
running it twice writes byte-identical output. The parity test is
``tests/unit/models/test_event_style.py::test_generated_ts_matches_registry``.

Usage: ``uv run python scripts/gen_event_style_ts.py``
"""

import json
import sys
from pathlib import Path

from otto.models.event_style import (
    DEFAULT_EVENT_COLOR,
    DEFAULT_EVENT_DASH,
    EVENT_COLOR_PATTERN,
    EVENT_COLOR_PRESETS,
    EVENT_DASH_STYLES,
    LIFECYCLE_COLOR_IDS,
    LIFECYCLE_DASHES,
)

TS_PATH = Path("web/src/api/eventStyle.gen.ts")
"""Where the mirror lives, relative to the checkout root."""

_BANNER = (
    "/* AUTO-GENERATED from src/otto/models/event_style.py"
    " — run scripts/gen_web_types.sh; do not edit. */"
)


def _q(value: str) -> str:
    """Return *value* as a TS string literal (JSON double-quoted escaping is valid TS)."""
    return json.dumps(value)


def render() -> str:
    """Return the full text of ``eventStyle.gen.ts``."""
    presets = "\n".join(
        f"  {{ id: {_q(p.id)}, name: {_q(p.name)}, hex: {_q(p.hex)} }},"
        for p in EVENT_COLOR_PRESETS
    )
    lifecycle = "\n".join(f"  {key}: {_q(value)}," for key, value in LIFECYCLE_COLOR_IDS.items())
    lifecycle_dashes = "\n".join(
        f"  {key}: {_q(value)}," for key, value in LIFECYCLE_DASHES.items()
    )
    dashes = "\n".join(f"  {_q(d)}," for d in EVENT_DASH_STYLES)
    return (
        f"{_BANNER}\n"
        "\n"
        "export interface EventColorPreset {\n"
        "  readonly id: string;\n"
        "  readonly name: string;\n"
        "  readonly hex: string;\n"
        "}\n"
        "\n"
        "export const EVENT_COLOR_PRESETS: readonly EventColorPreset[] = [\n"
        f"{presets}\n"
        "];\n"
        "\n"
        f"export const DEFAULT_EVENT_COLOR = {_q(DEFAULT_EVENT_COLOR)};\n"
        "\n"
        "export const LIFECYCLE_COLOR_IDS = {\n"
        f"{lifecycle}\n"
        "} as const;\n"
        "\n"
        "export const EVENT_DASH_STYLES = [\n"
        f"{dashes}\n"
        "] as const;\n"
        "\n"
        "export type EventDash = (typeof EVENT_DASH_STYLES)[number];\n"
        "\n"
        f"export const DEFAULT_EVENT_DASH: EventDash = {_q(DEFAULT_EVENT_DASH)};\n"
        "\n"
        "export const LIFECYCLE_DASHES = {\n"
        f"{lifecycle_dashes}\n"
        "} as const satisfies Record<keyof typeof LIFECYCLE_COLOR_IDS, EventDash>;\n"
        "\n"
        "/** Unanchored: apply it as a whole-string match, `^(?:…)$`. */\n"
        f"export const EVENT_COLOR_PATTERN = {_q(EVENT_COLOR_PATTERN)};\n"
    )


def main() -> int:
    """Write :data:`TS_PATH` under the checkout root (this script's parent's parent)."""
    root = Path(__file__).resolve().parent.parent
    (root / TS_PATH).write_text(render(), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
