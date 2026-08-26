"""The coverage floors are codified minimums, not a record of the last measurement.

Two numbers gate Python coverage: ``COVERAGE_THRESHOLD`` (the full fold —
``make coverage``'s Python legs plus the dashboard browser process's
``--cov-append``) and ``CI_COVERAGE_THRESHOLD`` (the hostless selection CI
runs on five Pythons). Both live in the Makefile; the hostless one is
restated in ``noxfile.py``'s ``HOSTLESS_SERIAL_ARGS`` because the nox session
is what CI actually invokes, and the two are a hand-kept pair — the noxfile
comment says "keep in step" and nothing enforced it.

This module turns the floors into decisions. Measured on 2026-08-25: the full
fold at 96.27-96.31 % over four runs; the hostless selection at 95.21-95.29 %
across the five CI Pythons and 95.18-95.22 % on a local 3.14 leg. The floors
below sit just under those, so ordinary work that loses a few dozen covered
lines has to argue with this file instead of quietly ratcheting the number
down. Raising a floor is free; lowering one edits a constant here, on
purpose, in a reviewed diff.

The comparison has to mean its number for that to hold. ``--cov-fail-under``
compares ``round(total, precision)`` and coverage's default precision is 0,
so a nominal 96 accepted 95.50 — half a point, ~120 statements here, that
no comment mentioned. ``.coveragerc`` sets ``precision = 2``, pinned below.

Same shape as ``test_declared_harness_bounds.py``: the value is read from the
build file where it is declared, never restated here. The readers strip
comment lines and refuse duplicate declarations, because a regex that reads
the first match is one commented-out experiment away from reading the wrong
number.
"""

import re

import pytest

from tests._fixtures.paths import PROJECT_ROOT

_FULL_COVERAGE_FLOOR = 96
_HOSTLESS_COVERAGE_FLOOR = 95


