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
"""

import ast
import re
import sys

import pytest

from tests._fixtures.paths import PROJECT_ROOT

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
