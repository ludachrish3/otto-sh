"""The Makefile's per-leg wall-clock cap, read one way by every test that needs it."""

import re


def makefile_session_cap(text: str) -> int:
    """The declared ``LANE_TIMEOUT`` in seconds; loud on absence or duplication.

    Accepts ``:=`` or ``?=`` (the environment may override the cap, never
    replace its declared default). Refuses a second declaration rather than
    reading the first or the last: Make's ``:=`` lets a later line win at
    runtime (the same trap
    ``tests/unit/test_declared_harness_bounds.py`` reads the LAST vitest bound
    for), and a reader that picked either silently would green the gate on the
    wrong number.
    """
    matches = re.findall(r"^LANE_TIMEOUT\s*[:?]=\s*(\d+)s\s*$", text, re.MULTILINE)
    assert matches, (
        "the Makefile no longer declares LANE_TIMEOUT — the coverage legs would run "
        "unbounded and a hung integration test would stall the pipeline until the CI "
        "job limit killed it with no per-leg attribution"
    )
    assert len(matches) == 1, f"the Makefile declares LANE_TIMEOUT {len(matches)} times: {matches}"
    return int(matches[0])
