"""The coverage floors are codified minimums, not a record of the last measurement.

Two numbers gate Python coverage: ``COVERAGE_THRESHOLD`` (the full fold —
``make coverage``'s Python legs plus the dashboard browser process's
``--cov-append``) and ``CI_COVERAGE_THRESHOLD`` (the hostless selection CI
runs and gates on the primary Python, 3.10; the 3.15 canary runs the unit
and e2e tiers minus the ``interpreter_agnostic`` family without coverage
instrumentation, and the interior versions a trimmed, uninstrumented unit tier —
see ``noxfile.py``'s ``HOSTLESS_MIDDLE_TEST_ARGS`` and
``HOSTLESS_E2E_CANARY_TEST_ARGS``). Both live
in the Makefile and nowhere else. CI's hostless floor is judged by ``make
coverage-hostless-fold`` on the fold of the primary's three nox legs (each leg
writes a partial report at ``--cov-fail-under=0``); ``make coverage-hostless``
reads the same variable by hand. The noxfile carries no positive floor.

This module turns the floors into decisions. Every floor is a COMBINED
line+branch number: ``.coveragerc`` sets ``branch = true`` (pinned below), so
``--cov-fail-under`` judges statements and branch arcs together and the console
report carries the Branch / BrPart columns. Measured on 2026-09-11 with
branches on: the full fold at 95.80 % (lines 96.84 %, branches 92.24 %); the
hostless selection at 95.08 % (lines 96.17 %, branches 91.33 %). Lines-only
history, 2026-08-25: full fold 96.27-96.31 % over four runs; hostless
95.21-95.29 % across the five CI Pythons and 95.18-95.22 % on a local 3.14
leg. The floors below sit just under the measurements, so ordinary work that
loses a few dozen covered lines or branches has to argue with this file
instead of quietly ratcheting the number down. Raising a floor is free;
lowering one edits a constant here, on purpose, in a reviewed diff.

The comparison has to mean its number for that to hold. ``--cov-fail-under``
compares ``round(total, precision)`` and coverage's default precision is 0,
so a nominal 96 accepted 95.50 — half a point, ~120 statements here, that
no comment mentioned. ``.coveragerc`` sets ``precision = 2``, pinned below.

Same shape as ``test_declared_harness_bounds.py``: the value is read from the
build file where it is declared, never restated here. The readers strip
comment lines and refuse duplicate declarations, because a regex that reads
the first match is one commented-out experiment away from reading the wrong
number. ``noxfile_floors`` is the exception: it is an inventory, returning
every floor it finds and refusing nothing; the "no floor in the noxfile"
claim lives in ``test_the_noxfile_carries_no_positive_floor``.
"""

import re

import pytest

from tests._fixtures.make_recipes import makefile_live_recipe
from tests._fixtures.paths import CHECKOUT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

_FULL_COVERAGE_FLOOR = 96.0
_HOSTLESS_COVERAGE_FLOOR = 94.75


