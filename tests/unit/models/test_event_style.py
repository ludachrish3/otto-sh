"""otto.models.event_style — the one registry that decides event colours and dashes (#857)."""

import subprocess
import sys

import pytest

from otto.models.event_style import (
    DEFAULT_EVENT_COLOR,
    DEFAULT_EVENT_COLOR_ID,
    DEFAULT_EVENT_DASH,
    EVENT_COLOR_PRESETS,
    EVENT_DASH_STYLES,
    LIFECYCLE_COLOR_IDS,
    LIFECYCLE_DASHES,
    check_event_dash,
    normalize_event_color,
    preset_hex,
)
from tests._fixtures.paths import CHECKOUT_ROOT


def test_presets_exact() -> None:
    assert [(p.id, p.name, p.hex) for p in EVENT_COLOR_PRESETS] == [
        ("neutral", "Grey", "#888888"),
        ("annotation", "Violet", "#7c5cff"),
        ("success", "Green", "#2ca02c"),
        ("failure", "Red", "#d62728"),
    ]


def test_default_is_derived() -> None:
    assert DEFAULT_EVENT_COLOR_ID == "neutral"
    assert DEFAULT_EVENT_COLOR == preset_hex(DEFAULT_EVENT_COLOR_ID) == "#888888"


def test_lifecycle_ids_name_presets() -> None:
    assert LIFECYCLE_COLOR_IDS == {"start": "neutral", "pass": "success", "fail": "failure"}
    for preset_id in LIFECYCLE_COLOR_IDS.values():
        preset_hex(preset_id)  # every lifecycle id resolves


def test_lifecycle_dashes_cover_the_lifecycle_and_are_legal() -> None:
    assert LIFECYCLE_DASHES == {"start": "dash", "pass": "solid", "fail": "solid"}
    assert LIFECYCLE_DASHES.keys() == LIFECYCLE_COLOR_IDS.keys()
    for dash in LIFECYCLE_DASHES.values():
        check_event_dash(dash)


def test_dash_styles_exact() -> None:
    assert EVENT_DASH_STYLES == ("solid", "dot", "dash", "longdash", "dashdot", "longdashdot")
    assert DEFAULT_EVENT_DASH == "dash"


def test_unknown_preset_raises() -> None:
    with pytest.raises(KeyError):
        preset_hex("blue")


def test_normalize_accepts_short_and_upper() -> None:
    assert normalize_event_color("#ABC") == "#aabbcc"
    assert normalize_event_color("#7C5CFF") == "#7c5cff"
    assert normalize_event_color("#888888") == "#888888"


@pytest.mark.parametrize("value", ["red", "rgb(1,2,3)", "#aabbcc\n", "#12345", "#aabbccdd", ""])
def test_normalize_rejects_named_rgb_and_trailing_newline(value: str) -> None:
    with pytest.raises(ValueError, match=r"color must be #rrggbb, got"):
        normalize_event_color(value)


def test_leaf_imports_no_monitor() -> None:
    code = (
        "import sys, otto.models.event_style\n"
        "bad = sorted(k for k in sys.modules if k.startswith(('otto.monitor', 'otto.suite')))\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == ""


def test_generated_ts_matches_registry() -> None:
    from scripts.gen_event_style_ts import TS_PATH, render

    committed = (CHECKOUT_ROOT / TS_PATH).read_text(encoding="utf-8")
    assert render() == committed, "run scripts/gen_web_types.sh and commit the result"
