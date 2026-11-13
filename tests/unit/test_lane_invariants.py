"""Build-lane invariants: an addopts override must not drop the tach guard.

``-p no:tach`` in pyproject's ``addopts`` is the load-bearing guard against
tach's pytest plugin panicking otto's own harness (issue #193: its Rust
extension installs a C-level Ctrl-C handler at import, and consecutive
in-process pytest sessions panic ``MultipleHandlers``). Any lane that clears
``addopts`` with ``-o addopts=...`` silently drops that guard — the root
conftest's stub-module mitigation seeds too late (the ``pytest11`` entry
point has already loaded by conftest import), and the dev venv can carry
tach after a ``uv run --group lint`` (see pyproject's dependency-groups
note). Three lanes had done exactly that when this pin landed:
``tests_unit_repeat`` and the ``docs`` doctest leg in noxfile.py, and
``doctest-src`` in the Makefile (review 2026-08-06 §5.4, gate G13).

Scope: the scanner covers exactly the two build files, ``noxfile.py`` and
``Makefile``. A further ``--override-ini addopts=`` site lives in PRODUCT
code (``src/otto/suite/run.py``, where every otto pytest session starts) and
re-created the same exposure for otto's own in-process pytest sessions — the
recorded live defect "otto test panics when tach is installed"
(todo/churn-review-cheap-items-followups.md). There the guard is not an
addopts value but direct ``"-p", "no:tach"`` argv elements, so that file
gets the companion literal pin below rather than the scanner.

The scanner is delimiter-aware line parsing, not "up to the next quote": the
value ends at the partner of the quote that actually delimits it — the quote
immediately BEFORE ``addopts`` (nox shape, ``"addopts=..."``) or immediately
AFTER the ``=`` (Make shape, ``-o addopts="..."``); unquoted values end at
whitespace, and an override whose quote never closes is reported as an
offender so unparseable is loud, never green. The first cut assumed the next
``"`` terminated the value, which meant a same-line comment *mentioning*
``-p no:tach`` — the most plausible way a human annotates removing it —
greened the guard on the comment text (caught in review; pinned below).

A second family of pins (bottom of this module) guards the serial_timing
lane: wall-clock discriminators whose slow arm sibling xdist workers can
counterfeit as a false red (three loaded-gate sightings of the lifecycle
force discriminators). Every parallel lane excludes the marker and
re-appends it in a paired ``-n0`` leg — the pins hold both directions:
exclusion without the leg (a CI-invisible test) and an unmarked
discriminator (the flake returns). The runtime backstop for lanes these
scanners cannot foresee lives in the root conftest, which fails any marked
test that reaches an xdist worker; its control is here too.

A third family (the two ``ast-grep test`` pins) holds the Makefile <-> noxfile
parity that both files' own comments ask for in prose: ``make lint-arch`` and
the noxfile ``lint`` session must each RUN the architecture rule tests, not
merely mention them. ``ast-grep scan`` does not execute
``.ast-grep/rule-tests/`` -- only ``ast-grep test`` does -- so a rule that
stopped matching its own motivating snippet would fail nothing the day the leg
is dropped from one of the two files. Both scanners ignore the shapes that
look like the leg without being it: a ``$(SAY)`` banner and a tab-commented
recipe line on the Make side, a docstring mention and a non-``session.run``
statement on the nox side.

A fourth family (the ``_Leg`` block, below the serial_timing pins) asks the
only lane-leg question the other two cannot: does the leg select ANYTHING?
Both scanners above audit leg TEXT; neither can see MEMBERSHIP, and an empty
leg is a hard failure — pytest exits 5 when it collects nothing and make
aborts on that, after paying for every leg ahead of it in the recipe. That
gate decides membership from real pytest collection, in a subprocess, because
a static model of this repository's markers reported a live lane empty; the
reasoning is in
:func:`test_every_lane_leg_selects_at_least_one_test`.

The release census at the end of the module holds ``make release``'s test
surface equal to a literal manifest and proves every collected test is
selected by some stage of it (issue #566).
"""

import ast
import dataclasses
import itertools
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from _pytest.mark.expression import Expression

from noxfile import (
    BED_CLAUSES,
    HOSTLESS_E2E_CANARY_SERIAL_ARGS,
    HOSTLESS_E2E_CANARY_TEST_ARGS,
    HOSTLESS_MIDDLE_SERIAL_ARGS,
    HOSTLESS_MIDDLE_TEST_ARGS,
    TESTS_ALL_NONPRIMARY_SERIAL_ARGS,
    TESTS_ALL_NONPRIMARY_TEST_ARGS,
    TESTS_BED_SERIAL_ARGS,
    TESTS_BED_TEST_ARGS,
)
from scripts import gate_scope as gs
from scripts.gate_stamp_common import test_roots as pyproject_testpaths
from tests._fixtures.make_recipes import is_commented_recipe, makefile_live_recipe
from tests._fixtures.paths import CHECKOUT_ROOT
from tests.unit.test_tier_marker_invariants import (
    _expand_makefile_variables,
    _makefile_marker_variables,
    _makefile_pytest_invocations,
    _marker_matcher,
    _python_pytest_invocations,
    _selected_roots,
)

pytestmark = pytest.mark.interpreter_agnostic

_REPO = CHECKOUT_ROOT

_QUOTES = ('"', "'")


