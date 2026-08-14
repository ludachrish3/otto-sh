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
``Makefile``. Two further ``--override-ini addopts=`` sites live in PRODUCT
code (``src/otto/suite/run.py``, ``src/otto/config/repo.py``) and re-created
the same exposure for otto's own in-process pytest sessions — the recorded
live defect "otto test panics when tach is installed"
(todo/churn-review-cheap-items-followups.md). There the guard is not an
addopts value but direct ``"-p", "no:tach"`` argv elements (matching
repo.py's existing ``-p no:terminal`` idiom), so those files get the
companion literal pin below rather than the scanner.

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

A third family (the ``_Leg`` block, below the serial_timing pins) asks the
only lane-leg question the other two cannot: does the leg select ANYTHING?
Both scanners above audit leg TEXT; neither can see MEMBERSHIP, and an empty
leg is a hard failure — pytest exits 5 when it collects nothing and make
aborts on that, after paying for every leg ahead of it in the recipe. That
gate decides membership from real pytest collection, in a subprocess, because
a static model of this repo's markers reported a live lane empty; the
reasoning is in
:func:`test_every_lane_leg_selects_at_least_one_test`.
"""

import ast
import dataclasses
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from _pytest.mark.expression import Expression

from tests._fixtures.paths import PROJECT_ROOT
from tests.unit.test_tier_marker_invariants import (
    _expand_makefile_variables,
    _makefile_marker_variables,
    _makefile_pytest_invocations,
    _python_pytest_invocations,
    _selected_roots,
)

_REPO = PROJECT_ROOT

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
    """The two in-product pytest sessions must pass ``-p no:tach`` as argv.

    Both sites clear ``addopts`` (dropping pyproject's guard) before starting
    an in-process pytest session, so each must re-assert the guard itself.
    The pin requires the *adjacent argv pair* ``"-p", "no:tach"`` — a prose
    mention in a comment (the plausible shape of an annotated removal, the
    same trap this module's scanner was reviewed for) and a stray quoted
    string elsewhere in the file both miss it. A commented-out copy of the
    exact pair on one line would still match; that shape is accepted as
    vanishingly unlikely rather than chased.
    """
    for rel in ("src/otto/suite/run.py", "src/otto/config/repo.py"):
        text = (_REPO / rel).read_text()
        assert re.search(r'"-p",\s*"no:tach",', text), (
            f"{rel}: in-process pytest session lost its `-p no:tach` argv guard "
            "(issue #193: tach's pytest plugin panics otto-started sessions; "
            "`addopts=` overrides at these sites drop the pyproject guard)"
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
    ),
    "tests/unit/host/test_session.py": ("test_recovery_timeout_rebind_is_live",),
    # Fourth sighting of the class, caught by this wave's own gates run: the
    # relative serial-vs-parallel docker exec discriminator false-failed when
    # sibling-worker load landed inside its parallel measurement window.
    "tests/integration/test_docker_run_get_put.py": ("test_exec_remains_concurrent_safe",),
}


def _is_commented_recipe(line: str) -> bool:
    """A recipe line make hands to the shell as a no-op comment.

    ``\\t# ...`` and ``\\t@# ...`` both execute as a shell comment — the leg
    "exists" textually but never runs. The review probe that motivated this
    (fable, W17c): prefixing the real coverage-python serial leg with ``\\t# ``
    left the scanner green while the discriminators silently left the gate.
    """
    body = line.lstrip("\t").lstrip()
    if body.startswith("@"):
        body = body[1:].lstrip()
    return body.startswith("#")


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
        live = [line for line in block if not _is_commented_recipe(line)]
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
    inside the same session. Module-level: the one sanctioned shape is the
    ``HOSTLESS_TEST_ARGS`` / ``HOSTLESS_SERIAL_ARGS`` constant pair, both run
    by one session — anything else module-level is reported so a novel shape
    extends this pin instead of sliding past it. Comment lines are dropped
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
    hostless_span = assign_ranges.get("HOSTLESS_TEST_ARGS")
    stray = [
        lineno
        for lineno in module_hits
        if hostless_span is None or not hostless_span[0] <= lineno <= hostless_span[1]
    ]
    gaps.extend(
        f"line {lineno}: module-level serial_timing exclusion outside "
        "HOSTLESS_TEST_ARGS — extend this pin for the new shape"
        for lineno in stray
    )
    if len(stray) < len(module_hits):  # HOSTLESS_TEST_ARGS carries an exclusion
        serial_span = assign_ranges.get("HOSTLESS_SERIAL_ARGS")
        if serial_span is None:
            gaps.append(
                "HOSTLESS_TEST_ARGS excludes serial_timing but HOSTLESS_SERIAL_ARGS is gone"
            )
        else:
            serial_segment = live_segment(serial_span[0], serial_span[1])
            if _SERIAL_LEG_TOKEN not in serial_segment or '"-n0"' not in serial_segment:
                gaps.append(
                    "HOSTLESS_SERIAL_ARGS lost its positive serial_timing expression or -n0"
                )
            if not any(
                "HOSTLESS_TEST_ARGS" in segment and "HOSTLESS_SERIAL_ARGS" in segment
                for segment in def_segments.values()
            ):
                gaps.append("no session runs both HOSTLESS_TEST_ARGS and HOSTLESS_SERIAL_ARGS")
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
        'HOSTLESS_TEST_ARGS = ("-m", "not stability and not serial_timing")\n'
        'HOSTLESS_SERIAL_ARGS = ("-m", "serial_timing and not stability", "-n0")\n'
        "def tests_hostless(session):\n"
        '    session.run("pytest", *HOSTLESS_TEST_ARGS)\n'
        '    session.run("pytest", *HOSTLESS_SERIAL_ARGS)\n'
    )
    assert noxfile_serial_lane_gaps(pair) == []
    no_serial_const = 'HOSTLESS_TEST_ARGS = ("-m", "not stability and not serial_timing")\n'
    assert noxfile_serial_lane_gaps(no_serial_const) == [
        "HOSTLESS_TEST_ARGS excludes serial_timing but HOSTLESS_SERIAL_ARGS is gone"
    ]
    unused_pair = pair.replace('    session.run("pytest", *HOSTLESS_SERIAL_ARGS)\n', "")
    assert noxfile_serial_lane_gaps(unused_pair) == [
        "no session runs both HOSTLESS_TEST_ARGS and HOSTLESS_SERIAL_ARGS"
    ]
    stray = 'STRAY_ARGS = ("-m", "not stability and not serial_timing")\n'
    (gap,) = noxfile_serial_lane_gaps(stray)
    assert "extend this pin" in gap  # novel module-level shapes are loud, never green


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


# ── lane-leg membership ─────────────────────────────────────────────────────
# The scanners above audit leg TEXT. This block audits leg MEMBERSHIP: does
# the leg select anything at all? Kept in one place because every piece of it
# — the inventory, the collection subprocess, the expression evaluator — is
# load-bearing for the one assertion in
# `test_every_lane_leg_selects_at_least_one_test`.

# The three surfaces that spell pytest lanes, the same set
# `test_no_lane_but_the_busybox_lane_can_select_the_busybox_tier` covers. A
# lane written straight into a GitHub workflow step is NOT seen here; that is
# the SURFACE BOUND `test_a_lane_that_selects_by_path_cannot_reach_the_busybox_tier`
# states, and it names the live sightings.
_MAKEFILE = "Makefile"
_PYTHON_LANE_SURFACES = ("noxfile.py", "scripts/stability_campaign.py")

# NOT `OTTO_`-prefixed, and that is not a style choice: the root conftest
# strips ambient `OTTO_*` variables at IMPORT time (all but a named opt-in
# list — the hermetic-env rule). An `OTTO_`-named dump path is therefore gone
# by the time the dump hook reads it: the child collects, then dies with
# KeyError in an INTERNALERROR. Measured, while writing this.
_DUMP_ENV = "LANE_LEG_MARKER_DUMP"
_DUMP_PLUGIN_MODULE = "_lane_leg_marker_dump"

# Loaded with `-p` into the collecting child. `pytest_collection_finish` runs
# after every `pytest_collection_modifyitems` hook, which is the only moment
# all four of this repo's marker routes have been applied (see the gate's
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
            handle.write(",".join(sorted({mark.name for mark in item.iter_markers()})) + "\n")
'''

# Only speed and hygiene, never membership: `-n0` so one process sees every
# item (and no `@group` nodeid suffixes), no coverage, no cache writes.
# pyproject's `addopts` is NOT cleared — `--doctest-modules` adds items every
# real lane also collects, so a run without it would measure a tree no lane
# runs.
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


@dataclasses.dataclass(frozen=True)
class _Leg:
    """One lane leg: a marker expression AND the paths it is scoped to."""

    surface: str
    expr: str
    roots: "tuple[str, ...]"

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
        roots = tuple(str(path.relative_to(_REPO)) for path in _selected_roots(tokens))
        legs.append(_Leg(surface, expr, roots))
    return legs


def lane_legs() -> "list[_Leg]":
    """Every `-m` lane leg in the repo's three lane-spelling surfaces."""
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


def _marker_matcher(markers: "frozenset[str]"):
    """A pytest ``ExpressionMatcher`` over one collected item's marker names.

    Refuses the ``marker(kwarg=...)`` expression form loudly instead of
    guessing: no lane spells one today, and a gate that quietly answered
    "matches nothing" for a shape it does not model would report a live leg
    as empty (or, worse for the next reader, be believed).
    """

    def matches(name: str, /, **kwargs: object) -> bool:
        if kwargs:
            raise pytest.UsageError(
                f"marker expression uses the kwargs form `{name}(...)`, which this "
                "gate does not model — teach it the form rather than dropping the leg"
            )
        return name in markers

    return matches


def _collect_marker_sets(plugin_dir: Path, roots: "tuple[str, ...]") -> "list[frozenset[str]]":
    """Collect *roots* for real in a child pytest, and return each item's markers.

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
    return [
        frozenset(name for name in line.split(",") if name)
        for line in dump.read_text(encoding="utf-8").splitlines()
    ]


@pytest.fixture(scope="session")
def collect_marker_sets(tmp_path_factory: pytest.TempPathFactory):
    """Memoised real collection, keyed by path set.

    Session-scoped and memoised because a collection costs seconds and the
    legs share only a handful of distinct path sets; the tests that use it
    share one ``xdist_group`` so a distributed run pays for one worker's
    collections, not several workers' duplicates.
    """
    plugin_dir = tmp_path_factory.mktemp("lane_leg_collect")
    (plugin_dir / f"{_DUMP_PLUGIN_MODULE}.py").write_text(_MARKER_DUMP_PLUGIN, encoding="utf-8")
    collected: "dict[tuple[str, ...], list[frozenset[str]]]" = {}

    def collect(roots: "tuple[str, ...]") -> "list[frozenset[str]]":
        if roots not in collected:
            collected[roots] = _collect_marker_sets(plugin_dir, roots)
        return collected[roots]

    return collect


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
        collected = collect(leg.roots)
        if not collected:
            offenders.append(f"{leg} — those paths collect no tests at all")
            continue
        try:
            hits = sum(1 for markers in collected if compiled.evaluate(_marker_matcher(markers)))
        except pytest.UsageError as exc:
            offenders.append(f"{leg} — this gate cannot evaluate the expression: {exc}")
            continue
        if not hits:
            offenders.append(f"{leg} — selects 0 of the {len(collected)} tests collected there")
    return offenders


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


def test_the_lane_leg_inventory_sees_every_surface() -> None:
    """The scan finds legs on all three surfaces, and recovers each `-m` value.

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
