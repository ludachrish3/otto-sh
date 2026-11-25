"""The Makefile's per-leg wall-clock cap, read one way by every test that needs it."""

import re


def makefile_session_cap(text: str, name: str = "LANE_TIMEOUT") -> int:
    """The declared cap *name* in seconds; loud on absence or duplication.

    ``LANE_TIMEOUT`` unless another is named: a lane with its own cap
    (``CONFORMANCE_BED_TIMEOUT``) is read by the same rules.

    Accepts ``:=`` or ``?=`` (the environment may override the cap, never
    replace its declared default). Refuses a second declaration rather than
    reading the first or the last: Make's ``:=`` lets a later line win at
    runtime (the same trap
    ``tests/unit/test_declared_harness_bounds.py`` reads the LAST vitest bound
    for), and a reader that picked either silently would green the gate on the
    wrong number.
    """
    matches = re.findall(rf"^{re.escape(name)}\s*[:?]=\s*(\d+)s\s*$", text, re.MULTILINE)
    assert matches, (
        f"the Makefile no longer declares {name} — the legs it caps would run "
        "unbounded and a hung test would stall the pipeline until something outside "
        "the lane killed it with no per-leg attribution"
    )
    assert len(matches) == 1, f"the Makefile declares {name} {len(matches)} times: {matches}"
    return int(matches[0])