def _live_lines(text: str) -> str:
    """*text* without its comment lines (Make ``#`` and Python ``#`` alike)."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def makefile_threshold(text: str, name: str) -> int:
    matches = re.findall(rf"^{name}\s*[?:]?=\s*(\d+)\s*$", _live_lines(text), re.MULTILINE)
    assert matches, f"Makefile no longer declares `{name}`"
    assert len(matches) == 1, f"Makefile declares `{name}` {len(matches)} times: {matches}"
    return int(matches[0])


def noxfile_hostless_serial_floor(text: str) -> int:
    """The ``--cov-fail-under`` inside ``HOSTLESS_SERIAL_ARGS``, and only that one.

    The parallel leg deliberately gates at 0 (the threshold judges the whole
    session's fold, which only exists after the serial leg appends), so a
    bare search for ``--cov-fail-under`` would read whichever leg came first.
    """
    block = re.search(r"^HOSTLESS_SERIAL_ARGS\s*=\s*\((.*?)^\)", text, re.MULTILINE | re.DOTALL)
    assert block is not None, "noxfile.py no longer declares HOSTLESS_SERIAL_ARGS"
    matches = re.findall(r"--cov-fail-under=(\d+)", _live_lines(block.group(1)))
    assert matches, "HOSTLESS_SERIAL_ARGS carries no --cov-fail-under"
    assert len(matches) == 1, f"HOSTLESS_SERIAL_ARGS carries {len(matches)} floors: {matches}"
    return int(matches[0])


def coveragerc_precision(text: str) -> int:
    section = re.search(r"^\[report\](.*?)(?=^\[|\Z)", text, re.MULTILINE | re.DOTALL)
    assert section is not None, ".coveragerc has no [report] section"
    match = re.search(r"^precision\s*=\s*(\d+)\s*$", _live_lines(section.group(1)), re.MULTILINE)
    assert match is not None, ".coveragerc's [report] sets no precision"
    return int(match.group(1))


def test_the_full_fold_floor_holds_at_its_codified_minimum() -> None:
    value = makefile_threshold((PROJECT_ROOT / "Makefile").read_text(), "COVERAGE_THRESHOLD")
    assert value >= _FULL_COVERAGE_FLOOR, (
        f"COVERAGE_THRESHOLD is {value}, below the codified {_FULL_COVERAGE_FLOOR}% floor for "
        f"the full fold. Lowering it is a decision: change _FULL_COVERAGE_FLOOR here, in the "
        f"same diff, and say why"
    )


def test_the_hostless_floor_holds_at_its_codified_minimum() -> None:
    value = makefile_threshold((PROJECT_ROOT / "Makefile").read_text(), "CI_COVERAGE_THRESHOLD")
    assert value >= _HOSTLESS_COVERAGE_FLOOR, (
        f"CI_COVERAGE_THRESHOLD is {value}, below the codified {_HOSTLESS_COVERAGE_FLOOR}% floor "
        f"for the hostless selection CI runs. Lowering it is a decision: change "
        f"_HOSTLESS_COVERAGE_FLOOR here, in the same diff, and say why"
    )


def test_the_hostless_floor_is_one_number_in_both_build_files() -> None:
    """CI invokes the nox session and local runs the Makefile target: one floor for both.

    The two selections are a hand-kept pair by intent (``M_HOSTLESS`` against
    ``HOSTLESS_TEST_ARGS`` / ``HOSTLESS_SERIAL_ARGS``; ``test_lane_invariants``
    pins their serial_timing halves). This pins the NUMBER only: a local
    hostless green must mean what a CI green means.
    """
    make = makefile_threshold((PROJECT_ROOT / "Makefile").read_text(), "CI_COVERAGE_THRESHOLD")
    nox = noxfile_hostless_serial_floor((PROJECT_ROOT / "noxfile.py").read_text())
    assert make == nox, (
        f"the hostless floor has drifted: Makefile CI_COVERAGE_THRESHOLD={make} but "
        f"noxfile HOSTLESS_SERIAL_ARGS gates at {nox}. CI runs the nox session and `make "
        f"coverage-hostless` runs the Makefile leg; a local green must mean the same thing "
        f"as a CI green"
    )


def test_the_makefile_still_gates_on_both_variables() -> None:
    """A floor nobody applies is a number, not a gate: each variable must reach a fail-under."""
    live = _live_lines((PROJECT_ROOT / "Makefile").read_text())
    for name in ("COVERAGE_THRESHOLD", "CI_COVERAGE_THRESHOLD"):
        assert f"--cov-fail-under=$({name})" in live, (
            f"no Makefile recipe applies `--cov-fail-under=$({name})` — the floor is declared "
            f"but enforced nowhere"
        )


def test_the_comparison_is_to_two_decimals_not_to_a_rounding() -> None:
    """``round(total, 0) >= 96`` accepts 95.50; the floor has to compare to its own number."""
    assert coveragerc_precision((PROJECT_ROOT / ".coveragerc").read_text()) >= 2, (
        ".coveragerc [report] precision is below 2: --cov-fail-under compares round(total, "
        "precision), so precision 0 makes a floor of N enforce N-0.5"
    )


def test_the_floor_readers_fail_loud_on_the_shapes_they_must_not_guess() -> None:
    """Positive controls for every shape the readers refuse or ignore."""
    with pytest.raises(AssertionError, match="no longer declares"):
        makefile_threshold("COVERAGE_THRESHOLD_OLD := 95\n", "COVERAGE_THRESHOLD")
    # A commented-out experiment above the live line is neither read nor a duplicate.
    assert (
        makefile_threshold(
            "# COVERAGE_THRESHOLD := 80\nCOVERAGE_THRESHOLD := 96\n", "COVERAGE_THRESHOLD"
        )
        == 96
    )
    # The `?=` idiom this Makefile uses elsewhere is a declaration too.
    assert makefile_threshold("COVERAGE_THRESHOLD ?= 96\n", "COVERAGE_THRESHOLD") == 96
    # Two live declarations: Make takes the last, a first-match reader the first; refuse both.
    with pytest.raises(AssertionError, match="2 times"):
        makefile_threshold(
            "COVERAGE_THRESHOLD := 80\nCOVERAGE_THRESHOLD := 96\n", "COVERAGE_THRESHOLD"
        )
    # `CI_COVERAGE_THRESHOLD` must not satisfy a search for `COVERAGE_THRESHOLD`.
    with pytest.raises(AssertionError, match="no longer declares"):
        makefile_threshold("CI_COVERAGE_THRESHOLD := 95\n", "COVERAGE_THRESHOLD")

    decoy_first = (
        'HOSTLESS_TEST_ARGS = (\n    "--cov-fail-under=0",\n)\n'
        'HOSTLESS_SERIAL_ARGS = (\n    # was "--cov-fail-under=0",\n    "-n0",\n'
        '    "--cov-fail-under=95",\n)\n'
    )
    assert noxfile_hostless_serial_floor(decoy_first) == 95
    with pytest.raises(AssertionError, match="carries no"):
        noxfile_hostless_serial_floor('HOSTLESS_SERIAL_ARGS = (\n    "-n0",\n)\n')

    assert coveragerc_precision("[run]\nprecision = 0\n[report]\n; note\nprecision = 2\n") == 2
    with pytest.raises(AssertionError, match="sets no precision"):
        coveragerc_precision("[report]\n; precision = 2\n[html]\n")