def addopts_overrides(text: str) -> list[str]:
    """Every ``addopts=`` override value in *text*, comment lines excluded."""
    values: list[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        for m in re.finditer(r"addopts=", line):
            start = m.end()
            before = line[m.start() - 1] if m.start() > 0 else ""
            after = line[start] if start < len(line) else ""
            if before in _QUOTES:
                quote, vstart = before, start
            elif after in _QUOTES:
                quote, vstart = after, start + 1
            else:
                # Unquoted: the value is the leading non-space run (possibly
                # empty). A spaced unquoted value truncates and reads as an
                # offender — fail-loud is the right direction for this guard.
                match = re.match(r"\S*", line[start:])
                values.append((match.group(0) if match else "").rstrip(","))
                continue
            end = line.find(quote, vstart)
            if end == -1:
                values.append(f"<unterminated addopts override: {line.strip()}>")
                continue
            values.append(line[vstart:end])
    return values


def test_addopts_overrides_keep_the_tach_guard() -> None:
    offenders = [
        f"{name}: addopts={value!r}"
        for name in ("noxfile.py", "Makefile")
        for value in addopts_overrides((_REPO / name).read_text())
        if "-p no:tach" not in value
    ]
    assert not offenders, (
        "addopts override(s) drop the issue-#193 tach guard — every "
        "`-o addopts=...` must re-state `-p no:tach` (the conftest stub seeds "
        "too late to protect plugin load):\n  " + "\n  ".join(offenders)
    )


def test_product_pytest_sessions_keep_the_tach_guard() -> None:
    """The in-product pytest session must pass ``-p no:tach`` as argv.

    Every otto pytest session (run, collect-only, listing) starts in one
    place, which clears ``addopts`` (dropping pyproject's guard) before
    starting it, so it must re-assert the guard itself. The pin requires the
    *adjacent argv pair* ``"-p", "no:tach"`` — a prose mention in a comment
    (the plausible shape of an annotated removal, the same trap this
    module's scanner was reviewed for) and a stray quoted string elsewhere
    in the file both miss it. A commented-out copy of the exact pair on one
    line would still match; that shape is accepted as vanishingly unlikely
    rather than chased.
    """
    for rel in ("src/otto/suite/run.py",):
        text = (_REPO / rel).read_text()
        assert re.search(r'"-p",\s*"no:tach",', text), (
            f"{rel}: in-process pytest session lost its `-p no:tach` argv guard "
            "(issue #193: tach's pytest plugin panics otto-started sessions; "
            "the `addopts=` override there drops the pyproject guard)"
        )


def test_scanner_flags_a_guardless_override() -> None:
    """Positive control: the scanner observed red on both build-file shapes."""
    nox_bad = '        "addopts=",\n'
    nox_good = '        "addopts=-p no:tach",\n'
    make_bad = '\t@uv run pytest -o addopts="--doctest-modules" src/otto\n'
    make_good = '\t@uv run pytest -o addopts="--doctest-modules -p no:tach" src/otto\n'
    comment = "# clearing addopts= here would be bad\n"
    assert addopts_overrides(nox_bad) == [""]
    assert addopts_overrides(make_bad) == ["--doctest-modules"]
    assert all("-p no:tach" in v for v in addopts_overrides(nox_good + make_good))
    assert addopts_overrides(comment) == []


def test_scanner_is_not_fooled_by_a_same_line_mention() -> None:
    """Review catch: an annotated removal must stay red.

    Deleting the flag and leaving a comment naming it is the most plausible
    human edit; the comment text must never be read as the override's value.
    """
    annotated_nox = '        "addopts=",  # -p no:tach is unnecessary here\n'
    annotated_make = "\t@uv run pytest -o addopts='' src  # -p no:tach lives in pyproject\n"
    assert addopts_overrides(annotated_nox) == [""]
    assert addopts_overrides(annotated_make) == [""]


def test_scanner_sees_every_override_and_fails_loud_on_the_unparseable() -> None:
    two_on_one_line = '    session.run("pytest", "-o", "addopts=-p no:tach", "-o", "addopts=")\n'
    assert addopts_overrides(two_on_one_line) == ["-p no:tach", ""]
    bare_unquoted = "\t@uv run pytest -o addopts= src/otto\n"
    assert addopts_overrides(bare_unquoted) == [""]
    unterminated = '\t@uv run pytest -o addopts="--doctest-modules src/otto\n'
    (value,) = addopts_overrides(unterminated)
    assert "-p no:tach" not in value  # unparseable reads as an offender, never green


def test_doctest_modules_is_passed_where_collection_must_import_helpers() -> None:
    """The flag left addopts (0 doctests under testpaths); the two children that NEED
    it — collect-check, which imports the non-test helpers under testpaths, and the
    sibling-conftest regression, whose defect it triggers — pass it themselves."""
    pyproject = (CHECKOUT_ROOT / "pyproject.toml").read_text()
    addopts = re.search(r'^addopts = "([^"]*)"', pyproject, re.MULTILINE)
    assert addopts is not None
    assert "--doctest-modules" not in addopts.group(1), addopts.group(1)
    makefile = (CHECKOUT_ROOT / "Makefile").read_text()
    collect = [t for t in _makefile_pytest_invocations(makefile) if "--collect-only" in t]
    assert collect
    assert all("--doctest-modules" in t for t in collect), collect
    from tests.unit.test_conftest_doctest_collection import _CHILD_ARGS

    assert "--doctest-modules" in _CHILD_ARGS


# ── serial_timing lane ──────────────────────────────────────────────────────
# A serial leg is recognized by BOTH tokens: a marker expression that STARTS
# with the positive marker (the `"serial_timing and ` convention every leg
# follows — `not serial_timing` can never match it) and a literal -n0.
# Comment lines are stripped before either token is looked for, so a
# commented-out leg (the annotated-removal shape) can never green a block.
# Stated blind spot: the token check is per-BLOCK, so a leg that loses only
# its `-n0` while a sibling SAY line still says "-n0" greens here — the
# runtime conftest guard then errors every discriminator in that lane's
# parallel re-run, so the drop is deterministic-red, just later.
# Second stated blind spot (fable, W17c re-ack): these scanners only audit
# lanes that already spell `not serial_timing` — a POSITIVE-selector lane
# (M_UNIX, M_EMBEDDED, browser, ...) that newly selects a marked test
# because the test also carries that resource marker is invisible here and
# must be paired by hand; the conftest guard turns the omission into a
# deterministic error in that lane rather than a silent skip or a flake.
_SERIAL_LEG_TOKEN = '"serial_timing and '

# The module-level constant pairs noxfile.py may spell a serial_timing
# exclusion in: (parallel bundle, its -n0 twin) — every catch-all session's
# primary and non-primary pairs (the non-primary ones deselect the
# `interpreter_agnostic` family and run coverage off —
# tests/unit/test_python_matrix_tiering.py pins both), and the changed-test
# repeat's two hostless lanes, which run their lane's pair over the diff's files.
_CONSTANT_PAIRS = (
    ("UNIT_TEST_ARGS", "UNIT_SERIAL_ARGS"),
    ("UNIT_NONPRIMARY_TEST_ARGS", "UNIT_NONPRIMARY_SERIAL_ARGS"),
    ("INTEGRATION_TEST_ARGS", "INTEGRATION_SERIAL_ARGS"),
    ("INTEGRATION_NONPRIMARY_TEST_ARGS", "INTEGRATION_NONPRIMARY_SERIAL_ARGS"),
    ("HOSTLESS_UNIT_TEST_ARGS", "HOSTLESS_UNIT_SERIAL_ARGS"),
    ("HOSTLESS_E2E_TEST_ARGS", "HOSTLESS_E2E_SERIAL_ARGS"),
    ("HOSTLESS_E2E_CANARY_TEST_ARGS", "HOSTLESS_E2E_CANARY_SERIAL_ARGS"),
    ("HOSTLESS_MIDDLE_TEST_ARGS", "HOSTLESS_MIDDLE_SERIAL_ARGS"),
    ("TESTS_ALL_TEST_ARGS", "TESTS_ALL_SERIAL_ARGS"),
    ("TESTS_ALL_NONPRIMARY_TEST_ARGS", "TESTS_ALL_NONPRIMARY_SERIAL_ARGS"),
    ("TESTS_BED_TEST_ARGS", "TESTS_BED_SERIAL_ARGS"),
    ("CHANGED_REPEAT_UNIT_TEST_ARGS", "CHANGED_REPEAT_UNIT_SERIAL_ARGS"),
    ("CHANGED_REPEAT_E2E_TEST_ARGS", "CHANGED_REPEAT_E2E_SERIAL_ARGS"),
)

_SERIAL_TIMING_TESTS = {
    "tests/unit/test_lifecycle_sync_phase.py": (
        "test_second_signal_forces_immediately",
        "test_mixed_signal_pair_forces_regardless_of_order",
        # Both added 2026-08-08 with the two-channel force path. Same
        # discriminator as their siblings above — an elapsed bound BELOW the
        # child's own teardown deadline, so a force that did not happen is
        # told apart from one that did by the only signal there is.
        "test_second_signal_forces_even_when_its_handler_never_runs",
        "test_second_signal_forces_after_asyncio_takes_the_wakeup_fd_away",
        # Added 2026-09-09 with the arm-time wakeup-fd reclaim and the
        # absolute deadline. Same discriminator again — the first is told
        # apart from the deadline path only by an elapsed bound below the
        # child's 30s deadline, and the second asserts a wedged teardown dies
        # AT its 1s deadline while a foreign-signal storm runs.
        "test_second_signal_forces_when_the_fd_was_stolen_and_no_handler_runs",
        "test_foreign_wakeup_bytes_cannot_postpone_the_teardown_deadline",
    ),
    "tests/unit/host/test_session.py": ("test_recovery_timeout_rebind_is_live",),
    # Fourth sighting of the class, caught by this wave's own gates run: the
    # relative serial-vs-parallel docker exec discriminator false-failed when
    # sibling-worker load landed inside its parallel measurement window.
    "tests/integration/test_docker_run_get_put.py": ("test_exec_remains_concurrent_safe",),
}


def makefile_serial_lane_gaps(text: str) -> list[str]:
    """Targets whose recipes exclude serial_timing without a paired -n0 leg.

    Excluding the marker from a parallel lane is only half the fix — without
    the re-append leg the discriminators become CI-invisible (the recorded
    defect shape: deferred run-time enforcement + ``-m`` filtering). Blocks
    are recipe lines (tab-indented) grouped under their target line;
    commented-out recipe lines are dropped first, so only lines the shell
    would actually run can satisfy (or trigger) the check.
    """
    gaps: list[str] = []
    target: str | None = None
    block: list[str] = []

    def flush() -> None:
        live = [line for line in block if not is_commented_recipe(line)]
        if target is None or not any("not serial_timing" in line for line in live):
            return
        joined = "\n".join(live)
        if _SERIAL_LEG_TOKEN not in joined or "-n0" not in joined:
            gaps.append(f"{target}: excludes serial_timing without a paired -n0 leg")

    for line in text.splitlines():
        if line.startswith("\t"):
            block.append(line)
            continue
        matched = re.match(r"^([A-Za-z0-9_.-]+)\s*:", line)
        if matched:
            flush()
            target, block = matched.group(1), []
    flush()
    return gaps


def noxfile_serial_lane_gaps(text: str) -> list[str]:
    """Sessions (or module constants) that exclude serial_timing without a leg.

    Function-level: an exclusion inside a session must pair with a serial leg
    inside the same session. Module-level: the sanctioned shapes are the
    constant pairs in ``_CONSTANT_PAIRS`` — a ``*_TEST_ARGS`` bundle that
    excludes serial_timing and its ``*_SERIAL_ARGS`` twin, both run by one
    session — anything else module-level is reported so a novel shape extends
    this pin instead of sliding past it. Comment lines are dropped
    from every segment before the tokens are looked for (the same
    annotated-removal trap the addopts scanner was reviewed for): without
    the strip, a commented-out leg with any statement after it would keep
    its lines inside the def's AST span and green the check.
    """
    tree = ast.parse(text)
    lines = text.splitlines()
    gaps: list[str] = []
    def_segments: dict[str, str] = {}
    def_ranges: list[tuple[int, int]] = []
    assign_ranges: dict[str, tuple[int, int]] = {}

    def live_segment(start: int, end: int) -> str:
        return "\n".join(
            line for line in lines[start - 1 : end] if not line.lstrip().startswith("#")
        )

    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            end = node.end_lineno or node.lineno
            def_ranges.append((start, end))
            def_segments[node.name] = live_segment(start, end)
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target_node = node.targets[0]
            if isinstance(target_node, ast.Name):
                assign_ranges[target_node.id] = (node.lineno, node.end_lineno or node.lineno)

    for name, segment in def_segments.items():
        if "not serial_timing" not in segment:
            continue
        if _SERIAL_LEG_TOKEN not in segment or '"-n0"' not in segment:
            gaps.append(f"{name}: excludes serial_timing without a paired -n0 leg")

    module_hits = [
        lineno
        for lineno, line in enumerate(lines, start=1)
        if "not serial_timing" in line
        and not line.lstrip().startswith("#")
        and not any(start <= lineno <= end for start, end in def_ranges)
    ]

    def inside(lineno: int, name: str) -> bool:
        span = assign_ranges.get(name)
        return span is not None and span[0] <= lineno <= span[1]

    stray = [
        lineno
        for lineno in module_hits
        if not any(inside(lineno, test_name) for test_name, _ in _CONSTANT_PAIRS)
    ]
    gaps.extend(
        f"line {lineno}: module-level serial_timing exclusion outside "
        f"{'/'.join(test_name for test_name, _ in _CONSTANT_PAIRS)} — extend this pin "
        "for the new shape"
        for lineno in stray
    )
    for test_name, serial_name in _CONSTANT_PAIRS:
        if not any(inside(lineno, test_name) for lineno in module_hits):
            continue  # this bundle carries no exclusion (or does not exist)
        serial_span = assign_ranges.get(serial_name)
        if serial_span is None:
            gaps.append(f"{test_name} excludes serial_timing but {serial_name} is gone")
            continue
        serial_segment = live_segment(serial_span[0], serial_span[1])
        if _SERIAL_LEG_TOKEN not in serial_segment or '"-n0"' not in serial_segment:
            gaps.append(f"{serial_name} lost its positive serial_timing expression or -n0")
        if not any(
            test_name in segment and serial_name in segment for segment in def_segments.values()
        ):
            gaps.append(f"no session runs both {test_name} and {serial_name}")
    return gaps


def test_serial_timing_marker_is_registered() -> None:
    text = (_REPO / "pyproject.toml").read_text()
    assert re.search(r'^\s*"serial_timing: ', text, re.MULTILINE), (
        "the serial_timing marker is no longer registered in pyproject.toml — the "
        "-n0 lane's `-m serial_timing` legs would select nothing and the marked "
        "discriminators would run wherever load can counterfeit them"
    )


def test_known_wall_clock_discriminators_carry_the_marker() -> None:
    """Unmarking a discriminator quietly re-enters it into the parallel lanes.

    That is exactly how the flake would return: the test looks load-innocent in
    isolation, someone drops the marker, and the third loaded-gate sighting
    becomes a fourth. A renamed or deleted test fails here too — the pin must
    follow the test, never silently green on its absence.
    """
    problems: list[str] = []
    for rel, names in _SERIAL_TIMING_TESTS.items():
        tree = ast.parse((_REPO / rel).read_text())
        defs = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        for name in names:
            node = defs.get(name)
            if node is None:
                problems.append(f"{rel}: {name} not found — renamed/deleted? Update this pin.")
            elif not any(
                "pytest.mark.serial_timing" in ast.unparse(dec) for dec in node.decorator_list
            ):
                problems.append(f"{rel}: {name} lost its serial_timing marker")
    assert not problems, "serial_timing discriminators drifted:\n  " + "\n  ".join(problems)


def test_build_lanes_pair_every_serial_timing_exclusion_with_a_leg() -> None:
    make_gaps = makefile_serial_lane_gaps((_REPO / "Makefile").read_text())
    nox_gaps = noxfile_serial_lane_gaps((_REPO / "noxfile.py").read_text())
    assert not make_gaps + nox_gaps, (
        "parallel lane(s) exclude serial_timing without re-appending it in a -n0 "
        "leg — the discriminators would silently stop running in those lanes:\n  "
        + "\n  ".join(make_gaps + nox_gaps)
    )


def test_serial_lane_scanners_flag_a_missing_leg() -> None:
    """Positive controls: both scanners observed red on the silent-drop shape."""
    bad_make = 'cov:\n\t@uv run pytest -m "not stability and not serial_timing"\n'
    good_make = (
        bad_make + '\t@uv run pytest -m "serial_timing and not stability" -n0 --cov-append\n'
    )
    say_only = bad_make + '\t@$(SAY) "serial_timing discriminators, -n0"\n'
    unrelated = 'cov:\n\t@uv run pytest -m "not stability"\n'
    assert makefile_serial_lane_gaps(bad_make) == [
        "cov: excludes serial_timing without a paired -n0 leg"
    ]
    assert makefile_serial_lane_gaps(good_make) == []
    assert makefile_serial_lane_gaps(say_only) != []  # a SAY line must never green the pin
    assert makefile_serial_lane_gaps(unrelated) == []
    # fable C1 (this wave's review): a tab-commented leg is a recipe line the
    # shell no-ops — textually present, never runs. Both comment spellings
    # must stay red, and a commented-out EXCLUSION must not trigger at all.
    commented_leg = bad_make + '\t# @uv run pytest -m "serial_timing and not stability" -n0\n'
    silenced_leg = bad_make + '\t@# uv run pytest -m "serial_timing and not stability" -n0\n'
    commented_exclusion = 'cov:\n\t# @uv run pytest -m "not stability and not serial_timing"\n'
    assert makefile_serial_lane_gaps(commented_leg) != []
    assert makefile_serial_lane_gaps(silenced_leg) != []
    assert makefile_serial_lane_gaps(commented_exclusion) == []

    bad_nox = (
        "def tests_x(session):\n"
        '    session.run("pytest", "-m", "not stability and not serial_timing")\n'
    )
    good_nox = (
        bad_nox + '    session.run("pytest", "-m", "serial_timing and not stability", "-n0")\n'
    )
    assert noxfile_serial_lane_gaps(bad_nox) == [
        "tests_x: excludes serial_timing without a paired -n0 leg"
    ]
    assert noxfile_serial_lane_gaps(good_nox) == []
    comment_only = "# a comment saying not serial_timing\n"
    assert noxfile_serial_lane_gaps(comment_only) == []
    # fable B2: with a statement AFTER it, a commented-out leg stays inside
    # the def's AST span — without the comment strip this shape false-greened.
    nox_commented_leg = (
        bad_nox
        + '    # session.run("pytest", "-m", "serial_timing and not stability", "-n0")\n'
        + '    session.run("pytest")\n'
    )
    assert noxfile_serial_lane_gaps(nox_commented_leg) == [
        "tests_x: excludes serial_timing without a paired -n0 leg"
    ]


def test_serial_lane_scanner_covers_the_hostless_constant_pair() -> None:
    """The module-level constant shape is checked end-to-end, not waved past."""
    pair = (
        'HOSTLESS_UNIT_TEST_ARGS = ("-m", "not stability and not serial_timing")\n'
        'HOSTLESS_UNIT_SERIAL_ARGS = ("-m", "serial_timing and not stability", "-n0")\n'
        "def tests_hostless_unit(session):\n"
        '    session.run("pytest", *HOSTLESS_UNIT_TEST_ARGS)\n'
        '    session.run("pytest", *HOSTLESS_UNIT_SERIAL_ARGS)\n'
    )
    assert noxfile_serial_lane_gaps(pair) == []
    no_serial_const = 'HOSTLESS_UNIT_TEST_ARGS = ("-m", "not stability and not serial_timing")\n'
    assert noxfile_serial_lane_gaps(no_serial_const) == [
        "HOSTLESS_UNIT_TEST_ARGS excludes serial_timing but HOSTLESS_UNIT_SERIAL_ARGS is gone"
    ]
    unused_pair = pair.replace('    session.run("pytest", *HOSTLESS_UNIT_SERIAL_ARGS)\n', "")
    assert noxfile_serial_lane_gaps(unused_pair) == [
        "no session runs both HOSTLESS_UNIT_TEST_ARGS and HOSTLESS_UNIT_SERIAL_ARGS"
    ]
    stray = 'STRAY_ARGS = ("-m", "not stability and not serial_timing")\n'
    (gap,) = noxfile_serial_lane_gaps(stray)
    assert "extend this pin" in gap  # novel module-level shapes are loud, never green
    # The interior-Python pair is a second sanctioned shape, judged by the
    # same rules: its twin must exist, keep -n0, and share a session.
    middle = pair.replace("HOSTLESS_UNIT_", "HOSTLESS_MIDDLE_").replace(
        "tests_hostless_unit", "middle"
    )
    assert noxfile_serial_lane_gaps(pair + middle) == []
    assert noxfile_serial_lane_gaps(
        pair + middle.replace('    session.run("pytest", *HOSTLESS_MIDDLE_SERIAL_ARGS)\n', "")
    ) == ["no session runs both HOSTLESS_MIDDLE_TEST_ARGS and HOSTLESS_MIDDLE_SERIAL_ARGS"]


class _MarkedItem:
    nodeid = "synthetic::serial_timing_guard_probe"

    @staticmethod
    def get_closest_marker(name: str) -> object | None:
        return object() if name == "serial_timing" else None


def test_root_conftest_refuses_serial_timing_inside_an_xdist_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control for the runtime backstop: marked test + worker env = loud fail.

    The scanners above cover the lanes we know; this guard covers the lane
    someone adds next year. Exercised directly against the loaded root
    conftest module — the precondition assert keeps this from silently
    double-importing it (module-level machinery: loop tracker, atexit) if
    pytest ever changes the module name it loads conftest under.
    """
    assert "tests.conftest" in sys.modules, "root conftest not loaded as tests.conftest"
    import tests.conftest as root_conftest

    # The hook records item.nodeid in a module global used for leaked-loop
    # attribution; snapshot it so the synthetic nodeid never leaks out.
    monkeypatch.setattr(root_conftest, "_current_test", root_conftest._current_test)
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw7")
    with pytest.raises(pytest.fail.Exception, match="xdist worker"):
        root_conftest.pytest_runtest_setup(_MarkedItem())
    monkeypatch.delenv("PYTEST_XDIST_WORKER")
    root_conftest.pytest_runtest_setup(_MarkedItem())  # the -n0 shape must pass


# ── ast-grep rule tests: Makefile <-> noxfile parity ────────────────────────
# sgconfig.yml's `testConfigs` entry makes `.ast-grep/rule-tests/` discoverable;
# only `ast-grep test` RUNS it. `ast-grep scan` — the leg both build files have
# always had — walks source and never opens a test file, so a rule quietly
# losing its own motivating match is invisible until someone runs the other
# subcommand. Both files therefore need the leg, and both say so only in prose
# ("keep both in sync"). These two pins are that sentence, enforced.
_RULE_TEST_ARGV = ("ast-grep", "test")


def noxfile_session_commands(text: str, session: str) -> list[list[str]]:
    """The literal argv of every ``session.run(...)`` inside *session*.

    Only ``session.run`` calls count, and only their CONSTANT string
    arguments: a docstring that names the command (this repository's ``lint``
    session has one) and a comment are text, not a leg.
    """
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == session:
            return [
                [arg.value for arg in call.args if isinstance(arg, ast.Constant)]
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "run"
            ]
    return []


def ast_grep_rule_test_gaps(make_text: str, nox_text: str) -> list[str]:
    """Which of the two build files fails to RUN the ast-grep rule tests."""
    gaps: list[str] = []
    joined = " ".join(makefile_live_recipe(make_text, "lint-arch"))
    if " ".join(_RULE_TEST_ARGV) not in joined:
        gaps.append("Makefile lint-arch: no live `ast-grep test` recipe line")
    if not any(
        argv[: len(_RULE_TEST_ARGV)] == list(_RULE_TEST_ARGV)
        for argv in noxfile_session_commands(nox_text, "lint")
    ):
        gaps.append('noxfile.py lint session: no `session.run("ast-grep", "test", ...)`')
    return gaps


def test_both_build_files_run_the_ast_grep_rule_tests() -> None:
    gaps = ast_grep_rule_test_gaps(
        (_REPO / "Makefile").read_text(), (_REPO / "noxfile.py").read_text()
    )
    assert not gaps, (
        "the architecture RULE TESTS (.ast-grep/rule-tests/) are not run by every "
        "lint surface — `ast-grep scan` does not execute them, so a rule that stops "
        "matching its own motivating snippet would fail nothing there:\n  " + "\n  ".join(gaps)
    )


def test_rule_test_parity_scanner_flags_a_missing_leg() -> None:
    """Positive controls: each half observed red, and neither greened by text."""
    good_make = (
        "lint-arch: check-breaking\n"
        '\t@$(SAY) "ast-grep: architecture pattern rules"\n'
        "\t@uv run --group lint ast-grep scan src/otto web/src tests\n"
        '\t@$(SAY) "ast-grep: rule tests"\n'
        "\t@uv run --group lint ast-grep test --skip-snapshot-tests\n"
    )
    good_nox = (
        "def lint(session):\n"
        '    """Runs ast-grep test on the rule tests."""\n'
        '    session.run("ast-grep", "scan", "src/otto")\n'
        '    session.run("ast-grep", "test", "--skip-snapshot-tests")\n'
    )
    assert ast_grep_rule_test_gaps(good_make, good_nox) == []
    # The leg gone from each side in turn, and gone in the two shapes that
    # leave its name behind: a SAY banner, and a tab-commented recipe line.
    no_make_leg = good_make.replace(
        "\t@uv run --group lint ast-grep test --skip-snapshot-tests\n", ""
    )
    assert ast_grep_rule_test_gaps(no_make_leg, good_nox) == [
        "Makefile lint-arch: no live `ast-grep test` recipe line"
    ]
    commented_make = good_make.replace(
        "\t@uv run --group lint ast-grep test", "\t# @uv run --group lint ast-grep test"
    )
    assert ast_grep_rule_test_gaps(commented_make, good_nox) != []
    # no_make_leg still carries its SAY banner naming the rule tests: a line
    # that only prints must never green the pin.
    assert "ast-grep: rule tests" in no_make_leg
    # A leg under a DIFFERENT target does not count as lint-arch having one.
    other_target = no_make_leg + "\nlint-ts:\n\t@uv run --group lint ast-grep test\n"
    assert ast_grep_rule_test_gaps(other_target, good_nox) != []
    # nox: the docstring names the command, and a bare expression is not a run.
    no_nox_leg = good_nox.replace(
        '    session.run("ast-grep", "test", "--skip-snapshot-tests")\n',
        '    print("ast-grep", "test")\n',
    )
    (gap,) = ast_grep_rule_test_gaps(good_make, no_nox_leg)
    assert "noxfile.py lint session" in gap


# ── lane-leg membership ─────────────────────────────────────────────────────
# The scanners above audit leg TEXT. This block audits leg MEMBERSHIP: does
# the leg select anything at all? Kept in one place because every piece of it
# — the inventory, the collection subprocess, the expression evaluator — is
# load-bearing for the one assertion in
# `test_every_lane_leg_selects_at_least_one_test`.

# The two surfaces that spell pytest lanes, the same set
# `test_no_lane_but_the_busybox_lane_can_select_the_busybox_tier` covers
# (`scripts/stability_campaign.py` delegates to them — pinned by
# `test_the_stability_campaign_spells_no_lane_of_its_own`). A
# lane written straight into a GitHub workflow step is NOT seen here; that is
# the SURFACE BOUND `test_a_lane_that_selects_by_path_cannot_reach_the_busybox_tier`
# states, and it names the live sightings.
_MAKEFILE = "Makefile"
_PYTHON_LANE_SURFACES = ("noxfile.py",)

# NOT `OTTO_`-prefixed, and that is not a style choice: the root conftest
# strips ambient `OTTO_*` variables at IMPORT time (all but a named opt-in
# list — the hermetic-env rule). An `OTTO_`-named dump path is therefore gone
# by the time the dump hook reads it: the child collects, then dies with
# KeyError in an INTERNALERROR. Measured, while writing this.
_DUMP_ENV = "LANE_LEG_MARKER_DUMP"
_DUMP_PLUGIN_MODULE = "_lane_leg_marker_dump"

# Loaded with `-p` into the collecting child. `pytest_collection_finish` runs
# after every `pytest_collection_modifyitems` hook, which is the only moment
# all four of this repository's marker routes have been applied (see the gate's
# docstring). One line per item, always terminated, so a run that collected
# nothing is an EMPTY file rather than a file holding one blank line.
_MARKER_DUMP_PLUGIN = r'''"""Dump every collected item's resolved marker names, one item per line.

Written to a temporary directory and loaded with ``-p`` by the lane-leg
membership gate in tests/unit/test_lane_invariants.py.
"""

import os


def pytest_collection_finish(session):
    with open(os.environ["LANE_LEG_MARKER_DUMP"], "w", encoding="utf-8") as handle:
        for item in session.items:
            names = ",".join(sorted({mark.name for mark in item.iter_markers()}))
            handle.write(item.nodeid + "\t" + names + "\n")
'''

# Only speed and hygiene, never membership: `-n0` so one process sees every
# item (and no `@group` nodeid suffixes), no coverage, no cache writes.
# pyproject's `addopts` is NOT cleared, and `--doctest-modules` is not added:
# since #566 no real lane passes it, so these children collect exactly what
# the lanes collect.
#
# `--collect-only` is also what keeps these children off the web build. Both
# browser conftests abort the session from `pytest_configure` when the React
# dist is missing or stale, and this gate passes NO `-m`, so nothing else
# would spare it — but `tests/_fixtures/_browser_guard.py` returns False for
# `config.option.collectonly` (that short-circuit IS the issue-#196 fix). A
# regression there does not corrupt this gate's answer: the child exits 1,
# which `_collect_marker_sets` reports with the child's own "run `make web`"
# message rather than reading as "this leg selects nothing".
_COLLECT_ARGS = ("--collect-only", "-q", "--no-cov", "-p", "no:cacheprovider", "-n0")

# Runaway guard, not a measurement: one collection is seconds, and this only
# exists so a wedged child is killed instead of inherited by the session.
_COLLECT_TIMEOUT_S = 900

# The budget of every test that takes `collect_marker_sets`. The fixture
# collects lazily, so its child runs inside the first such test's call
# phase, where `timeout_func_only` does not exclude it, and the pyproject
# default of 180 s is shorter than a collection of testpaths on a loaded
# nightly worker (#753). Derived from the child's own guard, so a wedged
# testpaths child is killed by that guard (with its output) before the test
# times out; a test that also spawns a fallback or dedicated child can run
# two, and then the test's own budget is the one that fires.
LANE_LEG_TIMEOUT_S = _COLLECT_TIMEOUT_S + 60


@dataclasses.dataclass(frozen=True)
class _Leg:
    """One lane leg: a marker expression AND the paths it is scoped to."""

    surface: str
    expr: str
    roots: "tuple[str, ...]"
    # Carries pytest-repeat's `--count`, which multiplies what `roots` COLLECT.
    repeated: bool = False

    def __str__(self) -> str:
        where = " ".join(self.roots) if self.roots else "<testpaths>"
        return f"{self.surface}: `-m {self.expr}` over {where}"


def _legs_from_invocations(
    surface: str,
    invocations: "list[list[str]]",
    variables: "dict[str, str] | None" = None,
) -> "list[_Leg]":
    """The `-m`-carrying invocations of one surface, paired with their paths.

    STATED BOUND: an invocation with no `-m` is not a leg here. Nothing
    deselects anything in one, so it is empty only when its own paths hold no
    tests — a path question, not the marker drift #229 was. Every such lane in
    the tree today is a doctest run over `src/otto` that CLEARS `addopts`
    first, so reproducing its collection would mean reproducing a different
    pytest configuration;
    `test_a_lane_that_selects_by_path_cannot_reach_the_busybox_tier` is the
    guard written over those lanes.

    A `-m` whose value the scanner could NOT recover becomes the empty
    expression, which :func:`_lane_leg_offenders` reports rather than skips.
    """
    legs: "list[_Leg]" = []
    for tokens in invocations:
        if "-m" not in tokens:
            continue
        value = tokens.index("-m") + 1
        expr = tokens[value] if value < len(tokens) else ""
        if variables is not None:
            expr = _expand_makefile_variables(expr, variables)
        parametric = (_PATHS_TOKEN,) if _PATHS_TOKEN in tokens else ()
        fixed = [token for token in tokens if token != _PATHS_TOKEN]
        roots = parametric + tuple(str(path.relative_to(_REPO)) for path in _selected_roots(fixed))
        repeated = any(token == "--count" or token.startswith("--count=") for token in tokens)
        legs.append(_Leg(surface, expr, roots, repeated))
    return legs


_PATHS_TOKEN = "$(PATHS)"
_PARAMETRIC_TARGETS = ("quick",)
"""The targets whose pytest roots are the caller's `PATHS` (pinned against the recipes)."""


def _is_parametric(leg: _Leg) -> bool:
    """Report whether `leg`'s roots come from the command line (`$(PATHS)`).

    Such a leg has no fixed roots to collect, so it is exempt from the nonempty-leg pin
    alone: its expression is still compiled there, and it is still paired with its serial
    twin. What it selects is the oracle census's question, over the roots `make gate-scope`
    hands it (tests/unit/scripts/test_gate_scope.py).
    """
    return _PATHS_TOKEN in leg.roots


def lane_legs() -> "list[_Leg]":
    """Every `-m` lane leg in the repository's lane-spelling surfaces."""
    makefile = (_REPO / _MAKEFILE).read_text()
    legs = _legs_from_invocations(
        _MAKEFILE,
        _makefile_pytest_invocations(makefile),
        _makefile_marker_variables(makefile),
    )
    for surface in _PYTHON_LANE_SURFACES:
        # `resolve_names=True`: nox's dashboard lane passes DASHBOARD_MARKER_EXPR
        # rather than a literal, and without resolution `-m` is followed by
        # `"--browser"` — which this gate would read as the marker expression.
        legs += _legs_from_invocations(
            surface, _python_pytest_invocations(_REPO / surface, resolve_names=True)
        )
    return legs


@dataclasses.dataclass(frozen=True)
class _Item:
    """One collected item: pytest's node id and the marker names it resolved."""

    nodeid: str
    markers: "frozenset[str]"

    @property
    def path(self) -> str:
        """The file the item was collected from, relative to the checkout root."""
        return self.nodeid.split("::", 1)[0]


def _collect_marker_sets(plugin_dir: Path, roots: "tuple[str, ...]") -> "list[_Item]":
    """Collect *roots* for real in a child pytest, and return each item's node id and markers.

    A child process, never an in-process ``Config``: the recorded defect is
    that an in-process pytest ``Config`` built inside this suite inherits
    otto's own ``addopts`` (``--cov``, ``-n auto``) and corrupts the xdist
    worker it is built in.
    """
    dump = plugin_dir / "markers.txt"
    dump.unlink(missing_ok=True)
    # PYTEST_* is dropped so the child is not steered by the parent's own run:
    # an ambient PYTEST_ADDOPTS can carry a `-m` of its own, which would filter
    # the very collection this gate measures.
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTEST_")}
    inherited = os.environ.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{plugin_dir}{os.pathsep}{inherited}" if inherited else str(plugin_dir)
    env[_DUMP_ENV] = str(dump)
    argv = [
        sys.executable,
        "-m",
        "pytest",
        *_COLLECT_ARGS,
        "-p",
        _DUMP_PLUGIN_MODULE,
        *roots,
    ]
    completed = subprocess.run(
        argv,
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=_COLLECT_TIMEOUT_S,
        check=False,  # exit 5 (collected nothing) is an ANSWER, not a crash
    )
    # 0 = collected, 5 = collected nothing (a real answer this gate reports).
    # Anything else means the child never finished collecting, and a missing
    # dump means it never reached collection_finish — both are loud here,
    # because a silent empty list would read as "this leg selects nothing".
    if completed.returncode not in (0, 5) or not dump.exists():
        raise AssertionError(
            f"collection child failed for {roots or '<testpaths>'} "
            f"(exit {completed.returncode}); the lane-leg gate cannot decide "
            f"membership without it.\nargv: {' '.join(argv)}\n"
            f"stdout tail:\n{completed.stdout[-2000:]}\nstderr tail:\n{completed.stderr[-2000:]}"
        )
    items: "list[_Item]" = []
    for line in dump.read_text(encoding="utf-8").splitlines():
        nodeid, _, names = line.partition("\t")
        items.append(_Item(nodeid, frozenset(name for name in names.split(",") if name)))
    return items


def _marker_set_collector(collect_child, reached: "tuple[str, ...]"):
    """A memoised ``collect(roots)`` that pays for ONE child collection of testpaths.

    Every lane root lies under testpaths (*reached*), so the items under
    *roots* are filtered out of that one collection (:func:`_within`) instead
    of being re-collected: one child per root set re-imported and re-collected
    `tests/unit` five times, which cost more than the default per-test
    timeout on a slower host (#753). The filter answers what a dedicated
    child would only because every conftest that edits markers at collection
    (`tests/e2e`, `tests/busybox`, `tests/conformance`, `tests/integration`
    and its `host` and `busybox_bed` subtrees) bounds itself to its own tree
    by path. The host conftest's grouping hook once also marked hostless
    `tests/unit` rows, so a full-tree run and a `tests/unit` run disagreed on
    7 items. :func:`test_the_filtered_collection_is_what_a_dedicated_child_collects`
    re-checks the agreement on `tests/unit`.

    A root set the filter cannot answer is collected by a child of its own,
    whole: one with a root outside *reached* (`tests/_fixtures`, or `tests`,
    which reaches the fixture projects testpaths leaves out), or a root with
    no item under it (a file pytest only collects when it is named).
    """
    collected: "dict[tuple[str, ...], list[_Item]]" = {}

    def collect(roots: "tuple[str, ...]") -> "list[_Item]":
        if () not in collected:
            collected[()] = collect_child(())
        if roots not in collected:
            within = [item for item in collected[()] if _within(item.path, roots)]
            answerable = all(
                any(_within(root, (path,)) for path in reached)
                and any(_within(item.path, (root,)) for item in within)
                for root in roots
            )
            collected[roots] = within if answerable else collect_child(roots)
        return collected[roots]

    return collect


def _write_marker_dump_plugin(plugin_dir: Path) -> None:
    """Write the plugin a collection child loads to dump each item's markers."""
    (plugin_dir / f"{_DUMP_PLUGIN_MODULE}.py").write_text(_MARKER_DUMP_PLUGIN, encoding="utf-8")


@pytest.fixture(scope="session")
def collect_marker_sets(tmp_path_factory: pytest.TempPathFactory):
    """Memoised real collection, keyed by path set (:func:`_marker_set_collector`).

    The collection runs lazily, inside the first test that asks, so it counts
    against that test's timeout: every consumer carries
    ``@pytest.mark.timeout(LANE_LEG_TIMEOUT_S)``. The consumers share one
    ``xdist_group`` so a distributed run pays for one worker's collection,
    not several workers' duplicates.
    """
    plugin_dir = tmp_path_factory.mktemp("lane_leg_collect")
    _write_marker_dump_plugin(plugin_dir)
    return _marker_set_collector(
        lambda roots: _collect_marker_sets(plugin_dir, roots), tuple(pyproject_testpaths(_REPO))
    )


def test_every_lane_root_is_answered_by_one_child_collection() -> None:
    """The memo spawns ONE child for every root set the legs name (#753), answers
    each with exactly the items under it, and collects a set for real when a root
    lies outside testpaths or holds nothing the one collection saw."""
    children: "list[tuple[str, ...]]" = []
    roots = {leg.roots for leg in lane_legs() if not _is_parametric(leg)}
    assert len(roots) > 1, "the legs must name several root sets, or this pins nothing"
    paths = {path for root in roots for path in root}
    reached = ("tests/unit", "tests/integration", "tests/e2e", "tests/elsewhere")
    sentinels = ("tests/elsewhere", "tests/_fixtures", "tests/unit/test_named_only.py")
    assert not paths & set(sentinels), "a lane names a sentinel root; pick another one"
    assert all(any(_within(p, (r,)) for r in reached) for p in paths), "a lane root moved"
    everything = [
        _Item(f"{p}::t" if p.endswith(".py") else f"{p}/test_x.py::t", frozenset())
        for p in sorted(paths | {"tests/elsewhere"})
    ]

    def child(asked: "tuple[str, ...]") -> "list[_Item]":
        children.append(asked)
        return everything if not asked else [_Item(f"{asked[0]}::own", frozenset())]

    collect = _marker_set_collector(child, reached)
    for root in sorted(roots - {()}):
        expected = [item for item in everything if _within(item.path, root)]
        assert expected, f"the fake tree must hold items under {root}"
        assert collect(root) == expected, root
    assert collect(()) == everything
    # One answer spelled out rather than derived, so `_within` is not judging itself.
    file_roots = next(root for root in roots if "tests/unit/host/test_unix_host.py" in root)
    assert "tests/unit/host/test_unix_host.py::t" in {item.nodeid for item in collect(file_roots)}
    assert "tests/unit/test_x.py::t" not in {item.nodeid for item in collect(file_roots)}
    assert children == [()]

    for unanswerable in (
        ("tests/elsewhere", "tests/_fixtures"),  # a root outside testpaths
        ("tests",),  # a root wider than testpaths
        ("tests/elsewhere", "tests/unit/test_named_only.py"),  # a root nothing reached
    ):
        assert collect(unanswerable) == [_Item(f"{unanswerable[0]}::own", frozenset())]
        assert children[-1] == unanswerable, "the whole set is collected for real"
    assert len(children) == 4


@pytest.mark.timeout(LANE_LEG_TIMEOUT_S)
@pytest.mark.xdist_group("lane_leg_membership")
def test_the_filtered_collection_is_what_a_dedicated_child_collects(
    collect_marker_sets, tmp_path: Path
) -> None:
    """The filter is only as true as the conftests' path bounds: a hook that marks
    items outside its own tree changes what a full-tree run sees, not what a
    `tests/unit` lane sees. One dedicated child for the largest lane root re-checks
    that both answers agree, item for item and marker for marker (#753)."""
    _write_marker_dump_plugin(tmp_path)
    dedicated = _collect_marker_sets(tmp_path, ("tests/unit",))
    filtered = collect_marker_sets(("tests/unit",))
    assert dedicated, "the dedicated child collected nothing, so this compares nothing"
    differing = sorted(set(dedicated) ^ set(filtered), key=lambda item: item.nodeid)
    assert differing == [], (
        f"{len(differing)} item(s) differ between a `tests/unit` collection and the "
        "full-tree one; a conftest hook is marking items outside its own tree:\n  "
        + "\n  ".join(f"{item.nodeid} {sorted(item.markers)}" for item in differing[:20])
    )


def test_every_collection_consumer_carries_the_lane_leg_budget() -> None:
    """A test that takes `collect_marker_sets` may be the one that pays for the
    collection, so each carries `LANE_LEG_TIMEOUT_S`, never the 180 s default (#753)."""
    expected = "pytest.mark.timeout(LANE_LEG_TIMEOUT_S)"
    consumers: "list[str]" = []
    offenders: "list[str]" = []
    for path in sorted((_REPO / "tests").rglob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        if "collect_marker_sets" not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if not node.name.startswith("test_"):
                continue
            if "collect_marker_sets" not in {arg.arg for arg in node.args.args}:
                continue
            name = f"{path.relative_to(_REPO)}::{node.name}"
            consumers.append(name)
            if expected not in {ast.unparse(dec) for dec in node.decorator_list}:
                offenders.append(name)
    assert len(consumers) > 1, "the scan must find the consumers, or this pins nothing"
    assert offenders == [], f"missing `@{expected}`:\n  " + "\n  ".join(offenders)


def _lane_leg_offenders(legs: "list[_Leg]", collect) -> "list[str]":
    """The legs that select nothing, plus the ones this gate cannot judge.

    Unparseable and unevaluatable expressions are offenders, not skips. A
    scanner that silently passes what it cannot read is worse than no gate:
    it reports green for the one shape nobody has checked.
    """
    offenders: "list[str]" = []
    for leg in legs:
        if not leg.expr.strip() or leg.expr.startswith("-"):
            offenders.append(f"{leg} — no usable `-m` value (the scanner mis-paired the flag?)")
            continue
        try:
            compiled = Expression.compile(leg.expr)
        except SyntaxError as exc:
            offenders.append(f"{leg} — pytest cannot compile this expression: {exc}")
            continue
        if _is_parametric(leg):
            continue
        collected = collect(leg.roots)
        if not collected:
            offenders.append(f"{leg} — those paths collect no tests at all")
            continue
        try:
            hits = sum(1 for item in collected if compiled.evaluate(_marker_matcher(item.markers)))
        except pytest.UsageError as exc:
            offenders.append(f"{leg} — this gate cannot evaluate the expression: {exc}")
            continue
        if not hits:
            offenders.append(f"{leg} — selects 0 of the {len(collected)} tests collected there")
    return offenders


@pytest.mark.timeout(LANE_LEG_TIMEOUT_S)
@pytest.mark.xdist_group("lane_leg_membership")
def test_every_lane_leg_selects_at_least_one_test(collect_marker_sets) -> None:
    """An empty lane leg is a red suite, not a quiet one — issue #229.

    pytest exits 5 when it collects nothing and make aborts on a non-zero
    recipe line, so a leg whose `-m` expression selects nothing does not
    silently do less work: it fails the lane, AFTER paying for every leg ahead
    of it in the recipe.

    Issue #229 is the incident. Its fix dropped `@pytest.mark.serial_timing`
    from the last two tests that carried both it and `concurrency`, emptying
    the second leg of `make stability-unit` (`-m "serial_timing and
    concurrency"`). Nothing in the tree could see that: `make stability-unit`
    is what nightly's Python-matrix soak job runs, so every leg of that matrix
    would have gone red after paying for the full soak, and it was caught by
    hand instead. `make dashboard-soak`'s recipe comment records the identical
    hazard in prose ("the non-serial leg would select nothing and pytest would
    exit 5"), also unguarded. `makefile_serial_lane_gaps` above audits leg
    TEXT — "does a recipe excluding serial_timing also carry a paired -n0
    leg?" — and never leg MEMBERSHIP; this is the other half.

    MEMBERSHIP COMES FROM REAL COLLECTION, and that is the whole design.
    Markers reach a test by at least four routes here: a module `pytestmark`,
    a function or class decorator, a directory conftest's `item.add_marker`,
    and param-level `marks=` inside a `pytest.param` a helper RETURNS
    (`_backend_param` in tests/integration/host/test_host_stability_contract.py,
    over the backend list tests/conftest.py exports). The last route is the one
    that bites, because those marks exist only once the module has been
    executed. Measured, not predicted: an AST scan attributing module
    `pytestmark` + class/function decorators + directory stamps to each test
    function reported ZERO tests for `make stability-embedded` (`-m "stability
    and embedded and not chaos"`) — a live, correct lane, which real collection
    answered with fifteen when this landed. So the gate shells out, per path set,
    and evaluates every expression in that group against the markers pytest
    itself resolved.

    Marker-expression semantics are pytest's own compiler
    (`_pytest.mark.expression.Expression`), not a hand-rolled conjunction
    reader — `or` and parentheses are legal in a `-m` and a partial reader
    would have to guess at them. That is private API, pinned by
    `test_the_marker_expression_compiler_is_pytests_own` so an upgrade fails
    here loudly instead of quietly changing what this gate means.

    A LEG IS (EXPRESSION, PATHS), never an expression alone, because emptiness
    is a property of both. Today no leg's answer differs between the two
    readings — but they already differ in COUNT, and only by amount: `make
    coverage-unit`'s serial leg (`-m "serial_timing and not stability"`) is
    scoped to `tests/unit`, while serial_timing tests also live under
    `tests/integration`, so a tree-wide reading would keep calling that leg
    non-empty after the last of its own tests was unmarked. That is issue #229
    exactly, one directory over.

    If this ever reddens, the fix is a scope call and not this gate's: either
    the lane should select something else, or the tests it named should still
    exist. Do not add an allowlist.
    """
    offenders = _lane_leg_offenders(lane_legs(), collect_marker_sets)
    assert not offenders, (
        "these lane legs select no tests, so each exits 5 and aborts its make "
        "recipe (issue #229) — decide whether the lane or the markers are "
        "wrong, and never silence this with an allowlist:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.timeout(LANE_LEG_TIMEOUT_S)
@pytest.mark.xdist_group("lane_leg_membership")
def test_the_lane_leg_gate_reddens_on_the_issue_229_leg(collect_marker_sets) -> None:
    """Positive control: the incident itself, replayed against today's tree.

    `serial_timing and concurrency` is the exact selector #229's fix emptied.
    It is fed through the same `_lane_leg_offenders` the gate above calls, so
    the control proves the code path that actually runs — not a
    reimplementation of it. It reddens against the CURRENT tree, which is the
    point: this is the observation a gate would have made the day #229's fix
    landed, and it goes on being made on every run.

    Also covers the shapes the gate must not wave through — an expression
    pytest cannot compile, one it can compile but this gate cannot evaluate
    (the `marker(kwarg=...)` form), and a `-m` whose value the scanner never
    recovered — and, at the end, the complement: a live selector must NOT be
    reported, or a gate that condemned everything would satisfy all of the
    above.
    """
    empty = _Leg("synthetic (issue #229)", "serial_timing and concurrency", ())
    unparseable = _Leg("synthetic", "serial_timing and (", ())
    unmodelled = _Leg("synthetic", "concurrency(count=1)", ())
    no_value = _Leg("synthetic", "", ())

    reports = _lane_leg_offenders([empty, unparseable, unmodelled, no_value], collect_marker_sets)
    assert len(reports) == 4, f"every synthetic offender must be reported: {reports}"
    assert "selects 0 of the" in reports[0], reports[0]
    assert "cannot compile" in reports[1], reports[1]
    assert "cannot evaluate" in reports[2], reports[2]
    assert "no usable `-m` value" in reports[3], reports[3]

    # And the complement: a selector that IS live must not be reported, or the
    # control above would pass on a gate that condemns everything.
    live = _Leg("synthetic", "serial_timing and not stability", ())
    assert _lane_leg_offenders([live], collect_marker_sets) == []


def test_the_quick_legs_are_parametric_not_testpaths_wide() -> None:
    """`make quick`'s two legs take their roots from the command line: the inventory reads
    them as parametric (roots `$(PATHS)`), never as legs over `<testpaths>`."""
    makefile = (_REPO / _MAKEFILE).read_text()
    m_quick = _makefile_marker_variables(makefile)["M_QUICK"]
    parametric = [leg for leg in lane_legs() if _is_parametric(leg)]
    assert [(leg.expr, leg.roots) for leg in parametric] == [
        (f"{m_quick} and not serial_timing", (_PATHS_TOKEN,)),
        (f"serial_timing and {m_quick}", (_PATHS_TOKEN,)),
    ]


def test_the_parametric_targets_are_exactly_the_recipes_that_read_paths() -> None:
    makefile = (_REPO / _MAKEFILE).read_text()
    targets = {match.group(1) for match in _RULE_LINE.finditer(makefile)}
    reading = sorted(
        target
        for target in targets
        if any(
            _PATHS_TOKEN in tokens
            for tokens in _makefile_pytest_invocations(
                "\n".join(makefile_live_recipe(makefile, target))
            )
        )
    )
    assert reading == sorted(_PARAMETRIC_TARGETS)


def test_an_unexpanded_variable_where_a_path_goes_is_refused() -> None:
    """A positional `$(NAME)` the reader cannot expand would silently become `<testpaths>`."""
    with pytest.raises(AssertionError, match=r"\$\(ROOTS\)"):
        _selected_roots(["uv", "run", "pytest", "$(ROOTS)", "-m", "unit"])
    for token in ("$(ROOT)/sub", "${ROOT}", "$$X", "tests/$(PKG)"):
        recipe = f'leg:\n\tuv run pytest {token} -m "unit"\n'
        (tokens,) = _makefile_pytest_invocations(recipe)
        with pytest.raises(AssertionError, match="unexpanded"):
            _selected_roots(tokens)
    called = ["uv", "run", "pytest", "tests/unit", "$(call", "junitxml,x)", "-m", "unit"]
    assert _selected_roots(called) == [_REPO / "tests/unit"], "a make function is no root"
    assert _selected_roots(["uv", "run", "pytest", "tests/unit", "||", "$(MAKE)"]) == [
        _REPO / "tests/unit"
    ], "a shell command after the pytest argv is not one of its roots"


@pytest.mark.timeout(LANE_LEG_TIMEOUT_S)
@pytest.mark.xdist_group("lane_leg_membership")
def test_a_fixed_lane_with_an_empty_root_still_fails_the_pin(collect_marker_sets) -> None:
    """Only the parametric legs are exempt from the nonempty pin: a fixed lane over a root
    that holds no test is reported, while the parametric leg beside it is still compiled."""
    synthetic = (
        'empty:\n\tuv run pytest tests/_fixtures -m "not stability"\n'
        'quick:\n\tuv run pytest $(PATHS) -m "not stability and ("\n'
    )
    legs = _legs_from_invocations(_MAKEFILE, _makefile_pytest_invocations(synthetic), {})
    assert [leg.roots for leg in legs] == [("tests/_fixtures",), (_PATHS_TOKEN,)]
    reports = _lane_leg_offenders(legs, collect_marker_sets)
    assert len(reports) == 2, reports
    assert "tests/_fixtures" in reports[0], reports[0]
    assert "collect no tests" in reports[0], reports[0]
    assert "cannot compile" in reports[1], "the parametric leg's expression is still judged"


def _count_leg_offenders(legs: "list[_Leg]", collect) -> "list[str]":
    """The `--count` legs that multiply more than they soak, or soak less than they say.

    Two rules, one per failure direction:

    * A `--count` leg must NAME its roots. pytest-repeat multiplies every
      collected item before `-m` deselects anything, so a path-less leg pays
      COUNT x the whole tree to run its slice.
    * A leg whose roots are FILES is an enumeration standing in for its marker,
      so those files must hold every test the expression selects tree-wide —
      otherwise marking a new test enrolls it nowhere, silently. A leg whose
      roots are DIRECTORIES is scoped by intent (`tests_unit_repeat` soaks
      `tests/unit` under a broad `-m`), and the tree-wide selection says
      nothing about what it should hold.
    """
    offenders: "list[str]" = []
    for leg in legs:
        if not leg.repeated:
            continue
        if not leg.roots:
            offenders.append(
                f"{leg} — a `--count` leg with no paths multiplies the whole collected "
                f"tree before `-m` deselects (issue #429)"
            )
            continue
        if not all((_REPO / root).is_file() for root in leg.roots):
            continue
        compiled = Expression.compile(leg.expr)
        tree_wide = sum(
            1 for item in collect(()) if compiled.evaluate(_marker_matcher(item.markers))
        )
        named = sum(
            1 for item in collect(leg.roots) if compiled.evaluate(_marker_matcher(item.markers))
        )
        if named != tree_wide:
            offenders.append(
                f"{leg} — its files hold {named} of the {tree_wide} tests `-m {leg.expr}` "
                f"selects tree-wide; add the missing tests' files to the leg"
            )
    return offenders


@pytest.mark.timeout(LANE_LEG_TIMEOUT_S)
@pytest.mark.xdist_group("lane_leg_membership")
def test_every_count_leg_names_the_files_holding_its_selection(collect_marker_sets) -> None:
    """A `--count` soak names its paths, and a file list misses no marked test — issue #429.

    The nightly `make stability-unit COUNT=100` ran `-m concurrency` with no
    path: 1.24M items collected per xdist worker to run 1,700, ~4.3 GB each on
    a 16 GB runner. Collection took 12m40s, and the swapped heap stalled
    Hypothesis's first `gc.collect()` for minutes on some workers — the
    "runner freeze" timeouts #408 raised a threshold for. Naming the files
    brings the same collection to ~1 s and ~145 MB, but a file list can go
    stale where a marker could not, hence the second rule in
    `_count_leg_offenders`.
    """
    offenders = _count_leg_offenders(lane_legs(), collect_marker_sets)
    assert not offenders, (
        "these `--count` legs either multiply the whole tree or miss tests their "
        "marker selects:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.timeout(LANE_LEG_TIMEOUT_S)
@pytest.mark.xdist_group("lane_leg_membership")
def test_the_count_leg_gate_reddens_on_both_rules(collect_marker_sets) -> None:
    """Positive control: the issue #429 leg itself, a file list short one file, and a pass.

    The legs are synthetic but the tree is real, as in the #229 control above:
    `test_collector_tunnel_soak.py` holds `concurrency` tests today, so a list
    without it must be reported. The inventory half pins that the real
    `stability-unit` leg is still seen as repeated — a scanner that stopped
    reading `--count` would green the gate by judging nothing.
    """
    files = (
        "tests/unit/host/test_app_shell_concurrency.py",
        "tests/unit/host/test_session_concurrency.py",
        "tests/unit/host/test_unix_host.py",
    )
    missing_one = _Leg("synthetic", "concurrency", files, repeated=True)
    pathless = _Leg("synthetic (issue #429)", "concurrency", (), repeated=True)
    complete = _Leg(
        "synthetic",
        "concurrency",
        (*files, "tests/unit/monitor/test_collector_tunnel_soak.py"),
        repeated=True,
    )
    directory = _Leg("synthetic", "concurrency", ("tests/unit/host",), repeated=True)

    reports = _count_leg_offenders([pathless, missing_one], collect_marker_sets)
    assert len(reports) == 2, reports
    assert "no paths" in reports[0], reports[0]
    assert "add the missing tests' files" in reports[1], reports[1]
    assert _count_leg_offenders([complete, directory], collect_marker_sets) == []

    stability_unit = [
        leg for leg in lane_legs() if leg.surface == _MAKEFILE and leg.expr == "concurrency"
    ]
    assert stability_unit, "the scanner no longer finds `make stability-unit`'s leg"
    assert all(leg.repeated for leg in stability_unit), stability_unit


def test_the_lane_leg_inventory_sees_every_surface() -> None:
    """The scan finds legs on every surface, and recovers each `-m` value.

    A membership gate whose inventory silently empties is green forever, so
    every surface must contribute. The `-m` value check is the specific
    scanner defect this gate hit while being written: `_python_pytest_invocations`
    drops non-literal arguments by design, which left nox's dashboard lane
    spelling `-m` immediately followed by `"--browser"` — a token this gate
    would have compiled and evaluated as a marker expression. Hence
    `resolve_names=True`, and hence this pin on its result.

    The last assertion is about the dump plugin rather than the inventory, and
    lives here because it is the other way this family goes quiet: the plugin
    source and the runner name the dump path independently, and if they ever
    disagree the child dies in its collection hook and no leg is ever judged.
    """
    legs = lane_legs()
    for surface in (_MAKEFILE, *_PYTHON_LANE_SURFACES):
        assert [leg for leg in legs if leg.surface == surface], (
            f"no `-m` lane leg found in {surface} — the inventory stopped seeing "
            f"a whole surface, which greens this family without proving anything"
        )

    unusable = [str(leg) for leg in legs if not leg.expr.strip() or leg.expr.startswith("-")]
    assert not unusable, (
        f"the scanner paired `-m` with something that is not a marker expression: "
        f"{unusable}. A dropped non-literal value shifts the pairing by one token."
    )

    dashboard = _Leg(
        "noxfile.py",
        "browser and not soak",
        ("tests/e2e/monitor/dashboard", "tests/e2e/cov/report_browser"),
    )
    assert dashboard in legs, (
        f"nox's dashboard leg no longer resolves to its DASHBOARD_MARKER_EXPR "
        f"constant and its two paths — name resolution or the path scan "
        f"regressed: {[str(leg) for leg in legs if leg.surface == 'noxfile.py']}"
    )
    assert _DUMP_ENV in _MARKER_DUMP_PLUGIN, (
        "the dump plugin reads a different env var than the runner sets — the "
        "child would raise KeyError and every collection would fail"
    )


def test_the_marker_expression_compiler_is_pytests_own() -> None:
    """Pin the private pytest API this gate's semantics are borrowed from.

    `_pytest.mark.expression.Expression` is not public. Borrowing it is still
    right — reimplementing `-m` semantics means reimplementing `or`,
    parentheses and precedence, and a partial reader would have to either
    guess or silently skip. What is not acceptable is the borrowing changing
    meaning under an upgrade without anyone noticing, so the behaviours the
    gate depends on are asserted directly: conjunction, negation, disjunction,
    parenthesised grouping, a loud failure on malformed input, and a matcher
    that refuses the kwargs form.
    """
    carried = _marker_matcher(frozenset({"serial_timing", "concurrency"}))
    assert Expression.compile("serial_timing and concurrency").evaluate(carried)
    assert not Expression.compile("serial_timing and not concurrency").evaluate(carried)
    assert Expression.compile("stability or concurrency").evaluate(carried)
    assert not Expression.compile("stability and (concurrency or embedded)").evaluate(carried)
    assert Expression.compile("serial_timing and (concurrency or embedded)").evaluate(carried)
    assert not Expression.compile("embedded").evaluate(carried)

    with pytest.raises(SyntaxError):
        Expression.compile("serial_timing and (")
    with pytest.raises(pytest.UsageError, match="kwargs form"):
        Expression.compile("concurrency(count=1)").evaluate(carried)


# ─── a serial leg re-appends its parallel twin's exclusions ─────────────────
#
# The serial_timing family above holds one direction of the pair: every
# parallel leg that excludes the marker has a `-n0` twin that re-appends it.
# What it never asked is whether the twin keeps the REST of the expression.
# It did not, eight times: every hand-spelled serial leg whose parallel half
# carried `not busybox and not conformance` had dropped them, so the day a
# busybox- or conformance-marked test gains
# `serial_timing` (a banner-timing assertion, say) the serial leg fetches
# artifacts and stands up a loopback sshd inside a lane whose parallel half
# was deliberately excluding both — and the two legs of ONE lane would
# disagree about what the lane is. The one serial leg built from a Makefile
# variable that carries those clauses (`serial_timing and $(M_HOSTLESS)`)
# never drifted, which is the tell: restated text drifts, derived text does
# not. Latent when pinned (no such test
# exists — `-m "serial_timing and (busybox or conformance)"` collects
# nothing anywhere), and pinned before it is live.
#
# The rule is symmetric on purpose: the serial leg's clauses, minus its
# positive `serial_timing`, must EQUAL the parallel leg's, minus its
# `not serial_timing`. "Missing" is the drift above; "extra" would be a
# serial leg excluding something its twin runs, i.e. a test that runs in
# neither leg of a lane that claims to run it.


_MARKER_NAME = r"(?!not\b)[A-Za-z_][A-Za-z0-9_]*"
_DISJUNCTION_GROUP = re.compile(rf"\(\s*({_MARKER_NAME}(?:\s+or\s+{_MARKER_NAME})+)\s*\)")
"""A parenthesised disjunction of bare markers, `(a or b …)`: the grouped shape a leg carries."""


def _conjunction_clauses(expr: str) -> "frozenset[str]":
    """The clauses of a conjunction: `[not] marker`, or `(a or b …)` groups; loud otherwise.

    Every paired leg in the tree is an `and` chain whose clauses are bare
    markers, except the bed half of the coverage selection (`coverage-bed`,
    #797), whose first clause is `(integration or embedded)`. That group is
    one clause, compared as its sorted alternatives. Any other `or` or
    parenthesis needs modelling, not a scanner that quietly compares the wrong
    thing, so that shape is a refusal rather than a skip.
    """
    groups = [
        "(" + " or ".join(sorted(t for t in m.group(1).split() if t != "or")) + ")"
        for m in _DISJUNCTION_GROUP.finditer(expr)
    ]
    rest = _DISJUNCTION_GROUP.sub("\x00", expr)
    if "(" in rest or ")" in rest or " or " in f" {rest} ":
        raise pytest.UsageError(
            f"not a conjunction of markers and `(a or b)` groups — teach this pin the shape "
            f"rather than dropping the pair: {expr!r}"
        )
    clauses = []
    for raw in rest.split(" and "):
        clause = " ".join(raw.split())
        clauses.append(groups.pop(0) if clause == "\x00" else clause)
    malformed = [c for c in clauses if not re.fullmatch(r"(not )?[A-Za-z_][A-Za-z0-9_]*|\(.*\)", c)]
    if malformed:
        raise pytest.UsageError(f"unreadable clause(s) {malformed} in {expr!r}")
    return frozenset(clauses)


class TestConjunctionClauses:
    def test_a_grouped_disjunction_is_one_clause_compared_by_its_alternatives(self) -> None:
        assert _conjunction_clauses("(integration or embedded) and not stability") == {
            "(embedded or integration)",
            "not stability",
        }
        assert _conjunction_clauses("(embedded or integration) and not stability") == (
            _conjunction_clauses("(integration or embedded) and not stability")
        )

    def test_a_twin_whose_group_differs_is_an_offender(self) -> None:
        """Through the pairing itself: a serial twin whose group differs is named, and a
        twin spelling the same group in another order is faithful."""
        parallel = _Leg(
            "x", "(integration or embedded) and not stability and not serial_timing", ()
        )
        unfaithful = _Leg("x", "serial_timing and (integration or hostless) and not stability", ())
        pairs, offenders = serial_twin_offenders([parallel, unfaithful])
        assert pairs == 1
        assert len(offenders) == 1
        assert "(embedded or integration)" in offenders[0]
        assert "(hostless or integration)" in offenders[0]
        reordered = _Leg("x", "serial_timing and (embedded or integration) and not stability", ())
        assert serial_twin_offenders([parallel, reordered]) == (1, [])

    @pytest.mark.parametrize(
        "expr",
        [
            "((integration or embedded)) and not stability",
            "integration or embedded",
            "(integration and embedded) and not stability",
            "(not integration or embedded) and not stability",
            "(integration or embedded and not stability)",
            "not (integration or embedded) and not stability",
        ],
    )
    def test_every_other_grouped_or_shape_refuses(self, expr: str) -> None:
        with pytest.raises(pytest.UsageError):
            _conjunction_clauses(expr)


def serial_twin_offenders(legs: "list[_Leg]") -> "tuple[int, list[str]]":
    """How many pairs were judged, and the serial legs that disagree with their twin.

    Pairing is by ADJACENCY within a surface: the leg right after a
    `not serial_timing` parallel leg, on the same roots, whose expression
    begins `serial_timing and`. That is the shape every lane spells today
    (the recipe's next line, the session's next `session.run`), and the count
    is returned so the caller can hold it EQUAL to the number of parallel
    legs that exclude the marker — a lane whose halves are not adjacent, or
    whose serial leg is spelled another way, is otherwise silently unjudged.
    """
    offenders: "list[str]" = []
    pairs = 0
    for parallel, serial in itertools.pairwise(legs):
        if parallel.surface != serial.surface or parallel.roots != serial.roots:
            continue
        if "not serial_timing" not in parallel.expr or not serial.expr.startswith(
            "serial_timing and "
        ):
            continue
        pairs += 1
        expected = _conjunction_clauses(parallel.expr) - {"not serial_timing"}
        actual = _conjunction_clauses(serial.expr) - {"serial_timing"}
        missing, extra = sorted(expected - actual), sorted(actual - expected)
        if missing or extra:
            offenders.append(
                f"{serial} — its parallel twin is `-m {parallel.expr}`; "
                f"missing {missing}, extra {extra}"
            )
    return pairs, offenders


def test_every_serial_leg_carries_its_parallel_twins_exclusions() -> None:
    legs = lane_legs()
    pairs, offenders = serial_twin_offenders(legs)
    excluders = [leg for leg in legs if "not serial_timing" in leg.expr]
    assert excluders, "premise: no parallel leg excludes serial_timing — the extractor is broken"
    # Against the population, not a count: a lane whose halves are not
    # adjacent would leave a fixed floor green and its serial leg unjudged.
    assert pairs == len(excluders), (
        f"{len(excluders)} parallel legs exclude serial_timing but only {pairs} were paired "
        f"with an adjacent `serial_timing and ...` twin on the same roots — fix the pairing "
        f"(or teach it the new shape), never the count"
    )
    assert not offenders, (
        "a serial leg dropped (or added) an exclusion its parallel twin carries, so the two "
        "legs of one lane disagree about what the lane is:\n  " + "\n  ".join(offenders)
    )


def test_the_serial_twin_scanner_reddens_on_a_dropped_exclusion() -> None:
    """Positive control: the drift this pin was written against, in miniature."""
    parallel = _Leg("x", "not stability and not busybox and not serial_timing", ("tests/unit",))
    serial = _Leg("x", "serial_timing and not stability", ("tests/unit",))
    pairs, offenders = serial_twin_offenders([parallel, serial])
    assert pairs == 1
    assert len(offenders) == 1
    assert "missing ['not busybox']" in offenders[0], offenders

    faithful = _Leg("x", "serial_timing and not busybox and not stability", ("tests/unit",))
    assert serial_twin_offenders([parallel, faithful]) == (1, [])

    # Different roots are not a pair, whatever the expressions say.
    elsewhere = _Leg("x", "serial_timing and not stability", ("tests/e2e",))
    assert serial_twin_offenders([parallel, elsewhere]) == (0, [])


def test_the_serial_twin_scanner_refuses_a_shape_it_cannot_read() -> None:
    parallel = _Leg("x", "not stability and not serial_timing", ())
    serial = _Leg("x", "serial_timing and (stability and browser)", ())
    with pytest.raises(pytest.UsageError, match="teach this pin the shape"):
        serial_twin_offenders([parallel, serial])


# ── Release census ──────────────────────────────────────────────────────────
#
# "Releases lose no tests" (issue #566) has a mechanism, `make release-campaign`,
# and this is its proof. Two halves:
#
# * RELEASE_LANES is a literal manifest of every pytest leg `make release`
#   reaches, held EQUAL to what `release_legs()` finds by walking the Makefile
#   from `release` (through `$(MAKE)` calls and prerequisites, into the nox
#   sessions a reached recipe runs, on the interpreter it runs them on). The
#   manifest is spelled out so a reader sees the release's test surface in one
#   place; the equality keeps it honest in both directions.
# * Every item pytest collects under `testpaths` must be selected by at least
#   one manifest leg, or by an owner-exempt row (_RELEASE_EXEMPT_LANES, each
#   with its reason, held to being live and unreached by the release): the
#   leg's roots contain the item's file and the leg's expression matches the
#   item's markers (pytest's own compiler, as above).
#
# STATED BOUNDS. The census sees the HERMETIC collection: items that exist only
# under a venue or plugin — the conformance bed venue's cells, the browser
# engines' parametrisations — are covered by the LANE being required
# (test_the_release_reaches_every_required_lane), not by item identity.
# Selection, not execution: a skip or xfail inside a lane is that lane's
# business. A reached leg selects by its roots and its `-m` ONLY: any narrowing
# flag (`-k`, `--deselect`, `--ignore`, `--lf`, `-o testpaths=…`, …), whether
# on the pytest line or forwarded through a nox `--`, is refused — forwarded
# arguments are read literally, so a flag hidden in a `$(VAR)` there is as
# invisible as a wrapper script. A pytest invocation with NO `-m` (lint-arch's
# import-rule files) is a leg whose selector is everything under its roots,
# spelled _ANY_MARKER.
# The walker does not follow the recipes of FILE-target rules (`$(TS_E2E_RAW_STAMP)`,
# `docs/_build/html/index.html`): a pytest lane reachable ONLY through one is
# invisible to it — today both lead to a stage reached elsewhere (`dashboard`)
# or to no pytest at all (sphinx). Everything the walker cannot read it REFUSES:
# an unresolvable `$(VAR)`, a `$(MAKE)` goal that is no rule target, a nox line
# in a shape other than `nox -s <sessions> [-- …]`, a `$(MAKE)` line naming two
# goals. A nox run spelled through a variable or a wrapper script (`$(NOX) -s x`,
# `scripts/run_nox.sh`) is INVISIBLE to it, as a pytest run hidden behind a
# script is to every scanner here — no such spelling exists today.

_MAKE_CALL = re.compile(r"\$\(MAKE\)\s+(?:--no-print-directory\s+)?([a-zA-Z0-9_.$()-]+)")
_NOX_SESSIONS = re.compile(r"\bnox\s+-s\s+(.*?)(?:\s+--\s+(.*?))?\s*$")
_NOX_WORD = re.compile(r"(?:^|\s)nox(?:\s|$)")
# A pytest flag that selects LESS than the roots and `-m` say (or runs nothing):
# a reached leg carrying one is refused, directly or forwarded through nox.
_FILTER_FLAGS = (
    "-k",
    "--deselect",
    "--ignore",
    "--ignore-glob",
    "--collect-only",
    "--co",
    "--lf",
    "--last-failed",
    "--sw",
    "--stepwise",
)
_OVERRIDE_FLAGS = ("-o", "--override-ini")


def _narrowing(tokens: "list[str]") -> "list[str]":
    """The tokens of *tokens* that narrow pytest's selection past its roots and `-m`."""
    found: "list[str]" = []
    for index, token in enumerate(tokens):
        attached = token.startswith((*(f"{f}=" for f in _FILTER_FLAGS), "-k"))
        # `-o testpaths=x`, `-o=testpaths=x`, `-otestpaths=x` and the long spellings
        overrides = index > 0 and tokens[index - 1] in _OVERRIDE_FLAGS
        testpaths = (overrides and token.startswith("testpaths=")) or token.startswith(
            tuple(f"{f}{sep}testpaths=" for f in _OVERRIDE_FLAGS for sep in ("", "="))
        )
        if token in _FILTER_FLAGS or attached or testpaths:
            found.append(token)
    return found


_FOREACH = re.compile(r"\$\(foreach\s+(\w+),\$\(([A-Za-z_]+)\),([^)]*\$\(\w+\))\)")
_ANY_MARKER = (
    "not no_such_marker_ever"  # compiles; every item matches (the matcher knows no such name)
)


def _makefile_assignments(text: str) -> "dict[str, str]":
    """``NAME := value`` / ``NAME ?= value`` / ``NAME = value`` at column 0, literally."""
    return dict(re.findall(r"^([A-Za-z_][A-Za-z0-9_]*)\s*[:?]?=\s*(.*)$", text, re.MULTILINE))


def _resolve(name: str, assignments: "dict[str, str]", where: str) -> str:
    """*name* with every ``$(VAR)`` replaced from *assignments*; refuses what it cannot resolve."""
    while "$(" in name:
        variable = re.search(r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)", name)
        known = variable is not None and variable.group(1) in assignments
        assert known, (
            f"{where}: `{name}` names a variable this reader cannot resolve from the "
            f"Makefile's assignments — teach it the shape, never skip the stage"
        )
        name = name.replace(variable.group(0), assignments[variable.group(1)])
    return name


_RULE_LINE = re.compile(r"^([a-zA-Z0-9_.-]+):(?![=])([^#\n]*)", re.MULTILINE)
# A token shaped like a goal name: no `=`, no `-` prefix, no `$(`, no shell separator.
_GOAL_TOKEN = re.compile(r"[a-zA-Z0-9_.][a-zA-Z0-9_.-]*")


def _makefile_prerequisites(text: str) -> "dict[str, list[str]]":
    """Target -> the prerequisite names on its rule line, literally (``$(VAR)`` included).

    The caller resolves variables and drops what resolves to a path; nothing is
    dropped here, so a `release: $(STAGES)` cannot lose a stage silently.
    """
    out: "dict[str, list[str]]" = {}
    for match in _RULE_LINE.finditer(text):
        rest = match.group(2)
        if "=" in rest or rest.lstrip().startswith("export "):
            continue  # a target-specific variable line, not a rule
        out.setdefault(match.group(1), []).extend(rest.split())
    return out


def make_reachable(start: str, text: str) -> "list[str]":
    """Every target `make <start>` reaches, in first-seen order.

    Through ``$(MAKE) x`` calls in the LIVE recipe lines (comments and
    `$(SAY)` banners dropped, column-0 comments inside a recipe stepped over)
    and the prerequisites on rule lines, every ``$(VAR)`` resolved. A name that
    resolves to a path (`/`) is a file prerequisite and is not walked; every
    other name must be a defined rule target, so a `$(MAKE) COUNT=3 stage`,
    `$(MAKE) -s stage` or `$(MAKE) $@` is refused rather than walked wrongly.
    A `$(MAKE) a b` line is refused too: this reader walks ONE goal per call
    (the Makefile spells one per line), and inspects only the token DIRECTLY
    after the goal — a goal-shaped one there is refused; a goal later on the
    line (`$(MAKE) a VAR=1 b`) is not seen.
    """
    prereqs = _makefile_prerequisites(text)
    assignments = _makefile_assignments(text)
    targets = {match.group(1) for match in _RULE_LINE.finditer(text)}
    seen: "list[str]" = []
    queue = [(start, "the start")]
    while queue:
        target, origin = queue.pop(0)
        if target in seen:
            continue
        assert target in targets, (
            f"`{target}` ({origin}) is neither a rule target of the Makefile nor a path: a "
            f"plain file prerequisite without a `/`, or a `$(MAKE)` goal this reader "
            f"mis-read — teach it the shape, never skip it"
        )
        seen.append(target)
        live = "\n".join(makefile_live_recipe(text, target))
        calls = list(_MAKE_CALL.finditer(live))
        for call in calls:
            rest = live[call.end() :].split("\n", 1)[0].split()
            second = rest[0] if rest and _GOAL_TOKEN.fullmatch(rest[0]) else None
            assert second is None, (
                f"`{target}` runs `$(MAKE) {call.group(1)} {second}` — two goals on one "
                f"line; this reader walks one per call, so split the line"
            )
        found = [(name, f"a prerequisite of `{target}`") for name in prereqs.get(target, [])]
        found += [(call.group(1), f"a `$(MAKE)` goal in `{target}`") for call in calls]
        for name, where in found:
            resolved = _resolve(name, assignments, f"`{target}`")
            queue.extend((part, where) for part in resolved.split() if "/" not in part)
    return seen


def nox_sessions_run_by(
    lines: "list[str]", assignments: "dict[str, str]"
) -> "list[tuple[str, str | None]]":
    """(session, python) for every nox session the recipe lines run.

    A python of None means the session's own default interpreter.
    """
    found: "list[tuple[str, str | None]]" = []
    for line in lines:
        matches = list(_NOX_SESSIONS.finditer(line))
        # `$(MAKE) nox` names a make GOAL, not the program; strip those first.
        assert matches or not _NOX_WORD.search(_MAKE_CALL.sub("", line)), (
            f"a recipe line runs nox in a shape this reader cannot read (only "
            f"`nox -s <sessions> [-- …]` is): {line.strip()!r}"
        )
        for match in matches:
            tail = match.group(1)
            # Everything after `--` reaches pytest through session.posargs.
            forwarded = _narrowing((match.group(2) or "").split())
            assert not forwarded, (
                f"nox forwards {forwarded} to pytest in {line.strip()!r}; a reached leg "
                f"selects by its roots and `-m` only — a forwarded flag narrows what runs "
                f"without changing either, so the census could not see the loss"
            )
            for each in _FOREACH.finditer(tail):
                var, listname, body = each.groups()
                expansion = " ".join(
                    body.replace(f"$({var})", v)
                    for v in _resolve(f"$({listname})", assignments, line.strip()).split()
                )
                tail = tail.replace(each.group(0), expansion)
            for token in tail.split():
                assert not token.startswith("-"), (
                    f"nox option {token!r} after the session names in {line.strip()!r} — "
                    f"it could pick the interpreter (`-p`) or the sessions (`-k`), and this "
                    f"reader would credit the wrong legs"
                )
                resolved = _resolve(token, assignments, line.strip())
                name, _, python = resolved.partition("-")
                readable = re.fullmatch(r"[a-z][a-z0-9_]*", name) and (
                    not python or re.fullmatch(r"\d+\.\d+", python)
                )
                assert readable, f"unreadable nox session token {resolved!r} in {line.strip()!r}"
                if (name, python or None) not in found:
                    found.append((name, python or None))
    return found


def _legs_including_markerless(
    surface: str, invocations: "list[list[str]]", variables: "dict[str, str] | None" = None
) -> "list[_Leg]":
    """Like _legs_from_invocations, but a `-m`-less pytest is a leg over all of its roots.

    EVERY reached leg, marked or not, is refused if it narrows past its roots
    and `-m`: `-k`/`--deselect`/`--ignore`/`-o testpaths=` select less than
    the row says while leaving the row unchanged, so neither the manifest
    equality nor the item census could see the loss; `--collect-only`/`--lf`/
    `--sw` run less than they select. A `-m`-less leg must further NAME its
    roots: path-less, it would be `_ANY_MARKER` over testpaths — a row that
    selects every item and makes the census vacuously green.
    """
    legs: "list[_Leg]" = []
    for tokens in invocations:
        argv = " ".join(tokens[tokens.index("pytest") :])
        narrowing = _narrowing(tokens[tokens.index("pytest") :])
        assert not narrowing, (
            f"{surface}: `{argv}` narrows with {narrowing}; a reached leg selects by its "
            f"roots and `-m` only — drop the flag, or teach the reader (and the census) "
            f"what it deselects"
        )
        if "-m" in tokens:
            legs += _legs_from_invocations(surface, [tokens], variables)
            continue
        roots = tuple(str(path.relative_to(_REPO)) for path in _selected_roots(tokens))
        assert roots, (
            f"{surface}: `{argv}` has no `-m` and names no path, so it is every item under "
            f"testpaths — a row that would make the census vacuously green"
        )
        repeated = any(token == "--count" or token.startswith("--count=") for token in tokens)
        legs.append(_Leg(surface, _ANY_MARKER, roots, repeated))
    return legs


# The hostless sessions the pull request runs on the primary Python, one per
# tier; together they are the hostless half of the coverage selection.
_PRIMARY_CI_SESSIONS = ("tests_hostless_unit", "tests_hostless_e2e", "tests_hostless_guards")


def primary_ci_legs() -> "list[_Leg]":
    """The pull request's hostless legs on the primary Python, read from the noxfile."""
    legs: "list[_Leg]" = []
    for session in _PRIMARY_CI_SESSIONS:
        legs += _legs_including_markerless(
            f"noxfile.py:{session}@3.10",
            _python_pytest_invocations(
                _REPO / "noxfile.py", resolve_names=True, scope=session, python="3.10"
            ),
        )
    return legs


def release_legs() -> "list[_Leg]":
    """Every pytest leg `make release` reaches (see :func:`make_legs`)."""
    return make_legs("release")


def make_legs(start: str) -> "list[_Leg]":
    """Every pytest leg `make <start>` reaches.

    Labelled `Makefile:<target>` or `noxfile.py:<session>@<python>`.
    """
    text = (_REPO / _MAKEFILE).read_text()
    variables = _makefile_marker_variables(text)
    assignments = _makefile_assignments(text)
    legs: "list[_Leg]" = []
    for target in make_reachable(start, text):
        live = makefile_live_recipe(text, target)
        legs += _legs_including_markerless(
            f"{_MAKEFILE}:{target}", _makefile_pytest_invocations("\n".join(live)), variables
        )
        for session, python in nox_sessions_run_by(live, assignments):
            legs += _legs_including_markerless(
                f"noxfile.py:{session}@{python or 'default'}",
                _python_pytest_invocations(
                    _REPO / "noxfile.py", resolve_names=True, scope=session, python=python
                ),
            )
    return legs


# The release's test surface, spelled out. Equal to release_legs() — see
# test_the_manifest_is_exactly_what_the_release_reaches, whose failure message
# lists the rows to add or drop. Roots are relative to the checkout; () means
# pyproject's testpaths. A nox surface carries the interpreter the release runs
# it on ("@3.10"); "@default" is a session whose Python the recipe did not pick.
_COVERAGE = (
    "not stability and not browser and not busybox and not conformance and not kmodcov "
    "and not release_proof and not serial_timing"
)
_COVERAGE_SERIAL = (
    "serial_timing and not stability and not browser and not busybox and not conformance "
    "and not kmodcov and not release_proof"
)


def _marker_expr(args: "tuple[str, ...]") -> str:
    """The `-m` value of a noxfile argument bundle: the selection, read from its one home."""
    return args[args.index("-m") + 1]


# The canary's selections, read from the noxfile bundles the sessions run, so the
# partition test below judges the live expressions, never a retyped copy.
_ALL_NONPRIMARY = _marker_expr(TESTS_ALL_NONPRIMARY_TEST_ARGS)
_ALL_NONPRIMARY_SERIAL = _marker_expr(TESTS_ALL_NONPRIMARY_SERIAL_ARGS)
_HOSTLESS_MIDDLE = _marker_expr(HOSTLESS_MIDDLE_TEST_ARGS)
_HOSTLESS_MIDDLE_SERIAL = _marker_expr(HOSTLESS_MIDDLE_SERIAL_ARGS)
_HOSTLESS_E2E_CANARY = _marker_expr(HOSTLESS_E2E_CANARY_TEST_ARGS)
_HOSTLESS_E2E_CANARY_SERIAL = _marker_expr(HOSTLESS_E2E_CANARY_SERIAL_ARGS)
_BED_NONPRIMARY = _marker_expr(TESTS_BED_TEST_ARGS)
_BED_NONPRIMARY_SERIAL = _marker_expr(TESTS_BED_SERIAL_ARGS)
_STABILITY_UNIT_FILES = (
    "tests/unit/host/test_app_shell_concurrency.py",
    "tests/unit/host/test_session_concurrency.py",
    "tests/unit/host/test_unix_host.py",
    "tests/unit/monitor/test_collector_tunnel_soak.py",
)
_STABILITY_UNIX_FILES = (
    "tests/integration/host/test_app_shell_stability_integration.py",
    "tests/integration/host/test_host_stability_contract.py",
    "tests/integration/host/test_proxy_user_stability_integration.py",
    "tests/integration/host/test_session_stability_integration.py",
)
RELEASE_LANES: "tuple[_Leg, ...]" = (
    # release-preflight → the live-ruleset pin (skipped unless OTTO_RULESET_CHECK=1,
    # which the recipe sets).
    _Leg("Makefile:release-preflight", _ANY_MARKER, ("tests/unit/test_landing_ruleset.py",)),
    # release-matrix → conformance-bed: the whole conformance tree, BED venue.
    _Leg("Makefile:conformance-bed", "conformance", ()),
    # release-kmodcov-matrix → kmodcov: the toolchain proofs on the bed.
    _Leg("Makefile:kmodcov", "kmodcov", ()),
    # docs → doctest-src: the src doctests (no `-m`, cleared addopts), outside testpaths.
    _Leg("Makefile:doctest-src", _ANY_MARKER, ("src/otto",)),
    # `release` runs `coverage_stamp.py check || $(MAKE) coverage` first: when
    # HEAD's tree carries no valid stamp the release runs `make coverage`
    # itself (Q5), so the coverage-python legs are release legs — the
    # family's only release selection since Phase B (3.14 deselects it).
    _Leg("Makefile:coverage-python", _COVERAGE, ()),
    _Leg("Makefile:coverage-python", _COVERAGE_SERIAL, ()),
    # nox-release → tests_bed on the canary only: the bed half of the coverage
    # selection (the family deselected — it runs on the primary only, #566 Q2 —
    # and coverage off). The canary's and the middle Pythons' hostless halves are
    # CI's tests_hostless-3.1x sessions; the primary's session is what the stamp proves.
    # The literals are the manifest's declaration; the partition test reads the noxfile.
    _Leg(
        "noxfile.py:tests_bed@3.14",
        "(integration or embedded) and not stability and not browser and not busybox "
        "and not conformance and not kmodcov and not release_proof "
        "and not serial_timing and not interpreter_agnostic",
        (),
    ),
    _Leg(
        "noxfile.py:tests_bed@3.14",
        "serial_timing and (integration or embedded) and not stability and not browser "
        "and not busybox and not conformance and not kmodcov and not release_proof "
        "and not interpreter_agnostic",
        (),
    ),
    # coverage-python's `dashboard` prerequisite: the two browser suites on
    # DASHBOARD_BROWSERS (chromium); the other engines are CI's dashboard-e2e.
    _Leg(
        "Makefile:dashboard",
        "browser and not soak",
        ("tests/e2e/monitor/dashboard", "tests/e2e/cov/report_browser"),
    ),
    # release-campaign: every lane the per-change gates exclude, once each (COUNT=1).
    _Leg("Makefile:stability-unit", "concurrency", _STABILITY_UNIT_FILES, repeated=True),
    _Leg(
        "Makefile:stability-unix",
        "stability and integration and not embedded and not hops and not chaos",
        _STABILITY_UNIX_FILES,
        repeated=True,
    ),
    _Leg(
        "Makefile:stability-tunnel",
        "stability and hops",
        ("tests/e2e/tunnel_stability",),
        repeated=True,
    ),
    # release-campaign → console-release-bed: the #386 bed proof, its only lane.
    _Leg(
        "Makefile:console-release-bed",
        "release_proof and embedded",
        ("tests/integration/host/test_console_release_bed.py",),
    ),
    _Leg(
        "Makefile:stability-embedded",
        "stability and embedded and not chaos",
        ("tests/integration/host/test_host_stability_contract.py",),
        repeated=True,
    ),
    _Leg(
        "Makefile:dashboard-soak",
        "browser and soak",
        ("tests/e2e/monitor/dashboard/test_replay_soak.py",),
    ),
    _Leg("Makefile:conformance", "conformance and not conformance_bed", ()),
    _Leg("Makefile:busybox", "busybox", ()),
)

_LEGS_CI_PROVES = (
    "Makefile:check-python",
    "Makefile:lint-arch",
    "Makefile:docs@first",
    "noxfile.py:tests_hostless_unit@3.11",
    "noxfile.py:tests_hostless_unit@3.12",
    "noxfile.py:tests_hostless_unit@3.13",
    "noxfile.py:tests_all@3.14:hostless",
    "Makefile:dashboard-all",
    "Makefile:check-ts",
    "Makefile:coverage-ts",
)
"""An excerpt of the pre-2026-11 release census: the legs the old `make release`
ran that the release now trusts CI (or the coverage stamp) for."""


def test_the_evidence_map_is_exactly_the_legs_the_release_stopped_running() -> None:
    """Both ways: every dropped leg has evidence; each evidence key is a leg the old release ran."""
    from scripts import release_stages

    assert set(_LEGS_CI_PROVES) == set(release_stages.EVIDENCE)
    assert len(set(_LEGS_CI_PROVES)) == len(_LEGS_CI_PROVES)


# OWNER EXEMPTIONS. Lanes the release deliberately does NOT run, each with its
# reason, spelled as the leg it would be. The census treats their selection as
# accounted for, so "no test left out" keeps meaning "in the release, or
# named here"; test_the_exemptions_are_live_and_not_in_the_release holds every
# row to two facts: it covers tests the release rows do not, and no release
# stage reaches it (an exemption the release runs anyway is a stale lie).
# Never add a row here without the owner.
_CUT_SWEEP_MODULE = "tests/unit/host/test_probe_line_cut_sweep.py"
_RELEASE_EXEMPT_LANES: "tuple[_Leg, ...]" = (
    # Chris, 2026-10-15: the chaos lanes are on demand only. They are
    # bed-hostile by construction (warm reboots, SIGKILL mid-impair, SSH
    # blackholes) and a red there is as often a bed finding as a product
    # defect — two runs on 2026-10-14 each stranded a lab VM in its firmware
    # after a warm reboot. A release must not wait on that. #643 tracks a
    # scheduled dev-VM run so the lane keeps producing signal.
    _Leg("noxfile.py:chaos@default", "chaos and not embedded", ("tests/e2e/chaos",)),
    _Leg("noxfile.py:chaos_embedded@default", "chaos and embedded", ("tests/e2e/chaos",)),
    # Chris, 2026-11-07 (#853): the every-offset probe-line cut sweep is nightly-only,
    # plus one by-hand pass when it landed. It is deterministic, and its fixed
    # hazard cases run on every push; the sweep adds exhaustiveness (~13k
    # fresh shells, about two hours serial), which a release need not wait
    # on. Nightly's `probe-cut-sweep` job runs it.
    _Leg(
        "Makefile:stability-cut-sweep",
        "stability",
        (_CUT_SWEEP_MODULE,),
    ),
)

# Pinned by reachability: lanes whose items the hermetic collection cannot show
# (venue/plugin-dependent), the src doctests (no `-m`, cleared addopts), every
# release-campaign lane — some are shadowed at item level (`concurrency` by
# tests_all, hermetic conformance by the bed row), so only reachability pins
# them — and the orchestrators that reach them (`release-campaign`, `stability`).
_REQUIRED_LANES = (
    "release-matrix",
    "conformance-bed",
    # the stamp fallback: `make coverage` must stay reachable from `release`, and
    # its stamp prologue with it
    "coverage",
    "coverage-stamp-start",
    "coverage-python",
    "nox-release",
    "release-kmodcov-matrix",
    "kmodcov",
    "dashboard",
    "docs",
    "doctest-src",
    "release-campaign",
    "stability",
    "stability-unit",
    "stability-unix",
    "stability-tunnel",
    "stability-embedded",
    "console-release-bed",
    "dashboard-soak",
    "conformance",
    "busybox",
)


def _manifest_diff(
    rows: "tuple[_Leg, ...]", reached: "list[_Leg]"
) -> "tuple[list[str], list[str]]":
    """(reached but not in the manifest, in the manifest but not reached), sorted as strings."""
    missing = sorted(str(leg) for leg in set(reached) - set(rows))
    stale = sorted(str(leg) for leg in set(rows) - set(reached))
    return missing, stale


def _within(path: str, roots: "tuple[str, ...]") -> bool:
    """Whether a leg over `roots` reaches `path`; `()` is pyproject's testpaths, so every path."""
    return not roots or gs._within(path, roots)


def _selected_node_ids(legs: "list[_Leg] | tuple[_Leg, ...]", items) -> "set[str]":
    """The node ids *legs* select from *items*: the one selection every census shares.

    A leg selects an item when its roots contain the item's file (:func:`_within`) AND its
    `-m` expression matches the item's markers; each leg is judged over its own roots only.
    """
    compiled = [(leg, Expression.compile(leg.expr)) for leg in legs]
    return {
        item.nodeid
        for item in items
        if any(
            _within(item.path, leg.roots) and expr.evaluate(_marker_matcher(item.markers))
            for leg, expr in compiled
        )
    }


def _release_census_gaps(rows: "tuple[_Leg, ...]", collect) -> "list[str]":
    """Node ids collected under testpaths that NO row selects (:func:`_selected_node_ids`)."""
    items = list(collect(()))
    selected = _selected_node_ids(rows, items)
    return [item.nodeid for item in items if item.nodeid not in selected]


def test_the_manifest_is_exactly_what_the_release_reaches() -> None:
    """Both directions: a row no stage runs is a lie; a reached leg with no row is a blind spot."""
    missing, stale = _manifest_diff(RELEASE_LANES, release_legs())
    drift = missing + stale
    assert not drift, (
        "RELEASE_LANES and the legs `make release` reaches differ.\n"
        "Reached by the release but not in the manifest (add these rows):\n  "
        + "\n  ".join(missing)
        + "\nIn the manifest but reached by no release stage (drop these rows):\n  "
        + "\n  ".join(stale)
    )


def test_the_manifest_diff_reports_each_direction() -> None:
    """Positive control for both directions of the equality, on synthetic legs."""
    a = _Leg("synthetic:a", "x", ())
    b = _Leg("synthetic:b", "y", ("tests/unit",))
    c = _Leg("synthetic:c", "z", ())
    assert _manifest_diff((a, b), [a, c]) == ([str(c)], [str(b)])
    assert _manifest_diff((a, b), [b, a]) == ([], [])


def test_the_release_reaches_every_required_lane() -> None:
    reached = make_reachable("release", (_REPO / _MAKEFILE).read_text())
    missing = [target for target in _REQUIRED_LANES if target not in reached]
    assert not missing, f"`make release` no longer reaches {missing}; reached: {reached}"


def test_the_release_walker_reads_the_shapes_the_makefile_uses() -> None:
    """Literal text, every shape the walker reads — and every shape it refuses.

    Read: `$(MAKE) x`, `--no-print-directory`, a `$(MAKE)` after a column-0
    comment inside the recipe (GNU make allows it; this Makefile does it), a
    prerequisite, a `$(VAR)` prerequisite resolved to targets, a `$(VAR)` one
    resolved to a file (not walked), a `$(VAR)` goal resolved, a tab-commented
    `$(MAKE)` and a `$(SAY)` banner naming one (neither runs, neither walked),
    and nox session tokens with a `$(foreach …)`.
    Refused: a `$(VAR)` nobody defines, a `$(MAKE)` goal that is no rule target
    (`COUNT=3 stage` captures `COUNT`), a plain-file prerequisite without a `/`
    (each refusal names its origin), a `$(MAKE)` line naming two goals, a
    nox line in another shape, a nox option after the session names, and a
    narrowing flag forwarded to pytest after `--`.
    """
    text = (
        "STAGE := two\n"
        "MIDDLE := 3.11 3.12\n"
        "EXTRA := four five\n"
        "STAMP := reports/x/.stamp\n"
        "one: pre $(EXTRA) $(STAMP)\n"
        "\t@$(MAKE) $(STAGE)\n"
        "# a column-0 comment inside the recipe\n"
        "\t@$(MAKE) --no-print-directory three || true\n"
        '\t@$(SAY) "next: $(MAKE) never"\n'
        "\t# @$(MAKE) never\n"
        "pre:\n"
        "\t@echo pre\n"
        "two:\n"
        "\t@echo two\n"
        "three:\n"
        "\t@uv run nox -s tests_all-3.10 $(foreach v,$(MIDDLE),tests_hostless-$(v)) chaos"
        " -- --count=1\n"
        "four:\n"
        "\t@echo four\n"
        "five:\n"
        "\t@echo five\n"
        "never:\n"
        "\t@echo never\n"
        "bad:\n"
        "\t@$(MAKE) $(UNDEFINED)\n"
        "worse:\n"
        "\t@$(MAKE) COUNT=3 two\n"
        "nofile: plainfile\n"
        "\t@echo nofile\n"
        "twice:\n"
        "\t@$(MAKE) two three\n"
        "nox_k:\n"
        "\t@uv run nox -k chromium\n"
        "nox_p:\n"
        "\t@uv run nox -s tests_hostless -p 3.11\n"
        "nox_ignore:\n"
        "\t@uv run nox -s chaos -- --ignore=tests/e2e/chaos/test_x.py\n"
    )
    assert make_reachable("one", text) == ["one", "pre", "four", "five", "two", "three"]
    assignments = _makefile_assignments(text)
    assert nox_sessions_run_by(makefile_live_recipe(text, "three"), assignments) == [
        ("tests_all", "3.10"),
        ("tests_hostless", "3.11"),
        ("tests_hostless", "3.12"),
        ("chaos", None),
    ]
    with pytest.raises(AssertionError, match="cannot resolve"):
        make_reachable("bad", text)
    with pytest.raises(AssertionError, match=r"`COUNT` \(a `\$\(MAKE\)` goal in `worse`\)"):
        make_reachable("worse", text)
    with pytest.raises(AssertionError, match=r"`plainfile` \(a prerequisite of `nofile`\)"):
        make_reachable("nofile", text)
    with pytest.raises(AssertionError, match="two goals on one line"):
        make_reachable("twice", text)
    with pytest.raises(AssertionError, match="shape this reader cannot read"):
        nox_sessions_run_by(makefile_live_recipe(text, "nox_k"), assignments)
    with pytest.raises(AssertionError, match="nox option '-p'"):
        nox_sessions_run_by(makefile_live_recipe(text, "nox_p"), assignments)
    with pytest.raises(AssertionError, match=r"nox forwards \['--ignore=tests/e2e/chaos"):
        nox_sessions_run_by(makefile_live_recipe(text, "nox_ignore"), assignments)


def test_the_session_reader_follows_the_interpreter() -> None:
    """Each hostless session contributes the bundles of the interpreter it is read for.

    tests_hostless_unit: the UNIT pair on 3.10 (instrumented, `--cov-append` on
    the serial leg), the MIDDLE pair on 3.11 (`--no-cov`); tests_hostless_e2e:
    the instrumented pair on 3.10, the `--no-cov` canary on 3.14. Compared on
    the RAW tokens, not the `-m` text: the 3.10 and 3.11 unit pairs and the
    3.10 and 3.14 e2e pairs select alike, so only the tokens can show which
    branch of the `session.python` ladder was taken.
    """

    def read(session: str, python: str) -> "list[list[str]]":
        return _python_pytest_invocations(
            _REPO / "noxfile.py", resolve_names=True, scope=session, python=python
        )

    invocations = {
        ("unit", "3.10"): read("tests_hostless_unit", "3.10"),
        ("unit", "3.11"): read("tests_hostless_unit", "3.11"),
        ("e2e", "3.10"): read("tests_hostless_e2e", "3.10"),
        ("e2e", "3.14"): read("tests_hostless_e2e", "3.14"),
    }
    assert all(len(pair) == 2 for pair in invocations.values()), invocations
    tokens = {key: {t for argv in pair for t in argv} for key, pair in invocations.items()}
    exprs = {
        key: {argv[argv.index("-m") + 1] for argv in pair} for key, pair in invocations.items()
    }
    for instrumented, bare in (
        (("unit", "3.10"), ("unit", "3.11")),
        (("e2e", "3.10"), ("e2e", "3.14")),
    ):
        assert "--cov-append" in tokens[instrumented], tokens[instrumented]
        assert "--no-cov" not in tokens[instrumented], tokens[instrumented]
        assert "--no-cov" in tokens[bare], tokens[bare]
        assert not [t for t in tokens[bare] if t.startswith("--cov-")], tokens[bare]
        assert exprs[bare] == exprs[instrumented], (exprs[instrumented], exprs[bare])
    # The unit legs deselect the family on every interpreter; the e2e legs never name it.
    assert all("not interpreter_agnostic" in e for e in exprs["unit", "3.11"]), exprs
    assert not any("interpreter_agnostic" in e for e in exprs["e2e", "3.10"]), exprs
    assert "tests/e2e" not in tokens["unit", "3.10"], tokens["unit", "3.10"]
    assert "tests/unit" not in tokens["e2e", "3.10"], tokens["e2e", "3.10"]


def test_the_session_reader_refuses_a_ladder_it_cannot_resolve(tmp_path: Path) -> None:
    """Synthetic noxfiles: every `session.python` shape but the top-level ladder is refused.

    A ladder nested in a `with` survives the top-level pass with both branches
    readable; `in (...)` is not `==`; `if session.posargs:` picks a branch this
    reader cannot see. The last case is the complement: the real shape reads.
    """
    bundle = 'A = ("tests/unit", "-m", "not stability")\nB = ("tests/e2e", "-m", "stability")\n'
    shapes = {
        "membership": ("    if session.python in ('3.10', '3.14'):\n", "cannot resolve"),
        "nested": (
            "    with session.chdir('.'):\n        if session.python == '3.10':\n",
            "cannot resolve",
        ),
        "posargs": ("    if session.posargs:\n", "cannot resolve"),
        "non_str": ("    if session.python == 3.1:\n", "not a str"),
    }
    for name, (head, refusal) in shapes.items():
        indent = "        " if name == "nested" else "    "
        body = (
            f"{head}{indent}    session.run('pytest', *A)\n{indent}else:\n"
            f"{indent}    session.run('pytest', *B)\n"
        )
        probe = tmp_path / f"{name}.py"
        probe.write_text(f"{bundle}def s(session):\n{body}")
        with pytest.raises(AssertionError, match=refusal):
            _python_pytest_invocations(probe, resolve_names=True, scope="s", python="3.10")
    readable = tmp_path / "ladder.py"
    readable.write_text(
        f"{bundle}P = '3.10'\ndef s(session):\n    if session.python == P:\n"
        "        session.run('pytest', *A)\n    else:\n        session.run('pytest', *B)\n"
    )
    found = _python_pytest_invocations(readable, resolve_names=True, scope="s", python="3.14")
    assert found == [["pytest", "tests/e2e", "-m", "stability"]], found
    # A run on the session's DEFAULT interpreter has nothing to resolve the ladder
    # for; read with both branches it would credit a leg the run never executes.
    with pytest.raises(AssertionError, match="cannot resolve"):
        _python_pytest_invocations(readable, resolve_names=True, scope="s", python=None)


def test_a_reached_leg_selects_by_roots_and_marker_only() -> None:
    """Positive controls for the shapes `_legs_including_markerless` refuses.

    A narrowing flag is refused on a MARKED leg as on a `-m`-less one: the
    manifest row (roots, `-m`) would not change, so only this refusal sees it.
    """
    named = ["uv", "run", "pytest", "tests/unit/test_patch_targets.py", "-q", "--no-cov"]
    marked = ["uv", "run", "pytest", "tests/unit", "-m", "not stability", "--no-cov"]
    (leg,) = _legs_including_markerless("synthetic", [named])
    assert leg == _Leg("synthetic", _ANY_MARKER, ("tests/unit/test_patch_targets.py",))
    (leg,) = _legs_including_markerless("synthetic", [marked])
    assert leg == _Leg("synthetic", "not stability", ("tests/unit",))
    with pytest.raises(AssertionError, match="names no path"):
        _legs_including_markerless("synthetic", [["uv", "run", "pytest", "-q"]])
    for base in (named, marked):
        for flag in _FILTER_FLAGS:
            with pytest.raises(AssertionError, match="narrows with"):
                _legs_including_markerless("synthetic", [[*base, flag, "x"]])
            with pytest.raises(AssertionError, match="narrows with"):
                _legs_including_markerless("synthetic", [[*base, f"{flag}=x"]])
        for attached in ("-kfoo", "-otestpaths=tests/unit", "--override-ini=testpaths=x"):
            with pytest.raises(AssertionError, match="narrows with"):
                _legs_including_markerless("synthetic", [[*base, attached]])
        for override in _OVERRIDE_FLAGS:
            with pytest.raises(AssertionError, match="narrows with"):
                _legs_including_markerless("synthetic", [[*base, override, "testpaths=x"]])
    # `-o` overriding anything else is not a selection; `-p no:cacheprovider` is a plugin.
    assert _legs_including_markerless(
        "synthetic", [[*marked, "-o", "log_cli=true", "-p", "no:cacheprovider"]]
    )


@pytest.mark.timeout(LANE_LEG_TIMEOUT_S)
@pytest.mark.xdist_group("lane_leg_membership")
def test_every_collected_test_is_selected_by_a_release_stage(collect_marker_sets) -> None:
    """The census.

    If this reddens, a lane or a marker changed — never add to
    _RELEASE_EXEMPT_LANES without the owner. Hermetic collection only:
    venue/plugin-only items (bed cells, browser engines) are covered by
    `_REQUIRED_LANES`, not here — see STATED BOUNDS at the top of this section.
    """
    gaps = _release_census_gaps(RELEASE_LANES + _RELEASE_EXEMPT_LANES, collect_marker_sets)
    assert not gaps, (
        f"{len(gaps)} collected test(s) are selected by NO stage of `make release` "
        "and by no owner exemption (roots and -m of every row considered):\n  "
        + "\n  ".join(gaps[:60])
        + ("\n  …" if len(gaps) > 60 else "")
    )


@pytest.mark.timeout(LANE_LEG_TIMEOUT_S)
@pytest.mark.xdist_group("lane_leg_membership")
def test_the_exemptions_are_live_and_not_in_the_release(collect_marker_sets) -> None:
    """Each exempt row covers tests the release rows do not, and no release stage reaches it.

    The first half keeps the list honest in one direction (a row that selects
    nothing the release misses is dead weight, or the lane was folded back in
    and the row now hides that); the second keeps it honest in the other (an
    exemption for a lane `make release` runs anyway is a stale lie — drop the
    row). The chaos rows and the cut sweep's row are the only ones today; the
    reasons are beside them.
    """
    assert _RELEASE_EXEMPT_LANES, "no exemptions — delete this test with the list"
    release_gaps = set(_release_census_gaps(RELEASE_LANES, collect_marker_sets))
    for row in _RELEASE_EXEMPT_LANES:
        with_row = (*RELEASE_LANES, row)
        covered = release_gaps - set(_release_census_gaps(with_row, collect_marker_sets))
        assert covered, f"exempt row {row} selects nothing the release rows miss; drop it"
    reached = set(release_legs())
    stale = [str(row) for row in _RELEASE_EXEMPT_LANES if row in reached]
    assert not stale, f"exempt rows `make release` reaches anyway: {stale}"


@pytest.mark.timeout(LANE_LEG_TIMEOUT_S)
@pytest.mark.xdist_group("lane_leg_membership")
def test_the_exempt_rows_are_exactly_the_chaos_and_cut_sweep_targets_legs(
    collect_marker_sets,
) -> None:
    """The exemption is spelled from the real lanes, and it covers the chaos tree and the sweep.

    Walking `make chaos`, `make chaos-embedded` and `make stability-cut-sweep`
    the way the release is walked must give exactly the exempt rows: a
    narrowed `-m` in the noxfile, or a chaos session moved elsewhere, reds
    here instead of silently leaving tests in no lane. And what the exempt
    rows add to the release's selection is every item collected under
    tests/e2e/chaos plus the cut sweep's module, no more and no fewer.
    """
    exempt_targets = ("chaos", "chaos-embedded", "stability-cut-sweep")
    text = (_REPO / _MAKEFILE).read_text()
    reached_by_release = make_reachable("release", text)
    for target in exempt_targets:
        assert target not in reached_by_release, f"`make release` reaches exempt `{target}`"
    exempt_legs = {leg for target in exempt_targets for leg in make_legs(target)}
    assert exempt_legs == set(_RELEASE_EXEMPT_LANES), (
        "the exempt rows and the exempt targets' legs differ:\n  "
        + "\n  ".join(sorted(str(leg) for leg in exempt_legs ^ set(_RELEASE_EXEMPT_LANES)))
    )
    release_only = set(_release_census_gaps(RELEASE_LANES, collect_marker_sets))
    with_exempt = set(
        _release_census_gaps(RELEASE_LANES + _RELEASE_EXEMPT_LANES, collect_marker_sets)
    )
    covered = release_only - with_exempt
    exempt_items = {
        item.nodeid for item in collect_marker_sets(("tests/e2e/chaos", _CUT_SWEEP_MODULE))
    }
    assert covered == exempt_items, sorted(covered ^ exempt_items)[:10]


@pytest.mark.timeout(LANE_LEG_TIMEOUT_S)
@pytest.mark.xdist_group("lane_leg_membership")
def test_the_release_census_reddens_on_a_dropped_lane(collect_marker_sets) -> None:
    """Positive control: without the tunnel soak's row its tests are gaps; with every row, none."""
    every_row = RELEASE_LANES + _RELEASE_EXEMPT_LANES
    tunnel = [row for row in every_row if row.roots == ("tests/e2e/tunnel_stability",)]
    assert tunnel, "the manifest no longer holds the stability-tunnel leg"
    without = tuple(row for row in every_row if row not in tunnel)
    gaps = _release_census_gaps(without, collect_marker_sets)
    assert gaps, "dropping the tunnel row left no gap — the census cannot see the lane"
    assert all(gap.startswith("tests/e2e/tunnel_stability/") for gap in gaps), gaps[:5]
    assert _release_census_gaps(every_row, collect_marker_sets) == []


@pytest.mark.timeout(LANE_LEG_TIMEOUT_S)
@pytest.mark.xdist_group("lane_leg_membership")
def test_the_family_runs_on_the_primary_interpreter_only_in_the_release_rows(
    collect_marker_sets,
) -> None:
    """Every `interpreter_agnostic` item is selected by a primary row and by no
    `noxfile.py:…@<other>` row: the family runs once per gate set, on the primary (#566 Q2).

    The primary rows are any `noxfile.py:…@3.10` row and the `Makefile:coverage-python`
    rows: since `make release` trusts the coverage stamp in place of the primary's nox
    session, `make coverage` is the release's primary run, and the stamp refuses one
    made under any interpreter but `$(NOX_PRIMARY)` (scripts/coverage_stamp.py)."""
    nox_rows = [row for row in RELEASE_LANES if row.surface.startswith("noxfile.py:")]
    primary_rows = tuple(
        row
        for row in RELEASE_LANES
        if row.surface == "Makefile:coverage-python"
        or (row.surface.startswith("noxfile.py:") and row.surface.endswith("@3.10"))
    )
    other_rows = tuple(row for row in nox_rows if not row.surface.endswith("@3.10"))
    assert primary_rows, (
        "no primary row (neither `Makefile:coverage-python` nor `noxfile.py:…@3.10`) in "
        f"RELEASE_LANES: {[str(r) for r in RELEASE_LANES]}"
    )
    assert other_rows, [str(r) for r in nox_rows]
    family = [
        item.nodeid for item in collect_marker_sets(()) if "interpreter_agnostic" in item.markers
    ]
    assert family, "no family item collected (guard misparse?)"
    # One census pass per row set (each walks the whole tree), not one per item.
    primary_gaps = set(_release_census_gaps(primary_rows, collect_marker_sets))
    other_gaps = set(_release_census_gaps(other_rows, collect_marker_sets))
    not_on_primary = [n for n in family if n in primary_gaps]
    assert not not_on_primary, not_on_primary[:10]
    selected_elsewhere = [n for n in family if n not in other_gaps]
    assert not selected_elsewhere, selected_elsewhere[:10]


# ---------------------------------------------------------------------------
# The bed lease: every bed target takes it first and releases it last
# ---------------------------------------------------------------------------
#
# scripts/bed_lease.py (spec 2026-11-01 bed-lease-design). The scanner reads the
# RAW recipe text: `makefile_live_recipe` expands nothing, so the pins match the
# literal `$(call bed_acquire,<target>)` and `$(call bed_release)` a recipe
# spells, and the macro definitions are pinned beside them (a macro emptied to
# nothing would otherwise satisfy the literal scan).

LAB_TOOLS = ["vm-health", "qemu-restart", "console-logout"]
"""The console and guest probes: leased (they touch the bed), never reaped (they are the reap's)."""
BED_TARGETS_DIRECT = [
    "coverage-python",
    "coverage-integration",
    "coverage-unix",
    "coverage-embedded",
    "coverage-bed",
    "conformance-bed",
    "kmodcov",
    "stability-unix",
    "stability-tunnel",
    "stability-embedded",
    "console-release-bed",
    "chaos",
    "chaos-embedded",
    "nox-integration",
    "nox-unix",
    "nox-embedded",
    "nox",
    "nox-release",
    "nox-full",
    "repeat",
    "docs-captures",
    "docs-captures-check",
    "vm-health",
    "qemu-restart",
    "console-logout",
]
"""Targets whose recipe touches the bed itself: a suite, or a probe of the consoles and guests."""
BED_TARGETS_AGGREGATORS = [
    "release",
    "release-campaign",
    "release-matrix",
    "release-kmodcov-matrix",
    "stability",
    "all",
]
"""Targets that run bed targets with `$(MAKE)`: they hold the lease across them."""
LEASE_INSIDE = ["gate-tip", "land"]
"""Targets whose script takes the lease itself, for the bed lanes it runs.

Pinned in test_gate_tip.py.
"""
LEASE_TOOLS = ["bed-status", "bed-lease", "bed-release"]
"""The lease's own verbs: they read or hold the lease, they do not take it around a lane."""
REAPED_TARGETS = [t for t in BED_TARGETS_DIRECT if t not in LAB_TOOLS]
"""The direct lanes that run a suite: reaped after the acquire and again before the release."""
HOSTLESS_TARGETS = [
    # the pinned and hostless suites and gates
    "coverage",
    "validate-python",
    "coverage-unit",
    "coverage-hostless",
    "coverage-hostless-fold",
    "coverage-stamp-start",
    "coverage-ts",
    "coverage-ts-unit",
    "unit-outliers",
    "nox-unit",
    "nox-hostless",
    "nox-unit-repeat",
    "quick-guards",
    "collect-check",
    "conformance",
    "busybox",
    "busybox-cache",
    "busybox-drift",
    "busybox-preflight",
    "dashboard",
    "dashboard-all",
    "dashboard-soak",
    "stability-unit",
    "stability-cut-sweep",
    "ci",
    "check-breaking",
    "check-python",
    "check-ts",
    "check",
    "check-api-snapshot",
    "check-api-teaching",
    "check-api-keywords",
    "api-snapshot",
    "api-surface-report",
    "gate-scope",
    "gate-status",
    "lock-check",
    "lint",
    "lint-arch",
    "lint-python",
    "lint-ts",
    "format",
    "format-python",
    "format-ts",
    "typecheck",
    "typecheck-python",
    "typecheck-ts",
    "validate",
    "validate-ts",
    "test-ts",
    "profile",
    "import-snapshot",
    "budget-delta",
    # docs: built from the tree and the committed captures; the media capture runs a
    # fake collector, the captures' bed refresh is docs-captures (leased, above)
    "docs",
    "docs-html",
    "docs-lint",
    "docs-inventories",
    "docs-media",
    "doctest",
    "doctest-src",
    # build, release bookkeeping and the web app
    "build",
    "clean",
    "clean-dist",
    "clean-web-assets",
    "dev",
    "help",
    "changelog",
    "release-preflight",
    "release-tag",
    "wheel-check",
    "wheel-smoke",
    "kmodcov-matrix",
    "support-matrix",
    "schema",
    "monitor-fixtures",
    "browsers",
    "web",
    "web-dev",
    "web-install",
    # a ping sweep of the lab's addresses: it logs into nothing
    "lab-reachability",
]
"""Targets that never touch the bed: no lease line, pinned so a copy-paste cannot add one.

Every Makefile target is in exactly one of the four lists (a test below), so a new
target must be classified before it exists: an unclassified one fails the census.
"""
QUICK_CLASSIFIER = " --unless-all-under tests/unit -- $(PATHS)"
"""`make quick` leases only when PATHS names a bed tree: the script's one classifier."""


def _bed_lines(target: str) -> "list[str]":
    return [line.strip() for line in makefile_live_recipe(_MAKEFILE_TEXT, target)]


_MAKEFILE_TEXT = (CHECKOUT_ROOT / "Makefile").read_text()


@pytest.mark.parametrize("target", BED_TARGETS_DIRECT + BED_TARGETS_AGGREGATORS)
def test_every_bed_target_takes_the_lease_first_and_releases_it_last(target: str) -> None:
    lines = _bed_lines(target)
    assert lines, f"{target} has no live recipe line"
    assert lines[0] == f"$(call bed_acquire,{target})", (target, lines[0])
    assert lines[-1] == "$(call bed_release)", (target, lines[-1])
    assert len(lines) > 2, f"{target} leases nothing: only the lease lines are left"


@pytest.mark.parametrize("target", REAPED_TARGETS)
def test_every_suite_lane_reaps_after_its_acquire_and_before_its_release(target: str) -> None:
    """scripts/bed_reap.py runs before the suite, and again after a green one."""
    lines = _bed_lines(target)
    assert lines[1] == "$(call bed_reap)", (target, lines[1])
    assert lines[-2] == "$(call bed_reap)", (target, lines[-2])
    assert lines.count("$(call bed_reap)") == 2, (target, lines)


@pytest.mark.parametrize("target", LAB_TOOLS + BED_TARGETS_AGGREGATORS)
def test_the_probes_and_the_holders_lease_without_reaping(target: str) -> None:
    """A probe is the reap's own step; a holder's lanes reap for themselves."""
    assert "$(call bed_reap)" not in _bed_lines(target), target


@pytest.mark.parametrize("target", HOSTLESS_TARGETS + LEASE_TOOLS)
def test_no_hostless_target_takes_the_lease(target: str) -> None:
    lines = _bed_lines(target)
    assert not [line for line in lines if "bed_" in line], (target, lines)


def test_quick_leases_and_reaps_only_for_a_bed_path() -> None:
    """The guard line first (a missing PATHS is an error before any lease), then the lease."""
    lines = _bed_lines("quick")
    assert "$(error quick: PATHS is required" in lines[0], lines[0]
    assert lines[1] == f"$(call bed_acquire,quick){QUICK_CLASSIFIER}", lines[1]
    assert lines[2] == f"$(call bed_reap){QUICK_CLASSIFIER}", lines[2]
    assert lines[-2] == f"$(call bed_reap){QUICK_CLASSIFIER}", lines[-2]
    assert lines[-1] == f"$(call bed_release){QUICK_CLASSIFIER}", lines[-1]


def test_the_hand_reap_is_the_full_reap_under_the_lease() -> None:
    """`make bed-reap` after a killed or interrupted run: the one target that passes --full."""
    lines = _bed_lines("bed-reap")
    assert lines == [
        "$(call bed_acquire,bed-reap)",
        "$(call bed_reap) --full $(if $(RESTART_QEMU),--restart-qemu)",
        "$(call bed_release)",
    ]
    assert "--full" not in "".join(
        line for t in [*REAPED_TARGETS, "quick"] for line in _bed_lines(t) if "bed_reap" in line
    ), "a lane's reap is the quick one: the console step is the hand reap's"


def test_the_lease_macros_are_what_the_recipes_rely_on() -> None:
    """The literal scan above is only as good as the macros it names."""
    text = _MAKEFILE_TEXT
    assert "\nLAB_FILE := tests/_fixtures/lab_data/tech1/lab.json\n" in text
    assert "\nBED_LEASE := uv run python scripts/bed_lease.py --lab $(LAB_FILE)\n" in text
    assert '\nbed_acquire = @$(BED_LEASE) acquire --pid $$PPID --purpose "$(1)"\n' in text
    assert "\nbed_release = @$(BED_LEASE) release --pid $$PPID\n" in text
    assert "\nBED_REAP := uv run python scripts/bed_reap.py --lab $(LAB_FILE)\n" in text
    assert "\nbed_reap = @$(BED_REAP) --pid $$PPID\n" in text
    assert "tests/_fixtures/lab_data/tech1/lab.json" not in text.replace(
        "LAB_FILE := tests/_fixtures/lab_data/tech1/lab.json", ""
    ), "the lab file is spelled once, as LAB_FILE"


_MAKE_BUILTIN_VARIABLES = frozenset({"MAKE", "CURDIR", "MAKEFILE_LIST", "MAKECMDGOALS"})
_MAKE_DIRECTIVES = (
    "ifeq", "ifneq", "ifdef", "ifndef", "else", "endif", "include", "-include",
    "define", "endef", "export", "unexport", ".",
)  # fmt: skip


def makefile_rules_naming_an_undefined_variable(text: str) -> "list[str]":
    """Rule lines whose targets or prerequisites name a `$(VAR)` no earlier line defines.

    Make expands a rule's targets and prerequisites when it reads the rule, so a
    variable assigned further down is empty there and the prerequisite silently
    vanishes: `coverage-bed: $(COVAPP_DIST)` written above `COVAPP_DIST :=` built
    no bundle (#797, found in review). Recipe lines (a leading tab) expand at run
    time and are not read; a target-specific variable assignment (`target: VAR :=`)
    is not a prerequisite list.
    """
    defined = set(_MAKE_BUILTIN_VARIABLES)
    offenders: "list[str]" = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if line.startswith(("\t", "#")) or not line.strip():
            continue
        assigned = re.match(
            r"^(?:export\s+)?(?:override\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*(?::::?|:|\?|\+)?=", line
        )
        if assigned:
            defined.add(assigned.group(1))
            continue
        if line.startswith(_MAKE_DIRECTIVES):
            continue
        rule = re.match(r"^([^:=]+?)\s*&?::?(?!=)\s*([^#]*)", line)
        if not rule:
            continue
        targets, prereqs = rule.groups()
        if "=" in prereqs:
            continue
        for var in re.findall(r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)", f"{targets} {prereqs}"):
            if var not in defined:
                offenders.append(
                    f"Makefile:{lineno}: $({var}) is not yet defined in {line.strip()!r}"
                )
    return offenders


def test_every_rule_names_only_variables_defined_above_it() -> None:
    assert makefile_rules_naming_an_undefined_variable(_MAKEFILE_TEXT) == []


def test_the_prerequisite_order_guard_sees_the_defect_it_was_written_for() -> None:
    late = "lane: $(DIST) ## help\n\t@echo\nDIST := x/index.html\nother: $(DIST)\n"
    assert makefile_rules_naming_an_undefined_variable(late) == [
        "Makefile:1: $(DIST) is not yet defined in 'lane: $(DIST) ## help'"
    ]
    early = "DIST := x/index.html\nlane: $(DIST)\n\t@echo $(LATER)\nlane: export X := 1\n"
    assert makefile_rules_naming_an_undefined_variable(early) == []


def test_the_bed_clauses_are_the_makefile_s_coverage_bed_selection() -> None:
    """noxfile's `BED_CLAUSES` is the Makefile's `M_COVERAGE_BED`, expanded: one selection."""
    variables = _makefile_marker_variables(_MAKEFILE_TEXT)
    assert (
        variables["M_COVERAGE_BED"].replace("$(M_COVERAGE)", variables["M_COVERAGE"]) == BED_CLAUSES
    )


@pytest.mark.timeout(LANE_LEG_TIMEOUT_S)
@pytest.mark.xdist_group("lane_leg_membership")
def test_the_canary_s_hostless_sessions_and_the_bed_leg_partition_tests_all(
    collect_marker_sets,
) -> None:
    """The release's bed leg plus CI's hostless sessions select what `tests_all` did.

    On the canary `tests_all` ran every product test minus the family; the
    release now runs only the bed half (`tests_bed`) and trusts CI's
    `tests_hostless_unit` (middle pair) and `tests_hostless_e2e` (canary pair)
    for the rest. Equality over one real collection proves no test fell out.
    """
    items = collect_marker_sets(tuple(pyproject_testpaths(_REPO)))
    whole = _selected_node_ids(
        [_Leg("a", _ALL_NONPRIMARY, ()), _Leg("a", _ALL_NONPRIMARY_SERIAL, ())], items
    )
    assert whole, "tests_all selects nothing, so this compares nothing"
    parts = _selected_node_ids(
        [
            _Leg("u", _HOSTLESS_MIDDLE, ("tests/unit",)),
            _Leg("u", _HOSTLESS_MIDDLE_SERIAL, ("tests/unit",)),
            _Leg("e", _HOSTLESS_E2E_CANARY, ("tests/e2e",)),
            _Leg("e", _HOSTLESS_E2E_CANARY_SERIAL, ("tests/e2e",)),
            _Leg("b", _BED_NONPRIMARY, ()),
            _Leg("b", _BED_NONPRIMARY_SERIAL, ()),
        ],
        items,
    )
    assert parts == whole, {"only in tests_all": whole - parts, "only in the union": parts - whole}


def test_the_coverage_bed_lane_is_the_bed_half_of_the_coverage_selection() -> None:
    """`coverage-bed` and the hostless CI slice partition `M_COVERAGE`, textually (#797).

    The bed lane selects `M_COVERAGE_BED` = `(integration or embedded) and
    $(M_COVERAGE)`; the hostless slice selects `not integration and not
    embedded and` the same body, in two spellings: the Makefile's `M_HOSTLESS`
    and the noxfile's `HOSTLESS_CLAUSES`, which the pull request's three hostless
    sessions run on the primary Python (:func:`primary_ci_legs`). Together they
    are the coverage selection and they share no item, so a src-leaf tip that
    runs the bed lane and lets the PR's CI run the hostless slice has run every
    test `make coverage` would, once. The roots differ — the bed lane runs over
    `testpaths`, the hostless slice over `tests/unit tests/e2e` — and the
    partition still holds because every `tests/integration` item is
    integration-marked by its conftest (the bed's) and `tests/busybox` and
    `tests/conformance` are excluded by marker on both sides. The three legs
    together select what the former single primary pair did — pinned by
    `test_the_three_primary_legs_select_what_the_former_primary_pair_did`. The bed lane is
    uninstrumented on both legs (`--no-cov`: a floor-less pass/fail lane that
    leaves no coverage stamp) and its twin is the `-n0` leg. The collected-set
    check was run once at the landing (the spec's measurement); this is the
    standing proof, over the spellings.
    """
    variables = _makefile_marker_variables(_MAKEFILE_TEXT)
    body = variables["M_COVERAGE"]
    hostless = f"not integration and not embedded and {body}"
    assert variables["M_HOSTLESS"] == hostless
    assert variables["M_COVERAGE_BED"] == f"(integration or embedded) and {body}"
    ci = primary_ci_legs()
    by_session = {
        session: [leg for leg in ci if leg.surface == f"noxfile.py:{session}@3.10"]
        for session in _PRIMARY_CI_SESSIONS
    }
    unit, e2e, guards = (by_session[session] for session in _PRIMARY_CI_SESSIONS)
    family = "interpreter_agnostic"
    assert [leg.expr for leg in unit] == [
        f"{hostless} and not serial_timing and not {family}",
        f"serial_timing and {hostless} and not {family}",
    ], unit
    assert [leg.expr for leg in e2e] == [
        f"{hostless} and not serial_timing",
        f"serial_timing and {hostless}",
    ], e2e
    assert [leg.expr for leg in guards] == [f"{hostless} and {family}"], guards
    assert all(leg.roots == ("tests/unit",) for leg in (*unit, *guards)), ci
    assert all(leg.roots == ("tests/e2e",) for leg in e2e), ci
    # The family is selected by exactly the guards leg and deselected by the unit legs.
    clauses = [{c.strip() for c in leg.expr.split(" and ")} for leg in ci]
    assert [leg for leg, c in zip(ci, clauses, strict=True) if family in c] == guards, ci
    assert [leg for leg, c in zip(ci, clauses, strict=True) if f"not {family}" in c] == unit, ci
    legs = make_legs("coverage-bed")
    assert [leg.expr for leg in legs] == [
        f"(integration or embedded) and {body} and not serial_timing",
        f"serial_timing and (integration or embedded) and {body}",
    ], legs
    assert all(leg.roots == () for leg in legs), "marker-selected over the whole tree"
    live = makefile_live_recipe(_MAKEFILE_TEXT, "coverage-bed")
    recipe = _makefile_pytest_invocations("\n".join(live))
    assert len(recipe) == 2, recipe
    assert all("--no-cov" in argv for argv in recipe), recipe
    assert "-n0" in recipe[1], recipe
    assert "-n0" not in recipe[0], recipe


def test_the_oracle_names_no_bed_lane_the_makefile_does_not_lease() -> None:
    """Every target gate_scope hands a tip or a full commit column carries the lease or is
    pinned hostless; every bed goal leases, and the static goals take no lease."""
    named = {line.split()[1] for lanes in gs.BED_LANES.values() for line in lanes}
    named |= {line.split()[1] for line in gs.FULL_GATE if line.startswith("make ")}
    named |= {line.split()[1] for line in gs.TEST_SUITE_LANES}
    named |= set(gs.BED_GOALS)
    leased = set(BED_TARGETS_DIRECT + BED_TARGETS_AGGREGATORS)
    unleased = sorted(t for t in named if t not in leased and t not in HOSTLESS_TARGETS)
    assert not unleased, f"oracle lanes neither leased nor pinned hostless: {unleased}"
    assert set(gs.BED_GOALS) <= leased, sorted(set(gs.BED_GOALS) - leased)
    assert set(gs.STATIC_GOALS) <= set(HOSTLESS_TARGETS), sorted(
        set(gs.STATIC_GOALS) - set(HOSTLESS_TARGETS)
    )


def test_every_makefile_target_is_classified_exactly_once() -> None:
    """The census is exhaustive: a target the lists do not name fails here, before it runs."""
    every = [
        *BED_TARGETS_DIRECT,
        *BED_TARGETS_AGGREGATORS,
        *LEASE_INSIDE,
        *HOSTLESS_TARGETS,
        *LEASE_TOOLS,
        "quick",
        "bed-reap",
    ]
    assert len(every) == len(set(every)), "a target in two lists"
    # a rule's name, not a variable's (`X := ...`) nor make's own dot-targets
    targets = set(re.findall(r"^([A-Za-z0-9_-]+)\s*:(?!=)", _MAKEFILE_TEXT, re.MULTILINE))
    assert sorted(targets - set(every)) == [], "Makefile targets the census does not classify"
    assert sorted(set(every) - targets) == [], "census entries that are not Makefile targets"


# The single primary pair the hostless sessions replaced, spelled out as LITERALS
# (never read from the noxfile) so the comparison below has a fixed baseline.
_FORMER_PRIMARY_LEGS = (
    _Leg(
        "noxfile.py:tests_hostless@3.10",
        "not integration and not embedded and not stability and not browser "
        "and not busybox and not conformance and not kmodcov and not serial_timing",
        ("tests/unit", "tests/e2e"),
    ),
    _Leg(
        "noxfile.py:tests_hostless@3.10",
        "serial_timing and not integration and not embedded and not stability and not browser "
        "and not busybox and not conformance and not kmodcov",
        ("tests/unit", "tests/e2e"),
    ),
)


@pytest.mark.timeout(LANE_LEG_TIMEOUT_S)
@pytest.mark.xdist_group("lane_leg_membership")
def test_the_three_primary_legs_select_what_the_former_primary_pair_did(
    collect_marker_sets,
) -> None:
    """Splitting the hostless session per tier moved no test in or out of the primary run.

    Both selections are evaluated over one real collection of `tests/unit` and
    `tests/e2e`; the mutation control drops the guards leg, and the union then
    misses the `interpreter_agnostic` family.
    """
    items = collect_marker_sets(("tests/unit", "tests/e2e"))
    former = _selected_node_ids(_FORMER_PRIMARY_LEGS, items)
    assert former, "the former pair selects nothing, so this compares nothing"
    current = primary_ci_legs()
    assert _selected_node_ids(current, items) == former
    without_guards = [leg for leg in current if "tests_hostless_guards" not in leg.surface]
    assert len(without_guards) == len(current) - 1, current
    missed = former - _selected_node_ids(without_guards, items)
    assert missed, "dropping the guards leg must lose the family"
    family = {item.nodeid for item in items if "interpreter_agnostic" in item.markers}
    assert missed <= family, sorted(missed - family)[:20]