def _live_lines(text: str) -> str:
    """*text* without its comment lines (Make ``#`` and Python ``#`` alike)."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def makefile_variable(text: str, name: str) -> str:
    """The value of the one live ``name :=`` (or ``?=``, ``=``) declaration, stripped.

    Loud on absence and on a second live declaration: Make takes the last, a first-match
    reader the first, and either guess could read the wrong value. Comment lines are not
    declarations.
    """
    matches = re.findall(rf"^{name}\s*[?:]?=[ \t]*(.*?)\s*$", _live_lines(text), re.MULTILINE)
    assert matches, f"Makefile no longer declares `{name}`"
    assert len(matches) == 1, f"Makefile declares `{name}` {len(matches)} times: {matches}"
    return matches[0]


def makefile_threshold(text: str, name: str) -> float:
    """The floor as declared; decimal since branch tracking (a quarter-point margin is real)."""
    value = makefile_variable(text, name)
    assert re.fullmatch(r"\d+(?:\.\d+)?", value), f"`{name}` is not a number: {value!r}"
    return float(value)


def noxfile_floors(text: str) -> "dict[str, list[float]]":
    """Every ``--cov-fail-under`` the noxfile carries, keyed by its ``*_ARGS`` tuple.

    Only live lines count (a commented-out floor is neither a gate nor a
    duplicate), and only floors inside a module-level ``NAME = (...)`` tuple:
    that is the one shape the sessions run (``session.run("pytest", *NAME, …)``,
    pinned by ``test_python_matrix_tiering``). A floor outside a bundle is
    outside the shape those pins police, so it is listed under its own key,
    ``"<outside a bundle>"``, for the no-floor pin to see.
    """
    live = _live_lines(text)
    floors: "dict[str, list[float]]" = {}
    covered: "list[tuple[int, int]]" = []
    for block in re.finditer(
        r"^([A-Z][A-Z0-9_]*_ARGS)\s*=\s*\((.*?)^\)", live, re.MULTILINE | re.DOTALL
    ):
        covered.append(block.span())
        found = [float(x) for x in re.findall(r"--cov-fail-under=(\d+(?:\.\d+)?)", block.group(2))]
        if found:
            floors[block.group(1)] = found
    outside = [
        float(m.group(1))
        for m in re.finditer(r"--cov-fail-under=(\d+(?:\.\d+)?)", live)
        if not any(start <= m.start() < end for start, end in covered)
    ]
    if outside:
        floors["<outside a bundle>"] = outside
    return floors


def makefile_recipe_floor(text: str, target: str) -> "list[str]":
    """The ``$(NAME)`` variables *target*'s recipe passes as a fail-under, in order.

    Both spellings count: pytest-cov's ``--cov-fail-under=`` and coverage's
    own ``--fail-under=``. The recipe is what the shell runs, read by the one
    recipe reader (:func:`makefile_live_recipe`: comment and banner lines dropped).
    """
    assert re.search(rf"^{re.escape(target)}\s*:", text, re.MULTILINE), (
        f"Makefile no longer declares target `{target}`"
    )
    return [
        name
        for line in makefile_live_recipe(text, target)
        for name in re.findall(r"--(?:cov-)?fail-under=\$\(([A-Z_]+)\)", line)
    ]


def coveragerc_branch(text: str) -> bool:
    """Whether ``[run]`` measures branches — the floors are combined numbers only if so."""
    section = re.search(r"^\[run\](.*?)(?=^\[|\Z)", text, re.MULTILINE | re.DOTALL)
    assert section is not None, ".coveragerc has no [run] section"
    live = "\n".join(
        line for line in section.group(1).splitlines() if not line.lstrip().startswith(";")
    )
    match = re.search(r"^branch\s*=\s*(\w+)\s*$", live, re.MULTILINE)
    return match is not None and match.group(1).lower() == "true"


def coveragerc_precision(text: str) -> int:
    section = re.search(r"^\[report\](.*?)(?=^\[|\Z)", text, re.MULTILINE | re.DOTALL)
    assert section is not None, ".coveragerc has no [report] section"
    match = re.search(r"^precision\s*=\s*(\d+)\s*$", _live_lines(section.group(1)), re.MULTILINE)
    assert match is not None, ".coveragerc's [report] sets no precision"
    return int(match.group(1))


def coveragerc_show_contexts(text: str) -> bool:
    """`[html] show_contexts = True`, ignoring `;` and `#` comment lines."""
    live = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith((";", "#")))
    section = re.search(r"^\[html\][^\n]*(?:\n|\Z)(.*?)(?=^\[|\Z)", live, re.MULTILINE | re.DOTALL)
    assert section is not None, ".coveragerc has no [html] section"
    match = re.search(r"^show_contexts\s*=\s*(\w+)", section.group(1), re.MULTILINE)
    return match is not None and match.group(1).lower() == "true"


def test_the_html_report_shows_which_tests_covered_each_line() -> None:
    """`--cov-context=test` records the contexts; `show_contexts` renders them (owner decision)."""
    assert coveragerc_show_contexts((CHECKOUT_ROOT / ".coveragerc").read_text())
    assert not coveragerc_show_contexts("[html]\ndirectory = x\n")
    assert not coveragerc_show_contexts("[html]\n; show_contexts = True\n")
    assert coveragerc_show_contexts("[run]\nbranch = true\n[html]\nshow_contexts = True\n[paths]\n")
    assert "--cov-context=test" in (CHECKOUT_ROOT / "pyproject.toml").read_text(), (
        "the contexts the report shows are recorded by addopts"
    )


def test_the_full_fold_floor_holds_at_its_codified_minimum() -> None:
    value = makefile_threshold((CHECKOUT_ROOT / "Makefile").read_text(), "COVERAGE_THRESHOLD")
    assert value >= _FULL_COVERAGE_FLOOR, (
        f"COVERAGE_THRESHOLD is {value}, below the codified {_FULL_COVERAGE_FLOOR}% floor for "
        f"the full fold. Lowering it is a decision: change _FULL_COVERAGE_FLOOR here, in the "
        f"same diff, and say why"
    )


def test_the_hostless_floor_holds_at_its_codified_minimum() -> None:
    value = makefile_threshold((CHECKOUT_ROOT / "Makefile").read_text(), "CI_COVERAGE_THRESHOLD")
    assert value >= _HOSTLESS_COVERAGE_FLOOR, (
        f"CI_COVERAGE_THRESHOLD is {value}, below the codified {_HOSTLESS_COVERAGE_FLOOR}% floor "
        f"for the hostless selection CI runs. Lowering it is a decision: change "
        f"_HOSTLESS_COVERAGE_FLOOR here, in the same diff, and say why"
    )


def test_the_fold_and_the_by_hand_target_read_the_one_hostless_floor() -> None:
    """CI judges the fold; a by-hand run judges its own fold: one variable for both."""
    text = (CHECKOUT_ROOT / "Makefile").read_text()
    assert makefile_recipe_floor(text, "coverage-hostless") == ["CI_COVERAGE_THRESHOLD"]
    assert makefile_recipe_floor(text, "coverage-hostless-fold") == ["CI_COVERAGE_THRESHOLD"]
    assert "--fail-under=$(CI_COVERAGE_THRESHOLD)" in _live_lines(text)


def test_the_makefile_still_gates_on_both_variables() -> None:
    """A floor nobody applies is a number, not a gate: each variable must reach a fail-under."""
    live = _live_lines((CHECKOUT_ROOT / "Makefile").read_text())
    for name in ("COVERAGE_THRESHOLD", "CI_COVERAGE_THRESHOLD"):
        assert re.search(rf"--(?:cov-)?fail-under=\$\({name}\)", live), (
            f"no Makefile recipe applies `--cov-fail-under=$({name})` or "
            f"`--fail-under=$({name})` — the floor is declared but enforced nowhere"
        )


def test_the_floors_judge_branches_as_well_as_lines() -> None:
    """Every floor here was measured with branch tracking ON; without it the same
    tree reports a higher number while measuring less, and a floor set against
    the combined total would then pass on lines alone."""
    assert coveragerc_branch((CHECKOUT_ROOT / ".coveragerc").read_text()), (
        ".coveragerc [run] no longer sets `branch = true`: the coverage floors are "
        "combined line+branch numbers and would silently gate lines only"
    )
    assert not coveragerc_branch("[run]\nsource = otto\n; branch = true\n")
    assert not coveragerc_branch("[run]\nbranch = false\n[report]\nbranch = true\n")
    assert coveragerc_branch("[run]\nsource = otto\nbranch = True\n[report]\n")


def test_the_comparison_is_to_two_decimals_not_to_a_rounding() -> None:
    """``round(total, 0) >= 96`` accepts 95.50; the floor has to compare to its own number."""
    assert coveragerc_precision((CHECKOUT_ROOT / ".coveragerc").read_text()) >= 2, (
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
    # A decimal floor reads as its own number, not truncated to the integer part
    # (a synthetic value, independent of the production floor).
    assert makefile_threshold("COVERAGE_THRESHOLD := 95.25\n", "COVERAGE_THRESHOLD") == 95.25
    # Two live declarations: Make takes the last, a first-match reader the first; refuse both.
    with pytest.raises(AssertionError, match="2 times"):
        makefile_threshold(
            "COVERAGE_THRESHOLD := 80\nCOVERAGE_THRESHOLD := 96\n", "COVERAGE_THRESHOLD"
        )
    # `CI_COVERAGE_THRESHOLD` must not satisfy a search for `COVERAGE_THRESHOLD`.
    with pytest.raises(AssertionError, match="no longer declares"):
        makefile_threshold("CI_COVERAGE_THRESHOLD := 95\n", "COVERAGE_THRESHOLD")

    # A floor outside any bundle is listed under its own key, never dropped.
    assert noxfile_floors('def s(x):\n    x.run("pytest", "--cov-fail-under=90")\n') == {
        "<outside a bundle>": [90.0]
    }
    # A digit in a bundle name is still a bundle.
    assert noxfile_floors('HOSTLESS_E2E_ARGS = (\n    "--cov-fail-under=0",\n)\n') == {
        "HOSTLESS_E2E_ARGS": [0.0]
    }

    recipe = (
        "a: ## help\n\t# --fail-under=$(X)\n\tcoverage report --fail-under=$(CI_T)\n"
        "b:\n\tpytest --cov-fail-under=$(OTHER)\n"
    )
    assert makefile_recipe_floor(recipe, "a") == ["CI_T"]
    assert makefile_recipe_floor(recipe, "b") == ["OTHER"]
    assert makefile_recipe_floor("a:\n\techo hi\n", "a") == []
    with pytest.raises(AssertionError, match="no longer declares target"):
        makefile_recipe_floor(recipe, "a-fold")

    assert coveragerc_precision("[run]\nprecision = 0\n[report]\n; note\nprecision = 2\n") == 2
    with pytest.raises(AssertionError, match="sets no precision"):
        coveragerc_precision("[report]\n; precision = 2\n[html]\n")


def test_the_noxfile_carries_no_positive_floor() -> None:
    """No floor in the noxfile: each instrumented leg writes a slice at 0.

    The hostless floor judges the fold of the primary's three legs (``make
    coverage-hostless-fold``, reading the Makefile's ``CI_COVERAGE_THRESHOLD``);
    a positive floor on any one slice would judge part of the selection against
    a number set for the whole. Owner ruling 5 (#566): no canary floor either.
    """
    floors = noxfile_floors((CHECKOUT_ROOT / "noxfile.py").read_text())
    assert floors, "the instrumented primary legs no longer pass --cov-fail-under=0"
    assert "<outside a bundle>" not in floors, floors
    assert all(v == 0.0 for values in floors.values() for v in values), (
        f"the noxfile carries a positive coverage floor: {floors}. The floor lives in the "
        "Makefile's CI_COVERAGE_THRESHOLD and judges the fold of the three primary legs"
    )


def test_the_floor_reader_sees_every_bundle_and_what_lies_outside_one() -> None:
    synthetic = (
        'HOSTLESS_TEST_ARGS = (\n    "--cov-fail-under=0",\n)\n'
        'HOSTLESS_SERIAL_ARGS = (\n    "-n0",\n    # "--cov-fail-under=90",\n'
        '    "--cov-fail-under=94.75",\n)\n'
        'TESTS_ALL_SERIAL_ARGS = (\n    "--cov-append",\n    "--cov-fail-under=92",\n)\n'
        'def session(s):\n    s.run("pytest", "--cov-fail-under=80")\n'
    )
    assert noxfile_floors(synthetic) == {
        "HOSTLESS_TEST_ARGS": [0.0],
        "HOSTLESS_SERIAL_ARGS": [94.75],
        "TESTS_ALL_SERIAL_ARGS": [92.0],
        "<outside a bundle>": [80.0],
    }
    assert noxfile_floors('X_ARGS = (\n    "-n0",\n)\n') == {}
