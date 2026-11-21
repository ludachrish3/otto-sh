"""Nox sessions for otto-sh.

Run all default sessions:
    uv run nox

Run a single Python version's tests:
    uv run nox -s tests_unit-3.10

List available sessions:
    uv run nox --list
"""

import atexit
import hashlib
import os
import platform
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import nox
import nox_uv
from packaging.tags import sys_tags
from packaging.utils import canonicalize_name

PYTHON_VERSIONS = ["3.10", "3.11", "3.12", "3.13", "3.14"]

# The single-interpreter ("pinned") Python for sessions that don't span the
# matrix. 3.10 — the oldest supported interpreter — deliberately: it is what
# the dev venv and the release/build CI jobs run, so single-version lanes
# exercise the floor rather than silently requiring something newer. The
# Makefile's NOX_PRIMARY mirrors this value (hand-kept pair — see
# DASHBOARD_MARKER_EXPR below for why Make can't read a Python constant).
PRIMARY_PYTHON = "3.10"

# The newest supported interpreter: the warning canary. With pytest's
# filterwarnings=error a warning only fails on versions that actually run the
# affected code, and the newest emits them first. The Makefile's NOX_CANARY
# mirrors this value (hand-kept, like PRIMARY_PYTHON).
CANARY_PYTHON = "3.14"

# The floor and the ceiling. The floor runs everything, instrumented (the
# HOSTLESS_UNIT, HOSTLESS_E2E and HOSTLESS_GUARDS bundles below); the ceiling
# runs the unit and e2e tiers with coverage off (HOSTLESS_MIDDLE_*_ARGS and
# HOSTLESS_E2E_CANARY_*_ARGS); the versions between run the unit tier only
# (HOSTLESS_MIDDLE_*_ARGS). Pinned to be exactly min/max of PYTHON_VERSIONS by
# tests/unit/test_python_matrix_tiering.py.
BOOKEND_PYTHONS = (PRIMARY_PYTHON, CANARY_PYTHON)

# JUnit XML is written into a per-target subdirectory of reports/junit/ named
# after the `make` target that drives the session (nox-unit, nox-unix,
# nox-embedded, nox), matching the layout the standalone Makefile test targets
# use. Each Python version is a separate pytest process, so the file inside the
# subdir keeps the full session name (e.g. reports/junit/nox-unit/tests_unit-3.10.xml)
# to avoid clobbering. With `--count=N` the repeats land in the same file, so a
# multi-run stability pass collects every failure for a given Python in one place.
JUNIT_DIR = Path("reports/junit")
# Where failed browser tests leave their Playwright artifacts. MUST match the
# Makefile's `--output reports/playwright` (`dashboard`, `dashboard-soak`) and
# the path CI's and nightly's `dashboard` jobs upload on failure; the Makefile
# can't read a Python constant, the same gap as DASHBOARD_MARKER_EXPR below.
PLAYWRIGHT_OUTPUT_DIR = Path("reports/playwright")


def _junitxml(session: nox.Session, group: str) -> str:
    """Return a `--junitxml=` arg pointing at reports/junit/<group>/<session>.xml."""
    return f"--junitxml={JUNIT_DIR / group / f'{session.name}.xml'}"


nox.options.default_venv_backend = "uv"
# `lint` runs ruff check + format --check; it is part of the default gate now
# that the strict config (select=ALL minus the deny-list) is green.
nox.options.sessions = [
    "lint",
    "tests_hostless_unit",
    "tests_hostless_e2e",
    "tests_hostless_guards",
    "typecheck",
    "docs",
]

# Coverage floors: no floor lives in the noxfile. The hostless selection is
# three sessions (one per tier, below) whose instrumented primary legs each
# write a partial report with `--cov-fail-under=0`. The hostless floor is the
# Makefile's CI_COVERAGE_THRESHOLD, judged by `make coverage-hostless-fold` on
# the fold of those three primary legs, never on any one session's slice.
# Every other leg runs with coverage OFF: the canary's job is "does otto
# still work on the newest
# interpreter", the interior's trimmed selection cannot meet a floor, and
# tests_all's primary leg is the selection `make coverage` already gates on
# the same tree — the coverage stamp (scripts/coverage_stamp.py) is the
# release's evidence of that run. Instrumenting a leg whose report nothing
# reads costs it ~14 % of wall time (the canary session measured 552 s with
# coverage, 473 s without) for no reader.

# `not busybox and not conformance` rides every catch-all selector below, for
# reasons unrelated to the bed and spelled out in full above the Makefile's
# M_HOSTLESS: the BusyBox artifact tier fetches ~5 MB from busybox.net and
# raises rather than skipping when it cannot, and the host-contract conformance
# suite stands up a throwaway loopback sshd and runs those artifacts as
# subprocesses. Both trees are in `testpaths` (they have to be — their lanes
# are path-less `-m` invocations) and neither carries a marker any `not X`
# clause here would otherwise catch, so without these clauses every default nox
# session selects them. `make busybox` and `make conformance` are their opt-in
# lanes.
# `not kmodcov` rides them too, and the positive `tests_unix`/`tests_embedded`
# selectors besides: the otto_kmodcov toolchain tier carries a resource marker
# of its own (`integration` for the bed matrix, `hostless` for the cross
# build), so no catch-all's other clauses deselect it — see the Makefile's
# `make kmodcov`, its opt-in lane.

# Arms the asyncio transport-leak detector's REPORTING on every hostless leg —
# the same token the Makefile's gate targets use, and deliberately the same
# value, so "which lanes are armed" stays one grep across both files.
#
# Reporting is all this buys, and that is the point. The registry itself
# (tests/_fixtures/_transport_leaks.py) is installed UNCONDITIONALLY by
# tests/conftest.py's pytest_configure, so every lane already pays to record
# transports at __init__ whether or not anyone reads the result. Armed, the
# per-test check iterates the live tracked transports — normally zero — and on
# the leak path only, prints the CREATING test and forces the gc that lands the
# ResourceWarning on it. Unarmed, that check returns before doing anything, and
# a leak instead surfaces as an unraisable warning on whichever unrelated test
# is running when the collector next fires.
#
# EVERY leg, not just the bookends, for two reasons. The cost is not
# measurable: tests/unit/host (2878 tests, the densest asyncio tree and where
# the exec-timeout transport leak fixed in dab13a7b lived) ran 20.65 s / 21.21 s
# unarmed against 20.29 s / 20.94 s armed on 2026-09-12 — armed was faster in
# both pairs, i.e. inside the noise. And "bookend-only" is a property of the
# current lane shape, not of the bug: the same leak class this is for was seen
# on tests_hostless_unit-3.11 (see _dashboard_harness._reap_orphaned_transports and
# its sibling regression test) back when the interior legs still ran tests/e2e.
#
# This can redden an interior leg that is green today, on a latent leak whose
# warning currently fires outside any test's unraisable window. That is the
# instrument working. Read the registry module's docstring before trusting a
# green either way: an UNREFERENCED transport is collected before the boundary
# and is attributed to nobody, so this flag catches a half, not the whole.
_LEAK_ENV = {"OTTO_DETECT_ASYNCIO_LEAKS": "1"}

# browser (Playwright) tests always run as their own pytest process — sync
# Playwright keeps an event loop running in the worker main thread for the
# whole session, which breaks pytest-asyncio tests that share the process.
# `dashboard` (below) is that dedicated process; every other session whose
# paths/markers could otherwise co-select browser + async tests in one
# pytest invocation (the hostless sessions, tests_all) excludes `browser`
# instead.
#
# serial_timing (wall-clock discriminators — sibling xdist workers can
# counterfeit their slow arm as a false red) is excluded from every parallel
# invocation and re-appended by a paired `-n0` leg in the same session —
# exclusion alone would make the tests CI-invisible. An instrumented serial
# leg folds into its parallel twin's report via --cov-append, so each
# session's report covers its whole run.
#
# HOSTLESS_CLAUSES is the ONE home of the hostless exclusion list: every
# hostless bundle below composes from it, so a resource exclusion added here
# reaches every leg, serial twins included — `not busybox and not conformance`
# on a serial leg is pinned for every paired leg in this file and the Makefile
# by tests/unit/test_lane_invariants.py (a serial leg that dropped them would
# fetch artifacts in a lane whose parallel half excludes them). The Makefile's
# M_HOSTLESS is the same text, held equal by that module's partition pin.
HOSTLESS_CLAUSES = (
    "not integration and not embedded and not stability and not browser "
    "and not busybox and not conformance and not kmodcov and not release_proof"
)

# BED_CLAUSES is the bed half of `make coverage`'s selection: the Makefile's
# M_COVERAGE_BED with $(M_COVERAGE) expanded, held equal to it by
# tests/unit/test_lane_invariants.py (the Makefile is the one site). The
# release's canary bed leg (tests_bed) composes from it.
BED_CLAUSES = (
    "(integration or embedded) and not stability and not browser and not busybox "
    "and not conformance and not kmodcov and not release_proof"
)
TESTS_BED_TEST_ARGS = (
    "-m",
    BED_CLAUSES + " and not serial_timing and not interpreter_agnostic",
    "--no-cov",
)
TESTS_BED_SERIAL_ARGS = (
    "-m",
    "serial_timing and " + BED_CLAUSES + " and not interpreter_agnostic",
    "-n0",
    "--no-cov",
)

# The hostless selection is three sessions, one per tier, so CI runs them as
# parallel jobs: tests_hostless_unit (tests/unit, every interpreter),
# tests_hostless_e2e (tests/e2e, the bookends) and tests_hostless_guards (the
# `interpreter_agnostic` family, the primary only). On the primary the three
# together select exactly what one session over `tests/unit tests/e2e` did —
# tests/unit/test_lane_invariants.py pins that against a literal baseline.
# Each instrumented leg writes a partial report (`--cov-fail-under=0`); no
# floor judges a slice.
#
# The UNIT pair: tests/unit less the family (the guards session runs it).
HOSTLESS_UNIT_TEST_ARGS = (
    "tests/unit",
    "-m",
    HOSTLESS_CLAUSES + " and not serial_timing and not interpreter_agnostic",
    "--cov-fail-under=0",
)
HOSTLESS_UNIT_SERIAL_ARGS = (
    "tests/unit",
    "-m",
    "serial_timing and " + HOSTLESS_CLAUSES + " and not interpreter_agnostic",
    "-n0",
    "--cov-append",
    "--cov-fail-under=0",
)

# The E2E pair: tests/e2e on the bookends. No family clause — the marker is
# declared under tests/unit only, so no e2e test carries it.
HOSTLESS_E2E_TEST_ARGS = (
    "tests/e2e",
    "-m",
    HOSTLESS_CLAUSES + " and not serial_timing",
    "--cov-fail-under=0",
)
HOSTLESS_E2E_SERIAL_ARGS = (
    "tests/e2e",
    "-m",
    "serial_timing and " + HOSTLESS_CLAUSES,
    "-n0",
    "--cov-append",
    "--cov-fail-under=0",
)

# The CANARY Python's e2e pair: the E2E pair with `--no-cov` in place of every
# coverage flag — the newest interpreter runs every product e2e test the
# primary does, uninstrumented. tests/unit/test_python_matrix_tiering.py pins
# that this pair is the E2E pair minus exactly the coverage flags.
HOSTLESS_E2E_CANARY_TEST_ARGS = (
    "tests/e2e",
    "-m",
    HOSTLESS_CLAUSES + " and not serial_timing",
    "--no-cov",
)
HOSTLESS_E2E_CANARY_SERIAL_ARGS = (
    "tests/e2e",
    "-m",
    "serial_timing and " + HOSTLESS_CLAUSES,
    "-n0",
    "--no-cov",
)

# The GUARDS leg: the `interpreter_agnostic` family, on the primary only — it
# proves the same thing on every Python (#566 Q2). One parallel leg, no
# serial_timing clause and no -n0 twin: tests/unit/test_make_quick.py pins
# that no family test is `serial_timing` (the `quick-guards` shape).
HOSTLESS_GUARDS_ARGS = (
    "tests/unit",
    "-m",
    HOSTLESS_CLAUSES + " and interpreter_agnostic",
    "--cov-fail-under=0",
)

# The INTERIOR Pythons' pair (every PYTHON_VERSIONS entry not in
# BOOKEND_PYTHONS). Measured 2026-09-11: a full hostless leg was ~1,420 s of
# CPU per interior Python, and 65 % of it was work whose subject is not otto
# under THIS interpreter — the tests/e2e tier (real `otto` subprocesses, real
# `pip` venv builds) and the repo-policy guards that re-collect the whole tree
# in a subprocess. The interior legs run tests_hostless_unit only (no e2e
# session there), so this pair is the UNIT pair with coverage off: the same
# roots and marker expressions, `--no-cov` in place of the coverage flags —
# pinned by tests/unit/test_python_matrix_tiering.py, and the constant pairing
# itself by tests/unit/test_lane_invariants.py's serial-leg scanner.
HOSTLESS_MIDDLE_TEST_ARGS = (
    "tests/unit",
    "-m",
    HOSTLESS_CLAUSES + " and not serial_timing and not interpreter_agnostic",
    "--no-cov",
)
HOSTLESS_MIDDLE_SERIAL_ARGS = (
    "tests/unit",
    "-m",
    "serial_timing and " + HOSTLESS_CLAUSES + " and not interpreter_agnostic",
    "-n0",
    "--no-cov",
)

# The per-push browser lane's marker expression. MUST match the Makefile's
# `dashboard` target (`-m "browser and not soak"`) — `soak` is the heavy,
# minutes-long replay stress test (tests/e2e/monitor/dashboard/test_replay_soak.py),
# gated to run only on demand (`make dashboard-soak`, chromium-only; see that
# test's module docstring), never on every push. There is no single source
# these two can share: this Makefile target is invoked directly (CI's
# `dashboard-e2e` job runs `uv run nox -k <browser>`, not `make dashboard`),
# so Make has no natural way to read a Python constant here without shelling
# out to a second interpreter for one string. That gap is exactly how this
# shipped once already: nox's `dashboard` session selected bare `browser`
# (no `not soak`), so the soak test ran on every push, on every engine,
# timing out on WebKit. If either expression changes, change both.
DASHBOARD_MARKER_EXPR = "browser and not soak"

# The two browser suites the `dashboard` session runs: the monitor dashboard
# and the coverage-report SPA. Also the roots the changed-test repeat's browser
# lane looks under.
DASHBOARD_PATHS = (
    "tests/e2e/monitor/dashboard",
    "tests/e2e/cov/report_browser",
)

# A failed browser test keeps its Playwright trace (actions, DOM snapshots,
# console, network) and a screenshot, which the CI jobs upload on failure. A
# rare browser failure (#1006: a confirm dialog that never opened, no repro)
# leaves nothing else behind; a passing test keeps nothing. Each job runs one
# engine, and the per-test directory name already carries it, so no per-engine
# subdirectory.
DASHBOARD_ARTIFACT_ARGS = (
    "--tracing",
    "retain-on-failure",
    "--screenshot",
    "only-on-failure",
    "--output",
    str(PLAYWRIGHT_OUTPUT_DIR),
)

# ── The changed-test repeat (#1044): a pull request repeats the test files its
# diff adds or modifies, so a test that fails only sometimes is caught where
# it is introduced. docs/contributing.md describes it under "The changed-test
# repeat". One bundle per lane, each the lane's own marker expression and
# `-n0` with coverage off and a count. They are spelled out, like the canary
# pair above, because the lane scanners (tests/unit/test_tier_marker_invariants.py)
# read a session's bundles as literal tuples and could not see the `-m` of a
# computed one; tests/unit/test_changed_repeat_lanes.py holds each equal to its
# lane's bundle, so the two cannot drift. The FILES come from the diff
# (scripts/changed_tests.py), so these carry no path. `--count` makes N items of each test, and
# in a parallel leg xdist spreads them over the workers, as nightly's
# `--count` lanes do: a test's repeats can run at the same time as each other
# unless an xdist group holds them on one worker. It finds a test that fails
# only sometimes, not state leaking from one test into the next in one
# process (that is `tests_unit_repeat`'s job).
CHANGED_REPEAT_COUNT = 20
CHANGED_REPEAT_GUARDS_COUNT = 5
CHANGED_REPEAT_BROWSER_COUNT = 10
CHANGED_REPEAT_UNIT_ROOTS = ("tests/unit",)
CHANGED_REPEAT_E2E_ROOTS = ("tests/e2e",)
CHANGED_REPEAT_UNIT_TEST_ARGS = (
    "-m",
    HOSTLESS_CLAUSES + " and not serial_timing and not interpreter_agnostic",
    "--no-cov",
    f"--count={CHANGED_REPEAT_COUNT}",
)
CHANGED_REPEAT_UNIT_SERIAL_ARGS = (
    "-m",
    "serial_timing and " + HOSTLESS_CLAUSES + " and not interpreter_agnostic",
    "-n0",
    "--no-cov",
    f"--count={CHANGED_REPEAT_COUNT}",
)
CHANGED_REPEAT_GUARDS_ARGS = (
    "-m",
    HOSTLESS_CLAUSES + " and interpreter_agnostic",
    "--no-cov",
    f"--count={CHANGED_REPEAT_GUARDS_COUNT}",
)
CHANGED_REPEAT_E2E_TEST_ARGS = (
    "-m",
    HOSTLESS_CLAUSES + " and not serial_timing",
    "--no-cov",
    f"--count={CHANGED_REPEAT_COUNT}",
)
CHANGED_REPEAT_E2E_SERIAL_ARGS = (
    "-m",
    "serial_timing and " + HOSTLESS_CLAUSES,
    "-n0",
    "--no-cov",
    f"--count={CHANGED_REPEAT_COUNT}",
)
CHANGED_REPEAT_BROWSER_ARGS = (
    "-m",
    DASHBOARD_MARKER_EXPR,
    "--no-cov",
    f"--count={CHANGED_REPEAT_BROWSER_COUNT}",
)

# The diff the changed-test repeat reads. In a pull request's CI the checkout
# is the merge commit, whose first parent is `main`: HEAD^1..HEAD is the pull
# request. Locally, name the range: CHANGED_TESTS_RANGE=origin/main...HEAD.
CHANGED_TESTS_RANGE_ENV = "CHANGED_TESTS_RANGE"
CHANGED_TESTS_DEFAULT_RANGE = "HEAD^1..HEAD"

# pytest's "no tests collected": every test in the changed files belongs to
# another lane (a bed-only test, a browser test in a hostless lane). That is
# nothing to repeat here, and it is green ONLY for the repeat sessions.
NO_TESTS_COLLECTED = 5


# ── The catch-all sessions' bundles. The `interpreter_agnostic` family (tests
# whose subject is the repository, its build files or a generated corpus) runs
# on PRIMARY_PYTHON only: it proves the same thing on every Python and already
# runs, instrumented, in `make coverage` (#566 Q2). Every non-primary bundle
# deselects it and runs with coverage off. Each `*_TEST_ARGS` has a
# `*_SERIAL_ARGS` twin carrying the same clauses with `serial_timing` flipped
# (tests/unit/test_lane_invariants.py's serial-leg scanner); the per-session
# branching and the trim are pinned by tests/unit/test_python_matrix_tiering.py.
UNIT_TEST_ARGS = (
    "tests/unit",
    "-m",
    (
        "not stability and not busybox and not conformance and not kmodcov "
        "and not release_proof and not serial_timing"
    ),
)
UNIT_SERIAL_ARGS = (
    "tests/unit",
    "-m",
    (
        "serial_timing and not stability and not busybox and not conformance and not kmodcov "
        "and not release_proof"
    ),
    "-n0",
    "--cov-append",
)
UNIT_NONPRIMARY_TEST_ARGS = (
    "tests/unit",
    "-m",
    (
        "not stability and not busybox and not conformance and not kmodcov "
        "and not release_proof and not serial_timing "
        "and not interpreter_agnostic"
    ),
    "--no-cov",
)
UNIT_NONPRIMARY_SERIAL_ARGS = (
    "tests/unit",
    "-m",
    (
        "serial_timing and not stability and not busybox and not conformance and not kmodcov "
        "and not release_proof "
        "and not interpreter_agnostic"
    ),
    "-n0",
    "--no-cov",
)
INTEGRATION_TEST_ARGS = (
    "tests/unit",
    "tests/integration",
    "-m",
    (
        "not stability and not busybox and not conformance and not kmodcov "
        "and not release_proof and not serial_timing"
    ),
)
INTEGRATION_SERIAL_ARGS = (
    "tests/unit",
    "tests/integration",
    "-m",
    (
        "serial_timing and not stability and not busybox and not conformance and not kmodcov "
        "and not release_proof"
    ),
    "-n0",
    "--cov-append",
)
INTEGRATION_NONPRIMARY_TEST_ARGS = (
    "tests/unit",
    "tests/integration",
    "-m",
    (
        "not stability and not busybox and not conformance and not kmodcov "
        "and not release_proof and not serial_timing "
        "and not interpreter_agnostic"
    ),
    "--no-cov",
)
INTEGRATION_NONPRIMARY_SERIAL_ARGS = (
    "tests/unit",
    "tests/integration",
    "-m",
    (
        "serial_timing and not stability and not busybox and not conformance and not kmodcov "
        "and not release_proof "
        "and not interpreter_agnostic"
    ),
    "-n0",
    "--no-cov",
)
TESTS_ALL_TEST_ARGS = (
    "-m",
    (
        "not browser and not stability and not busybox and not conformance and not kmodcov "
        "and not release_proof "
        "and not serial_timing"
    ),
    "--no-cov",
)
TESTS_ALL_SERIAL_ARGS = (
    "-m",
    (
        "serial_timing and not browser and not stability and not busybox and not conformance "
        "and not kmodcov and not release_proof"
    ),
    "-n0",
    "--no-cov",
)
TESTS_ALL_NONPRIMARY_TEST_ARGS = (
    "-m",
    (
        "not browser and not stability and not busybox and not conformance and not kmodcov "
        "and not release_proof "
        "and not serial_timing and not interpreter_agnostic"
    ),
    "--no-cov",
)
TESTS_ALL_NONPRIMARY_SERIAL_ARGS = (
    "-m",
    (
        "serial_timing and not browser and not stability and not busybox and not conformance "
        "and not kmodcov and not release_proof and not interpreter_agnostic"
    ),
    "-n0",
    "--no-cov",
)


@nox_uv.session(python=PYTHON_VERSIONS, uv_groups=["dev"])
def tests_unit(session: nox.Session) -> None:
    """Run the unit *level* tier (tests/unit only; no testbed) under each Python.

    The PRIMARY_PYTHON runs the full pair; every other interpreter runs the
    pair with the `interpreter_agnostic` family deselected and coverage off
    (see the bundle comment above). Bundles are named so the lane scanners
    read them.
    """
    parallel_junit = _junitxml(session, "nox-unit")
    serial_junit = _junitxml(session, "nox-unit-serial")
    if session.python == PRIMARY_PYTHON:
        session.run("pytest", *UNIT_TEST_ARGS, parallel_junit, *session.posargs)
        session.run("pytest", *UNIT_SERIAL_ARGS, serial_junit, *session.posargs)
    else:
        session.run("pytest", *UNIT_NONPRIMARY_TEST_ARGS, parallel_junit, *session.posargs)
        session.run("pytest", *UNIT_NONPRIMARY_SERIAL_ARGS, serial_junit, *session.posargs)


@nox_uv.session(python=PYTHON_VERSIONS, uv_groups=["dev"])
def tests_integration(session: nox.Session) -> None:
    """Run the unit + integration *level* tiers (tests/unit + tests/integration).

    Cumulative directory-based level: needs the full lab (the integration tier
    includes the Linux-VM, Zephyr, and Docker tests). No coverage gate — a single
    environment exercises only a slice of otto. The PRIMARY_PYTHON runs the
    full pair; every other interpreter deselects the `interpreter_agnostic`
    family and runs coverage off (see the bundle comment above).
    """
    parallel_junit = _junitxml(session, "nox-integration")
    serial_junit = _junitxml(session, "nox-integration-serial")
    if session.python == PRIMARY_PYTHON:
        session.run("pytest", *INTEGRATION_TEST_ARGS, parallel_junit, *session.posargs)
        session.run("pytest", *INTEGRATION_SERIAL_ARGS, serial_junit, *session.posargs)
    else:
        session.run("pytest", *INTEGRATION_NONPRIMARY_TEST_ARGS, parallel_junit, *session.posargs)
        session.run("pytest", *INTEGRATION_NONPRIMARY_SERIAL_ARGS, serial_junit, *session.posargs)


@nox_uv.session(python=PYTHON_VERSIONS, uv_groups=["dev"])
def tests_hostless_unit(session: nox.Session) -> None:
    """Run the no-testbed ``tests/unit`` tier, less the guards family, on every Python.

    The PRIMARY_PYTHON runs the instrumented UNIT pair; every other
    interpreter the HOSTLESS_MIDDLE pair (the same selection, coverage off).
    One of the three hostless sessions (with ``tests_hostless_e2e`` and
    ``tests_hostless_guards``) that ``.github/workflows/ci.yml`` runs and
    ``nox.options.sessions`` defaults to. The junit group names are shared,
    so reports/junit/nox-hostless/<session>.xml is one file per session.
    """
    # Each branch spells its bundles by NAME: the lane scanners in
    # tests/unit/test_tier_marker_invariants.py expand `*MODULE_CONSTANT` and
    # drop anything else, so a bundle passed through a local would read as a
    # path-less, marker-less `pytest` — and be judged as reaching every tier.
    parallel_junit = _junitxml(session, "nox-hostless")
    serial_junit = _junitxml(session, "nox-hostless-serial")
    if session.python == PRIMARY_PYTHON:
        session.run(
            "pytest", *HOSTLESS_UNIT_TEST_ARGS, parallel_junit, *session.posargs, env=_LEAK_ENV
        )
        session.run(
            "pytest", *HOSTLESS_UNIT_SERIAL_ARGS, serial_junit, *session.posargs, env=_LEAK_ENV
        )
    else:
        session.run(
            "pytest", *HOSTLESS_MIDDLE_TEST_ARGS, parallel_junit, *session.posargs, env=_LEAK_ENV
        )
        session.run(
            "pytest", *HOSTLESS_MIDDLE_SERIAL_ARGS, serial_junit, *session.posargs, env=_LEAK_ENV
        )


@nox_uv.session(python=PYTHON_VERSIONS, uv_groups=["dev"])
def tests_bed(session: nox.Session) -> None:
    """Run the bed half of ``make coverage``'s selection, uninstrumented, pass/fail.

    ``make nox-release`` runs it on the CANARY_PYTHON only: the release's
    cross-version bed leg. The hostless half on every non-primary Python is
    CI's (``tests_hostless_unit``, ``tests_hostless_e2e``). The
    ``interpreter_agnostic`` family is deselected (it runs on the primary
    only). Requires the dev VM with Vagrant hosts up; not run in CI.
    """
    # Bundles spelled by NAME, as in tests_hostless_unit: the lane scanners
    # expand `*MODULE_CONSTANT` only.
    parallel_junit = _junitxml(session, "nox-bed")
    serial_junit = _junitxml(session, "nox-bed-serial")
    session.run("pytest", *TESTS_BED_TEST_ARGS, parallel_junit, *session.posargs, env=_LEAK_ENV)
    session.run("pytest", *TESTS_BED_SERIAL_ARGS, serial_junit, *session.posargs, env=_LEAK_ENV)


@nox_uv.session(python=list(BOOKEND_PYTHONS), uv_groups=["dev"])
def tests_hostless_e2e(session: nox.Session) -> None:
    """Run the no-testbed ``tests/e2e`` tier on the bookend Pythons.

    The PRIMARY_PYTHON runs the instrumented E2E pair, the CANARY_PYTHON the
    same selection with coverage off. The interior versions run no e2e: its
    subject (real ``otto`` subprocesses, real ``pip`` venv builds) is the same
    on every Python.
    """
    parallel_junit = _junitxml(session, "nox-hostless")
    serial_junit = _junitxml(session, "nox-hostless-serial")
    if session.python == PRIMARY_PYTHON:
        session.run(
            "pytest", *HOSTLESS_E2E_TEST_ARGS, parallel_junit, *session.posargs, env=_LEAK_ENV
        )
        session.run(
            "pytest", *HOSTLESS_E2E_SERIAL_ARGS, serial_junit, *session.posargs, env=_LEAK_ENV
        )
    else:
        session.run(
            "pytest",
            *HOSTLESS_E2E_CANARY_TEST_ARGS,
            parallel_junit,
            *session.posargs,
            env=_LEAK_ENV,
        )
        session.run(
            "pytest",
            *HOSTLESS_E2E_CANARY_SERIAL_ARGS,
            serial_junit,
            *session.posargs,
            env=_LEAK_ENV,
        )


@nox_uv.session(python=[PRIMARY_PYTHON], uv_groups=["dev"])
def tests_hostless_guards(session: nox.Session) -> None:
    """Run the ``interpreter_agnostic`` guards family, on the primary Python only.

    One parallel leg: the family has no ``serial_timing`` member (see
    HOSTLESS_GUARDS_ARGS).
    """
    session.run(
        "pytest",
        *HOSTLESS_GUARDS_ARGS,
        _junitxml(session, "nox-hostless"),
        *session.posargs,
        env=_LEAK_ENV,
    )


@nox_uv.session(python=[PRIMARY_PYTHON], uv_groups=["dev"])
def tests_unit_repeat(session: nox.Session) -> None:
    """Repeat the whole ``tests/unit`` tree in one process to catch state leaks.

    Many unit tests mutate process-global state and rely on it being pristine:
    they register into the ``otto.registry.Registry`` singletons (``INSTRUCTIONS``,
    ``LOADER_CLASSES``, ``CLI_COMMANDS``, ``OPTIONS`` …) via ``@instruction`` /
    ``register_*`` / ``register_options``, import packages/projects from
    a ``tmp_path``, or ``monkeypatch.delitem`` an ``otto.cli.*`` submodule from
    ``sys.modules``. A test that leaves any of that behind is invisible to a
    single CI pass but breaks the second time it runs in the same process —
    which is why such leaks only ever surfaced in the nightly ``--count`` matrix,
    several nights after landing (issue #108, and the earlier SUITES leak).

    Running the whole no-VM ``tests/unit`` tree twice in one process — less the
    ``busybox`` tier, which fetches from the network (see the ``-m`` below) —
    (``--count=2 --repeat-scope=session``, single-process via a cleared
    ``addopts``) trips any such regression. It is nightly's ``unit-repeat`` job
    (#795): at ~60 min it was CI's longest job by a factor of two and set every
    pull request's wall clock, while the registry class above is now restored
    per test by the root conftest's ``_isolate_registries`` and the tmp-import
    class by ``purge_tmp_imports``; what the repeat still catches is the class
    nobody has named yet. The ``interpreter_agnostic`` guards are left out:
    they hold no process-global state, and repeating them is most of the job's
    wall clock
    (see the ``-m`` comment below). Single-process is deliberate: under
    ``-n auto`` the repeated items scatter across workers, so within-module
    collisions become probabilistic and cross-module leaks (a later test
    broken by an earlier one) are masked entirely — exactly how the nightly
    missed the ``test_root_group`` → ``test_listing`` desync. One Python is
    sufficient (isolation leakage is interpreter-version-independent — the
    primary, 3.10); clearing ``addopts`` drops the repo-wide ``-n auto`` and
    coverage so the repeat is single-process (strictest accumulation) and
    quick. Supersedes the former
    ``tests_suite_repeat`` (``tests/unit/suite`` ⊂ ``tests/unit``).
    """
    session.run(
        "pytest",
        "tests/unit",
        # `not busybox`, on a lane that selects by PATH. Every other default
        # lane carries this clause because it selects by marker; this one had
        # no `-m` at all, so it selected `tests/unit/host/test_busybox_artifacts.py`'s
        # five artifact tests whole — a ~5 MB busybox.net fetch, and a fixture
        # that RAISES rather than skips when upstream is down, inside a job
        # that runs on every push and every PR and gates `report-failure`.
        # Measured before the clause: 5 of 5483 collected items were the
        # `busybox` tier, and the lane's exact invocation against an empty
        # cache fetched all five artifacts. The whole G8 guard family reasons
        # over `-m` expressions and so could not see a path-only lane;
        # tests/unit/test_tier_marker_invariants.py's G8f now covers that
        # shape, and reds if this clause goes away.
        #
        # `not stability` rides along because an all-negation expression is a
        # catch-all by G4's rule whatever path scopes it, and this is the
        # spelling every other tests/unit lane uses. It deselects nothing today
        # (measured: `-m stability` over tests/unit collects 0 of 5484), so the
        # isolation guard's coverage is unchanged. `serial_timing` is
        # deliberately NOT excluded, unlike the sibling lanes: those exclude it
        # because they run under `-n auto` and re-append it in a paired `-n0`
        # leg, and this session is single-process already.
        #
        # `not conformance` rides along for the same reason `not stability`
        # does: this is an all-negation expression and so a catch-all by G4's
        # rule whatever path scopes it. It deselects nothing today — the lane
        # names `tests/unit`, and every conformance test lives under
        # `tests/conformance` — which makes it the clause that stays correct if
        # a conformance-marked module ever lands under `tests/unit`.
        #
        # `not interpreter_agnostic`: this session hunts state that leaks from
        # one test into the next, and the interpreter-agnostic guards hold no
        # registry state to leak — they read build files and spawn child
        # pytests. Repeating them doubles the subprocess collections, the most
        # expensive shape left in the family, for a question they cannot answer.
        # CI measured the job at 56-63 min (2026-11-01), the longest in the
        # workflow by a factor of two. Pinned by tests/unit/test_python_matrix_tiering.py.
        "-m",
        "not stability and not busybox and not conformance and not kmodcov and not release_proof "
        "and not interpreter_agnostic",
        # The shim differential is interpreter-DEPENDENT (it runs the
        # completion shim the user's Python executes against Typer in-process),
        # so it is not in the family and runs on every Python in the matrix.
        # It is still one test of ~4 min single-process: this session keeps it
        # out by path. Pinned by tests/unit/test_python_matrix_tiering.py.
        "--ignore=tests/unit/shim/test_differential.py",
        # Clearing addopts must still re-state `-p no:tach`: the override drops
        # pyproject's entry whole, and that flag is the only thing protecting
        # plugin LOAD from issue #193 (the conftest stub seeds too late).
        # Pinned by tests/unit/test_lane_invariants.py.
        "-o",
        "addopts=-p no:tach",
        "--count=2",
        "--repeat-scope=session",
        _junitxml(session, "nox-unit-repeat"),
        *session.posargs,
    )


@nox_uv.session(python=PYTHON_VERSIONS, uv_groups=["dev"])
def tests_unix(session: nox.Session) -> None:
    """Run the Unix-VM integration suite (incl. multi-hop) under each Python.

    Requires the dev VM with Vagrant hosts up; not run in CI. No coverage gate
    is enforced — a single environment exercises only a slice of otto. Bare
    positive selector, so `not stability and not chaos` are both required
    here rather than riding a catch-all's exclusion: tests/e2e/chaos/ shares
    the `integration` marker with this slice, so without the exclusion this
    session would co-select chaos scenarios that soft-reboot the leased host
    and blackhole SSH mid-suite (mirrors the Makefile's `M_UNIX`).
    """
    session.run(
        "pytest",
        "-m",
        "integration and not embedded and not stability and not chaos and not kmodcov "
        "and not release_proof "
        "and not serial_timing",
        _junitxml(session, "nox-unix"),
        *session.posargs,
    )
    session.run(
        "pytest",
        "-m",
        "serial_timing and integration and not embedded and not stability and not chaos "
        "and not kmodcov and not release_proof",
        "-n0",
        "--cov-append",
        _junitxml(session, "nox-unix-serial"),
        *session.posargs,
    )


@nox_uv.session(python=PYTHON_VERSIONS, uv_groups=["dev"])
def tests_embedded(session: nox.Session) -> None:
    """Run the embedded (Zephyr) suite under each supported Python.

    Requires the Vagrant lab (zephyr VM) up; not run in CI. Serialization is
    handled by the per-device xdist_group + console lock in
    tests/integration/host/conftest.py, so no -n0 is forced. No coverage
    gate. Bare positive selector, so `not stability and not chaos` are both
    required here rather than riding a catch-all's exclusion:
    tests/e2e/chaos/ shares the `embedded` marker with this slice (the
    console-client-death scenario), so without the exclusion this session
    would co-select it (mirrors the Makefile's `M_EMBEDDED`).
    """
    session.run(
        "pytest",
        "-m",
        "embedded and not stability and not chaos and not kmodcov and not release_proof",
        _junitxml(session, "nox-embedded"),
        *session.posargs,
    )


@nox_uv.session(python=[PRIMARY_PYTHON], uv_groups=["dev"])
def chaos(session: nox.Session) -> None:
    """Tier-3 chaos lane, unix legs (opt-in; run via `make chaos`).

    Bed-hostile by design: scenarios interrupt/SIGKILL real otto subprocesses
    mid-flight on a leased unix host, blackhole SSH over the data-plane
    link, and soft-reboot the leased host. Requires the lab VMs and EXCLUSIVE
    bed use — never co-run with any other bed lane. No coverage: these runs
    exist to hunt teardown leaks, not to measure lines.
    """
    session.run(
        "pytest",
        "tests/e2e/chaos",
        "-m",
        "chaos and not embedded",
        "--no-cov",
        "-p",
        "no:cacheprovider",
        _junitxml(session, "nox-chaos"),
        *session.posargs,
    )


@nox_uv.session(python=[PRIMARY_PYTHON], uv_groups=["dev"])
def chaos_embedded(session: nox.Session) -> None:
    """Tier-3 chaos lane, zephyr console leg (opt-in; run via `make chaos-embedded`).

    Separate from `chaos` on purpose: the console-client-death scenario
    reproduces the trigger class of the 2026-08-01 fat-board wedge, and a
    failure can require a manual zephyr bed restart. Run it deliberately,
    never as a rider.
    """
    session.run(
        "pytest",
        "tests/e2e/chaos",
        "-m",
        "chaos and embedded",
        "--no-cov",
        "-p",
        "no:cacheprovider",
        _junitxml(session, "nox-chaos-embedded"),
        *session.posargs,
    )


@nox_uv.session(python=PYTHON_VERSIONS, uv_groups=["dev"])
def tests_all(session: nox.Session) -> None:
    """Run the full suite — unit + integration + hops — under each supported Python.

    Requires the dev VM with Vagrant hosts up; not run in CI. Pytest's
    default ``testpaths`` (from pyproject.toml) covers the full tree, so no
    path filter is passed here. `browser` is excluded (see module-level
    comment above) — run it via `nox -s dashboard` / `make dashboard`
    instead. `stability` is excluded because those tests are bed-hostile
    (the SIGSTOP-wedge test stops test2's sshd; any other worker's fresh
    ssh to test2 then times out) — they own the bed only in the dedicated
    `make stability-tunnel` lane.

    Uninstrumented on every interpreter (`--no-cov`): the primary leg's
    selection is what ``make coverage`` gates at 95.5 % on the same tree,
    and a second floor here was a lower reading of the same run that only
    the release read (owner ruling 5, #566: deleted). Every other
    interpreter also deselects the `interpreter_agnostic` family — it runs
    on the primary only.
    """
    parallel_junit = _junitxml(session, "nox")
    serial_junit = _junitxml(session, "nox-all-serial")
    if session.python == PRIMARY_PYTHON:
        session.run("pytest", *TESTS_ALL_TEST_ARGS, parallel_junit, *session.posargs)
        session.run("pytest", *TESTS_ALL_SERIAL_ARGS, serial_junit, *session.posargs)
    else:
        session.run("pytest", *TESTS_ALL_NONPRIMARY_TEST_ARGS, parallel_junit, *session.posargs)
        session.run("pytest", *TESTS_ALL_NONPRIMARY_SERIAL_ARGS, serial_junit, *session.posargs)


@nox_uv.session(python=[PRIMARY_PYTHON], uv_groups=["dev"])
@nox.parametrize("browser", ["chromium", "firefox", "webkit"])
def dashboard(session: nox.Session, browser: str) -> None:
    """Run the monitor-dashboard + coverage-report browser e2e suites for one engine.

    Parametrized over Chromium (Blink), Firefox (Gecko), and WebKit (Safari):
    `nox -s dashboard` runs all three (serially — nox has no in-process
    session parallelism), while CI's `dashboard` matrix runs each variant in
    its OWN parallel job (see `.github/workflows/ci.yml`), so wall-clock is one
    engine's runtime, not three. Select one locally with `nox -k <browser>`.
    The one Safari-specific test is `@only_browser("webkit")`, so it runs only
    in the webkit variant and skips (not silently) in the others. Kept out of
    the hostless gate (and its 5-Python CI matrix) so only this session needs a
    browser binary; installs just this variant's engine.

    Uses `DASHBOARD_MARKER_EXPR` (`browser and not soak`), matching `make
    dashboard` exactly: this is the per-push lane, and the `soak` replay
    stress test (test_replay_soak.py) is on-demand only (`make
    dashboard-soak`) — see `DASHBOARD_MARKER_EXPR`'s comment for why this
    string is a hand-kept duplicate rather than a shared constant, and why
    that duplication is exactly what let the soak run on every push here
    before this comment existed.

    Runs both suites in one pytest invocation: `tests/e2e/monitor/dashboard`
    (the monitor dashboard, through a real `MonitorServer`) and
    `tests/e2e/cov/report_browser` (the coverage-report SPA — both its
    `file://` lane and, since it landed, the served-CSP lane against a real
    HTTP server; see `tests/_fixtures/_csp_server.py`). This is what puts the
    coverage-report suite's browser matrix into CI: this session is what
    `.github/workflows/ci.yml`'s `dashboard` job runs per engine.

    Both suites drive a BUILT React bundle (`src/otto/_webassets/monitor/dist/`
    and `src/otto/_webassets/covapp/dist/` respectively) —
    there's no legacy static fallback since the React cutover, so both
    `dist/` dirs must already exist before this session runs. Building them
    needs Node/npm (`make web`, which installs web/'s dependencies itself),
    which nox-uv's per-session venvs (Python-only, one per `PYTHON_VERSIONS`
    entry) have no way to provision. Rather than bolt a Node toolchain onto a
    nox session, that step lives directly in `.github/workflows/ci.yml`'s
    `dashboard` job, ahead of the nox call; `make dashboard` (the local/dev
    entrypoint) carries the same prerequisite —
    and, unlike this session, rebuilds a dist that has gone stale against
    web/src/ rather than only one that is missing.
    That job runs in Playwright's image (tagged from uv.lock), which already
    carries each engine and its system libraries, so the `playwright install`
    below finds the engine in place; locally it downloads the engine.
    """
    session.run("playwright", "install", browser)
    session.run(
        "pytest",
        *DASHBOARD_PATHS,
        "-m",
        DASHBOARD_MARKER_EXPR,
        "--browser",
        browser,
        # CI-only lane: unlike `make dashboard` (which writes coverage data for
        # `make coverage` to fold in via --cov-append), this job stands alone
        # with nothing to combine into, so coverage stays off entirely.
        "--no-cov",
        *DASHBOARD_ARTIFACT_ARGS,
        _junitxml(session, f"dashboard-{browser}"),
        *session.posargs,
    )


def _changed_test_files(session: nox.Session, roots: "tuple[str, ...]") -> "list[str]":
    """Return the test files the diff adds or modifies under *roots* (scripts/changed_tests.py)."""
    rev_range = os.environ.get(CHANGED_TESTS_RANGE_ENV, CHANGED_TESTS_DEFAULT_RANGE)
    # stderr=None: nox folds stderr into the captured output by default, and
    # a warning printed there would be handed to pytest as a path.
    listing = session.run(
        "python",
        "scripts/changed_tests.py",
        "--range",
        rev_range,
        *roots,
        silent=True,
        stderr=None,
    )
    return (listing or "").split()


@nox_uv.session(python=PYTHON_VERSIONS, uv_groups=["dev"])
def tests_changed_repeat_unit(session: nox.Session) -> None:
    """Repeat the ``tests/unit`` files the diff adds or modifies (less the guards family).

    The changed-test repeat (#1044): the lane ``tests_hostless_unit`` runs,
    over the diff's files only, ``CHANGED_REPEAT_COUNT`` times each. One
    failure in any repeat fails the session; nothing is retried.
    """
    files = _changed_test_files(session, CHANGED_REPEAT_UNIT_ROOTS)
    if not files:
        session.skip("the diff adds or modifies no test file under tests/unit")
    session.run(
        "pytest",
        *files,
        *CHANGED_REPEAT_UNIT_TEST_ARGS,
        _junitxml(session, "changed-repeat"),
        *session.posargs,
        env=_LEAK_ENV,
        success_codes=[0, NO_TESTS_COLLECTED],
    )
    session.run(
        "pytest",
        *files,
        *CHANGED_REPEAT_UNIT_SERIAL_ARGS,
        _junitxml(session, "changed-repeat-serial"),
        *session.posargs,
        env=_LEAK_ENV,
        success_codes=[0, NO_TESTS_COLLECTED],
    )


@nox_uv.session(python=[PRIMARY_PYTHON], uv_groups=["dev"])
def tests_changed_repeat_guards(session: nox.Session) -> None:
    """Repeat the ``interpreter_agnostic`` guards in the files the diff adds or modifies.

    The changed-test repeat's guards lane: what ``tests_hostless_guards``
    runs, over the diff's files only, on the primary Python only.
    """
    files = _changed_test_files(session, CHANGED_REPEAT_UNIT_ROOTS)
    if not files:
        session.skip("the diff adds or modifies no test file under tests/unit")
    session.run(
        "pytest",
        *files,
        *CHANGED_REPEAT_GUARDS_ARGS,
        _junitxml(session, "changed-repeat"),
        *session.posargs,
        env=_LEAK_ENV,
        success_codes=[0, NO_TESTS_COLLECTED],
    )


@nox_uv.session(python=list(BOOKEND_PYTHONS), uv_groups=["dev"])
def tests_changed_repeat_e2e(session: nox.Session) -> None:
    """Repeat the hostless tests in the ``tests/e2e`` files the diff adds or modifies.

    The changed-test repeat's e2e lane: what ``tests_hostless_e2e`` runs, over
    the diff's files only, on the bookend Pythons. A browser test in one of
    those files is deselected here and repeated by
    ``tests_changed_repeat_browser``.
    """
    files = _changed_test_files(session, CHANGED_REPEAT_E2E_ROOTS)
    if not files:
        session.skip("the diff adds or modifies no test file under tests/e2e")
    session.run(
        "pytest",
        *files,
        *CHANGED_REPEAT_E2E_TEST_ARGS,
        _junitxml(session, "changed-repeat"),
        *session.posargs,
        env=_LEAK_ENV,
        success_codes=[0, NO_TESTS_COLLECTED],
    )
    session.run(
        "pytest",
        *files,
        *CHANGED_REPEAT_E2E_SERIAL_ARGS,
        _junitxml(session, "changed-repeat-serial"),
        *session.posargs,
        env=_LEAK_ENV,
        success_codes=[0, NO_TESTS_COLLECTED],
    )


@nox_uv.session(python=[PRIMARY_PYTHON], uv_groups=["dev"])
def tests_changed_repeat_browser(session: nox.Session) -> None:
    """Repeat the browser tests in the dashboard files the diff adds or modifies.

    The changed-test repeat's browser lane: what ``dashboard`` runs, over the
    diff's files only. NOT parametrized by engine, unlike ``dashboard``: CI and
    nightly select that session with ``nox -k <engine>``, which must keep
    matching it alone. The engine comes in the posargs, as pytest takes it::

        nox -s tests_changed_repeat_browser -- --browser chromium

    Like ``dashboard``, it needs the built dashboard (``make web``) and the
    engine installed (``playwright install <engine>``; CI's image carries all
    three).
    """
    files = _changed_test_files(session, DASHBOARD_PATHS)
    if not files:
        session.skip("the diff adds or modifies no test file under the browser suites")
    session.run(
        "pytest",
        *files,
        *CHANGED_REPEAT_BROWSER_ARGS,
        *DASHBOARD_ARTIFACT_ARGS,
        _junitxml(session, "changed-repeat"),
        *session.posargs,
        success_codes=[0, NO_TESTS_COLLECTED],
    )


def _scratch(session: nox.Session, name: str) -> Path:
    """Return a fresh scratch directory *name* for *session*, outside the checkout.

    Every nox scratch directory comes from here, never ``session.create_tmp()``.
    ``create_tmp``'s directory lives under ``.nox/`` in the checkout and
    points the session's TMPDIR there, so a pytest or pytester run from it
    reads the checkout's pyproject.toml (its addopts and ``pythonpath``) and
    git work tree: about 110 of the first floor nightly's 203 failures, and
    the wheel smoke could see the checkout's code. Each session of each nox invocation has a
    private root (``mkdtemp``), so concurrent runs, of another worktree or the
    same one, never share or delete each other's files. The *name*
    subdirectory is emptied on each call, and the root goes when nox exits.
    """
    root = _SCRATCH_ROOTS.get(session.name)
    if root is None:
        root = Path(tempfile.mkdtemp(prefix=f"otto-nox-{session.name}-"))
        _SCRATCH_ROOTS[session.name] = root
        atexit.register(shutil.rmtree, root, ignore_errors=True)
    path = root / name
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir()
    return path


_SCRATCH_ROOTS: "dict[str, Path]" = {}
"""Each session's scratch root in this nox invocation (:func:`_scratch`)."""


def _built_wheel(session: nox.Session) -> Path:
    """Return the one built wheel, ``dist/*.whl``, resolved; any other count is an error.

    The one wheel selection, for every session that installs the build
    (:func:`_smoke_wheel`, :func:`packaging_offline`), so no two proofs can
    pick different files. ``make wheel-check`` builds it.
    """
    wheels = sorted(Path("dist").glob("*.whl"))
    if len(wheels) != 1:
        session.error(
            f"expected one wheel in dist/, found {len(wheels)}: build it with `make wheel-check`"
        )
    return wheels[0].resolve()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True, kw_only=True)
class OfflineCell:
    """One platform the offline install is proved on: how its host looks, what Step 1 fetches.

    A host is the cell whose *sys_platform*, *machine* (``platform.machine()``,
    as the dependency markers spell it) and C library match its own.
    *target* is the uv ``--python-platform`` target
    ``scripts/check_jsonnet_markers.py`` pins the binding of (``EXPECTED``),
    so the binding a cell asserts is read there, never restated here.
    *platforms* are the ``pip download --platform`` tags docs/installation.md's
    Step 1 names for the platform, in order; pip takes any of them.
    """

    sys_platform: str
    machine: str
    musl: bool
    target: str
    platforms: "tuple[str, ...]"


# The platforms the offline install is proved on, each by nightly's
# `packaging-offline` jobs (one runner per cell) and the docs' Step 1 platform
# tags (docs/installation.md, "Common platform tags"). A new platform is one
# row here, one runner there and one tag line in the docs.
OFFLINE_CELLS: "tuple[OfflineCell, ...]" = (
    OfflineCell(
        sys_platform="linux",
        machine="x86_64",
        musl=False,
        target="x86_64-manylinux_2_28",
        platforms=("manylinux_2_28_x86_64", "manylinux2014_x86_64"),
    ),
    OfflineCell(
        sys_platform="linux",
        machine="x86_64",
        musl=True,
        target="x86_64-unknown-linux-musl",
        platforms=("musllinux_1_2_x86_64", "musllinux_1_1_x86_64"),
    ),
    OfflineCell(
        sys_platform="linux",
        machine="aarch64",
        musl=False,
        target="aarch64-manylinux2014",
        platforms=("manylinux2014_aarch64",),
    ),
    OfflineCell(
        sys_platform="darwin",
        machine="arm64",
        musl=False,
        target="aarch64-apple-darwin",
        platforms=("macosx_11_0_arm64",),
    ),
    OfflineCell(
        sys_platform="win32",
        machine="AMD64",
        musl=False,
        target="x86_64-pc-windows-msvc",
        platforms=("win_amd64",),
    ),
)


def _host_cell(session: nox.Session) -> OfflineCell:
    """Return the :data:`OFFLINE_CELLS` row this host is; a host none describes is an error."""
    musl = any(tag.platform.startswith("musllinux") for tag in sys_tags())
    machine = platform.machine()
    found = [
        cell
        for cell in OFFLINE_CELLS
        if cell.sys_platform == sys.platform and cell.machine == machine and cell.musl == musl
    ]
    if len(found) != 1:
        libc = "musl" if musl else "not musl"
        session.error(
            f"{len(found)} OFFLINE_CELLS rows describe this host ({sys.platform}, {machine}, "
            f"{libc}): otto proves an installed wheel only on the platforms it lists, once each"
        )
    return found[0]


# `python -c` puts the checkout on sys.path, as for LOCKED_VERSION below, so
# scripts/check_jsonnet_markers.py, the one table of which binding each target
# installs, imports. EXPECTED_BINDING prints the binding a target's marker
# installs on every supported Python; CANARY_RESOLVE
# checks a target's binary-only resolve on the next Python (EXPECTED_315),
# which has no interpreter to install on.
EXPECTED_BINDING = (
    "import sys; from scripts.check_jsonnet_markers import EXPECTED; print(EXPECTED[sys.argv[1]])"
)
CANARY_RESOLVE = (
    "import sys; from scripts.check_jsonnet_markers import EXPECTED_315, NIGHTLY, Case, "
    "canary_python, check; target = sys.argv[1]; minor = canary_python(NIGHTLY.read_text()); "
    "problem = check(Case(target, minor, EXPECTED_315[target])) if target in EXPECTED_315 "
    "else None; print(problem or f'{target} on Python {minor}: ' + "
    "('resolves as EXPECTED_315 pins' if target in EXPECTED_315 else 'no EXPECTED_315 pin')); "
    "sys.exit(problem is not None)"
)


def _expected_binding(session: nox.Session, cell: OfflineCell) -> str:
    """Return the binding *cell*'s marker installs (``jsonnet`` or ``gojsonnet``)."""
    said = session.run(
        sys.executable, "-c", EXPECTED_BINDING, cell.target, silent=True, external=True
    )
    if not isinstance(said, str) or not said.strip():
        session.error(f"scripts/check_jsonnet_markers.py pins no binding for {cell.target}")
    return said.strip()


def _smoke_wheel(session: nox.Session, runtime: Path) -> None:
    """Install the built wheel with ``--no-deps`` over the *runtime* requirements, then smoke it.

    The one installed-otto smoke (scripts/wheel_smoke.py), for each runtime
    resolution a session hands it, including the identity check against the
    wheel and the installed ``otto.libsonnet`` render under this host's
    binding. It runs from a scratch directory, so neither ``src/`` nor the
    checkout's tests are importable.
    """
    wheel = _built_wheel(session)
    binding = _expected_binding(session, _host_cell(session))
    session.install("-r", str(runtime))
    session.install("--no-deps", str(wheel))
    script = Path("scripts/wheel_smoke.py").resolve()
    session.chdir(_scratch(session, "smoke"))
    session.run("python", str(script), str(wheel), f"--binding={binding}")


@nox.session(python=[PRIMARY_PYTHON])
def wheel_smoke(session: nox.Session) -> None:
    """Smoke the built wheel in a venv of runtime dependencies only (scripts/wheel_smoke.py).

    Every other session installs the `dev` group into an editable otto, so a
    development dependency or an auto-loaded plugin can hide what an
    installed otto lacks (#593: pytest-cov's `--no-cov`). This one installs
    `dist/*.whl` with `--no-deps` over the locked runtime dependencies
    (`uv export --no-dev`) — what `pip install otto-sh` resolves, pinned.
    :func:`wheel_smoke_floors` is the same smoke over the declared floors.

    It needs a built wheel. Building one needs the web assets (the build
    backend refuses an asset-less wheel), so the Node step stays outside
    this Python-only session, as for `dashboard`: `make wheel-smoke` builds
    first, and so does CI's `wheel-smoke` job.
    """
    runtime = _scratch(session, "locked") / "runtime-requirements.txt"
    session.run_install(
        "uv",
        "export",
        "--frozen",
        "--no-dev",
        "--no-emit-project",
        "--no-hashes",
        f"--output-file={runtime}",
        external=True,
    )
    _smoke_wheel(session, runtime)


def _floor_requirements(session: nox.Session) -> Path:
    """Resolve otto's runtime with every direct dependency pinned to its declared floor.

    The one floor resolution, for :func:`wheel_smoke_floors` and
    :func:`tests_unit_floors`. Each direct dependency is constrained to
    ``name==floor`` (``scripts/check_docs_dependency_table.py --constraints``,
    the floors' one reader), and the rest resolve for this session's Python.
    A floor no resolution can install (another dependency needs more of it)
    fails here, naming both, and so does a floor its index has yanked (a
    release withdrawn as broken is no floor to promise). Returns the pinned
    requirements file.
    """
    tmp = _scratch(session, "floors")
    # Read by the interpreter running nox (the dev venv): this session's venv is
    # empty until the floors it is about to read are installed.
    constraints_argv = (sys.executable, "scripts/check_docs_dependency_table.py", "--constraints")
    # run_install, so the floors are also read under `--install-only`; it returns
    # None under `--no-install`/`-R`, where `run` reads them instead.
    floors = session.run_install(*constraints_argv, silent=True, external=True) or session.run(
        *constraints_argv, silent=True, external=True
    )
    if not isinstance(floors, str) or not floors.strip():
        session.error("scripts/check_docs_dependency_table.py --constraints printed no floors")
    constraints = tmp / "floor-constraints.txt"
    constraints.write_text(floors)
    runtime = tmp / "floor-requirements.txt"
    said = session.run_install(
        "uv",
        "pip",
        "compile",
        "pyproject.toml",
        f"--constraints={constraints}",
        f"--python-version={session.python}",
        "--no-header",
        f"--output-file={runtime}",
        silent=True,
        external=True,
    )
    # Keyed on uv's warning wording ("`name==version` is yanked (reason: ...)");
    # a reworded warning would silence this refusal, not fail it.
    yanked = [line for line in (said or "").splitlines() if "is yanked" in line]
    if yanked:
        session.error("a declared floor is a yanked release:\n" + "\n".join(yanked))
    return runtime


@nox.session(python=[PRIMARY_PYTHON])
def wheel_smoke_floors(session: nox.Session) -> None:
    """Smoke the built wheel with every direct dependency at its declared floor (#1181).

    A declared floor is a promise: a user whose resolver lands on it gets an
    otto that works. ``pydantic>=2.6`` was not one: otto crashed at import
    below 2.10.4, and nothing ran it there. This session installs the floor
    resolution (:func:`_floor_requirements`) on the oldest Python otto
    supports and runs the same smoke as :func:`wheel_smoke`, so a floor below
    what otto needs fails here, at release and on every push.
    :func:`tests_unit_floors` runs the unit suite there nightly.
    """
    _smoke_wheel(session, _floor_requirements(session))


# pip run by these sessions reads no pip configuration file, so a developer's
# index or find-links setting can neither feed nor starve the offline proof.
_PIP_ENV = {"PIP_CONFIG_FILE": os.devnull, "PIP_DISABLE_PIP_VERSION_CHECK": "1"}


@nox.session(python=PYTHON_VERSIONS)
def packaging_offline(session: nox.Session) -> None:
    """Prove the documented air-gapped install of the built wheel on this host and Python.

    docs/installation.md's Step 1, run as it is documented, for this host's
    :data:`OFFLINE_CELLS` row: ``pip download <wheel path>`` under this
    session's Python with its ``--python-version``, the cell's ``--platform``
    tags and ``--only-binary :all:``, into a wheel directory. pip never looks
    on the index for a pathname requirement, so the directory holds this
    build and the dependencies it declares; the copy pip placed there must
    have the built wheel's sha256. Then a fresh venv of this Python installs
    the wheel by pathname with ``--no-index --find-links`` that directory,
    and, from a scratch directory outside the checkout,
    ``scripts/wheel_smoke.py --installed-only`` checks that the installed
    otto is the wheel, file for file, and that the installed
    ``otto.libsonnet`` renders under the binding the cell's marker installs.
    The fresh venv's ``site-packages`` size is printed. On the primary Python the
    cell's target is also resolved, binary-only, for the next Python
    (``EXPECTED_315``), which no interpreter here can install on.

    Nightly's `packaging-offline` jobs run it on every cell and Python. Like
    :func:`wheel_smoke`, it needs a built wheel (``make wheel-check``).
    """
    wheel = _built_wheel(session)
    digest = _sha256(wheel)
    cell = _host_cell(session)
    binding = _expected_binding(session, cell)
    session.log(f"cell {cell.target}: {wheel.name} sha256 {digest}, binding {binding}")
    if session.python == PRIMARY_PYTHON:
        session.run(sys.executable, "-c", CANARY_RESOLVE, cell.target, external=True)
    root = _scratch(session, "offline")
    wheels = root / "wheels"
    session.install("pip")
    session.run(
        "python",
        "-m",
        "pip",
        "download",
        str(wheel),
        "--dest",
        str(wheels),
        "--python-version",
        str(session.python),
        *(f"--platform={tag}" for tag in cell.platforms),
        "--only-binary",
        ":all:",
        env=_PIP_ENV,
    )
    staged = wheels / wheel.name
    if not staged.is_file() or _sha256(staged) != digest:
        found = _sha256(staged) if staged.is_file() else "no file"
        session.error(f"pip download staged {staged} as {found}, not the built wheel's {digest}")
    venv = root / "venv"
    session.run("python", "-m", "venv", str(venv))
    # The interpreter otto itself finds in a venv, on either layout; the
    # process running nox has otto (the dev venv, or the nightly job's tool env).
    from otto.env.backends import venv_python

    python = venv_python(venv)
    session.run(
        str(python),
        "-m",
        "pip",
        "install",
        "--no-index",
        "--find-links",
        str(wheels),
        str(wheel),
        env=_PIP_ENV,
        external=True,
    )
    purelib = session.run(
        str(python),
        "-c",
        "import sysconfig; print(sysconfig.get_paths()['purelib'])",
        silent=True,
        external=True,
    )
    if isinstance(purelib, str):
        site = Path(purelib.strip())
        size = sum(f.stat().st_size for f in site.rglob("*") if f.is_file())
        session.log(f"fresh venv site-packages: {size} bytes ({size / 2**20:.1f} MiB) in {site}")
    script = Path("scripts/wheel_smoke.py").resolve()
    session.chdir(_scratch(session, "installed"))
    session.run(
        str(python),
        str(script),
        str(wheel),
        f"--binding={binding}",
        "--installed-only",
        external=True,
    )


@nox.session(python=[PRIMARY_PYTHON])
def tests_unit_floors(session: nox.Session) -> None:
    """Run the hostless unit suite with every runtime dependency at its declared floor (#1181).

    The floor smoke (:func:`wheel_smoke_floors`) proves otto imports and runs
    at the floors; most floor breaks are behaviour on a path the smoke never
    takes (an SNMP GET's cleanup, a TLS context urllib3 1.x rejects), and
    only the suite reaches them. Nightly, not per push: the suite is the
    slowest hostless leg.

    The runtime is the floor resolution; the ``dev`` group is the lock's,
    installed ``--no-deps`` less every package the floors pin (its docs
    tooling needs newer versions than otto's floors, and the tests do not
    need it to agree); otto is the editable checkout. The selection is
    ``tests_hostless_unit``'s uninstrumented pair, by name.
    """
    runtime = _floor_requirements(session)
    tmp = _scratch(session, "dev")
    locked_dev = tmp / "dev-requirements.txt"
    session.run_install(
        "uv",
        "export",
        "--frozen",
        "--only-group",
        "dev",
        "--no-emit-project",
        "--no-hashes",
        "--quiet",
        f"--output-file={locked_dev}",
        external=True,
    )
    # Under `-R`/`--no-install` the compile and the export do not run, so the
    # files they write are absent; the installs below would be no-ops anyway.
    if runtime.exists():
        _install_floor_suite(session, runtime, locked_dev, tmp / "dev-extra-requirements.txt")
    session.run(
        "pytest",
        *HOSTLESS_MIDDLE_TEST_ARGS,
        _junitxml(session, "nox-unit-floors"),
        *session.posargs,
        env=_LEAK_ENV,
    )
    session.run(
        "pytest",
        *HOSTLESS_MIDDLE_SERIAL_ARGS,
        _junitxml(session, "nox-unit-floors-serial"),
        *session.posargs,
        env=_LEAK_ENV,
    )


def _install_floor_suite(
    session: nox.Session, runtime: Path, locked_dev: Path, dev_extra: Path
) -> None:
    """Install tests_unit_floors's venv: the floors, the lock's dev group less them, otto."""
    pin = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==")

    def pinned(path: Path) -> "list[tuple[str, str]]":
        """Each ``name==version`` line of *path*, with its canonical name."""
        return [
            (canonicalize_name(m.group(1)), line)
            for line in path.read_text().splitlines()
            if (m := pin.match(line))
        ]

    floor_names = {name for name, _ in pinned(runtime)}
    dev_extra.write_text(
        "".join(f"{line}\n" for name, line in pinned(locked_dev) if name not in floor_names)
    )
    session.install("-r", str(runtime))
    session.install("--no-deps", "-r", str(dev_extra))
    session.install("--no-deps", "-e", ".")


@nox_uv.session(uv_groups=["dev", "lint"])
def lint(session: nox.Session) -> None:
    """Run ruff lint + format checks, then the architecture gates.

    The architecture legs mirror `make lint-arch`'s tach and ast-grep steps:
    tach validates the module dependency contracts in tach.toml (a ratchet
    baseline — see its header); ast-grep enforces the scope-sensitive pattern
    rules in .ast-grep/rules/ and `ast-grep test` checks those rules against
    the pinned snippets in .ast-grep/rule-tests/ (a rule that stops matching
    its own motivating example is a silent gate loss); the duplicate-code
    ratchet (scripts/check_duplicate_code.py) refuses a clone pair pylint's
    similarities checker reports that scripts/duplicate_code_baseline.txt
    does not list, and a listed pair that no longer reports; check-api-keywords
    (scripts/check_api_keywords.py) refuses a public positional parameter
    api/public.toml does not list.
    The tach layer canary runs the installed tach on a throwaway project and
    fails if tach.toml's layer switch stops meaning what tach.toml assumes; the
    monitor boundary canary runs the monitor boundary test's fixtures through
    it and fails if the test no longer counts every import tach counts. Both
    need tach, so they ride here and in lint-arch, not in a pytest lane.
    `check-breaking` (scripts/check_breaking_marks.py) is a live goal of its
    own, not part of `make lint-arch`: it needs a resolved commit range, which
    this session has no opinion about; CI's lint-python job runs it as its
    own step, after this session, with the range computed from the
    workflow's push/pull_request event. Its parse-only pytest legs — the
    layer test, the import-string audit and the
    lazy-export import rules — are not repeated here either: they are
    ordinary unit tests, so the hostless sessions already run them.
    """
    session.run("ruff", "check", ".")
    session.run("ruff", "format", "--check", ".")
    session.run("tach", "check", "--exact")
    session.run("python", "scripts/tach_layer_canary.py")
    session.run("python", "scripts/monitor_boundary_tach_canary.py")
    session.run("ast-grep", "scan", "src/otto", "web/src", "tests", "scripts")
    session.run("ast-grep", "test", "--skip-snapshot-tests")
    session.run("python", "scripts/check_duplicate_code.py")
    session.run("python", "scripts/check_api_keywords.py", "--manifest", "api/public.toml")


@nox.session(venv_backend="none")
def typecheck(session: nox.Session) -> None:
    """Run `make typecheck-python`.

    The Makefile target, so CI checks the registry typing fixtures as the local gate does.
    """
    session.run("make", "typecheck-python", external=True)


@nox_uv.session(python=PYTHON_VERSIONS, uv_no_install_project=True)
def api_dump_invariance(session: nox.Session) -> None:
    """Regenerate the API dump under this Python and compare it with the golden (dump spec §6).

    One leg per interpreter, each under its own PYTHONHASHSEED (the version's
    digits, so never the gate's fixed 0): the committed golden must be
    byte-identical to all five. Runtime dependencies only, and otto is not
    installed, as `check-breaking` regenerates a commit (dump spec §5.2): the
    dump child puts `src` first on its own path.
    """
    seed = str(session.python).replace(".", "")
    session.run(
        "python",
        "scripts/api_snapshot.py",
        "--manifest",
        "api/public.toml",
        "--check",
        "--hash-seed",
        seed,
    )


# Run by `tests_uv_floor` under the pinned interpreter: the package metadata, the
# floor the script printed and the binary `uv_bin()` locates must all agree.
UV_FLOOR_CHECK = """
import importlib.metadata
import sys

from packaging.version import Version

from otto.env.backends import uv_bin, uv_version

floor = Version(sys.argv[1])
package = Version(importlib.metadata.version("uv"))
binary = Version(uv_version(uv_bin()))
print(f"floor {floor}, uv package {package}, uv binary {binary}")
assert package == floor == binary, "the pinned uv is not the declared floor"
"""


@nox_uv.session(python=[PRIMARY_PYTHON], uv_groups=["dev"])
def tests_uv_floor(session: nox.Session) -> None:
    """Run otto's env tests against the oldest uv otto declares support for.

    ``uv`` is a runtime dependency, so the declared uv floor in pyproject.toml
    is an obligation: the Python package API, both missing-installation legs, the
    version probe and the argv otto builds must all hold at the minimum, not
    only at whatever the lock resolved. The floor is read from
    ``scripts/check_docs_dependency_table.py --floor uv``, its one owner, and
    pinned with ``--no-deps``. The check then proves the package and the binary
    ``uv_bin()`` finds both report it (``0.11`` equals ``0.11.0``).

    The tests run through ``pytest`` directly, never ``uv run pytest``: that
    would re-sync the project and undo the pin. ``-p no:tach`` stays in force
    from pyproject's addopts; ``--no-cov`` only drops instrumentation.
    """
    # run_install, so the floor is also read under `--install-only`; it returns
    # None under `--no-install`/`-R`, where `run` reads it instead.
    floor_argv = ("python", "scripts/check_docs_dependency_table.py", "--floor", "uv")
    floor = session.run_install(*floor_argv, silent=True) or session.run(*floor_argv, silent=True)
    if not isinstance(floor, str) or not floor.strip():
        session.error("scripts/check_docs_dependency_table.py --floor uv printed no floor")
    floor = floor.strip()
    session.install("--no-deps", f"uv=={floor}")
    session.run("python", "-c", UV_FLOOR_CHECK, floor)
    session.run(
        "pytest",
        "tests/unit/env",
        "tests/e2e/cli/test_env_e2e.py",
        "--no-cov",
        _junitxml(session, "nox-uv-floor"),
        *session.posargs,
    )


# The Jsonnet template tests under the Go binding. Every path that renders a
# real template is listed here, so both bindings run it.
TEMPLATING_GO_PATHS = (
    "tests/unit/templating",
    "tests/unit/init/test_init_doctor.py",
    "tests/unit/init/test_next_steps.py",
    "tests/unit/cli/test_lab_cli.py",
    "tests/unit/config/test_completion_templated.py",
    "tests/unit/docs/test_templating_example.py",
)
# `python -c` puts the checkout on sys.path, so the lock's one version reader
# imports; the `nox` entry point itself never has it there.
LOCKED_VERSION = (
    "import sys; from pathlib import Path; "
    "from scripts.playwright_version import locked_version; "
    "print(locked_version(Path('uv.lock'), sys.argv[1]))"
)


def _locked_version(session: nox.Session, package: str) -> str:
    """Return *package*'s version in uv.lock, read by ``scripts/playwright_version.py``."""
    argv = (sys.executable, "-c", LOCKED_VERSION, package)
    # run_install, so it is also read under `--install-only`; it returns None
    # under `--no-install`/`-R`, where `run` reads it instead.
    said = session.run_install(*argv, silent=True, external=True) or session.run(
        *argv, silent=True, external=True
    )
    if not isinstance(said, str) or not said.strip():
        session.error(f"uv.lock names no {package} version")
    return said.strip()


@nox_uv.session(python=[PRIMARY_PYTHON], uv_groups=["dev"])
def templating_go(session: nox.Session) -> None:
    """Run the Jsonnet template tests under the Go binding, ``gojsonnet``.

    The markers in pyproject.toml install one binding per platform: the C++
    ``jsonnet`` on an x86_64 CI runner, ``gojsonnet`` on Linux aarch64. This
    session swaps the C++ binding out for the ``gojsonnet`` uv.lock pins (it
    ships manylinux2014 x86_64 wheels for every Python otto supports), so every pull request
    exercises both. ``OTTO_TEST_JSONNET_BINDING`` makes the templating
    conftest assert the swap took. Where ``jsonnet`` is not installed (the
    aarch64 dev VM), the uninstall is a no-op and the same suite runs.

    ``pytest`` runs directly, never ``uv run pytest``, which would re-sync the
    project and undo the swap. The selection is ``HOSTLESS_CLAUSES``, run
    single-process (``-n0``) in one leg, so ``serial_timing`` is NOT excluded:
    the paths hold no such test today, and the lane-leg membership pin forbids
    a serial leg that selects nothing. One ``-n0`` leg keeps a templating test
    marked ``serial_timing`` running, alone, the day one lands, for ~10 s more
    than ``-n auto`` over these ~540 tests.
    """
    session.run_install(
        "uv", "pip", "uninstall", "--python", session.virtualenv.location, "jsonnet", external=True
    )
    # The locked gojsonnet, so a bump of the lock moves the swap with it.
    session.install(f"gojsonnet=={_locked_version(session, 'gojsonnet')}")
    env = {"OTTO_TEST_JSONNET_BINDING": "gojsonnet"}
    session.run(
        "pytest",
        *TEMPLATING_GO_PATHS,
        "-m",
        HOSTLESS_CLAUSES,
        "-n0",
        "--no-cov",
        _junitxml(session, "nox-templating-go"),
        *session.posargs,
        env=env,
    )


@nox_uv.session(uv_groups=["dev"])
def docs(session: nox.Session) -> None:
    """Build HTML docs (warnings as errors) and run Sphinx doctests."""
    # Fast RST structural pre-check (title/underline, etc.) before the slow build.
    session.run("doc8", "docs/")
    session.run("python", "scripts/lint_markdown_doctests.py", "docs/")
    session.run("python", "scripts/lint_docs_versions.py", "docs/")
    session.run("python", "scripts/lint_docs_citations.py")
    session.run("python", "scripts/check_docs_dependency_table.py")
    session.run("python", "scripts/check_docs_wheel_matrix.py")
    session.run("python", "scripts/refresh_docs_captures.py", "--check", "--labless")
    session.run("python", "scripts/check_docs_examples.py")
    # -E (fresh env) + -a (write all) so the build matches a clean checkout.
    # -a deletes no page, so the tree goes first: a page a moved .rst left
    # behind would reach the built-site check below.
    shutil.rmtree("docs/_build/html", ignore_errors=True)
    session.run("sphinx-build", "-E", "-a", "-W", "-b", "html", "docs/", "docs/_build/html")
    # The built site's stability marks and pre-1.0 notice, read from the HTML
    # and objects.inv rather than from the hooks that wrote them.
    session.run("python", "scripts/check_docs_api_marks.py", "docs/_build/html")
    session.run("sphinx-build", "-E", "-b", "doctest", "docs/", "docs/_build/doctest")
    # `-p no:tach` re-stated: the override drops pyproject's addopts whole, and
    # only an addopts/CLI `-p` protects plugin load (issue #193). Pinned by
    # tests/unit/test_lane_invariants.py.
    #
    # Hermetic and without live logging, for the reasons at the Makefile's
    # `doctest-src` twin: a `None` value makes nox unset the variable.
    session.run(
        "pytest",
        "-p",
        "no:cacheprovider",
        "-o",
        "addopts=--doctest-modules -p no:tach",
        "-o",
        "log_cli=false",
        "src/otto",
        env={name: None for name in os.environ if name.startswith("OTTO_") or name == "OTTOPATH"},
    )


@nox_uv.session(uv_groups=["dev"])
def docs_captures(session: nox.Session) -> None:
    """Refresh or --check the Getting Started command captures (args pass through).

    ``nox -s docs_captures -- --check --labless`` is what the docs gate runs;
    without ``--labless`` the bed must be reachable.
    """
    session.run("python", "scripts/refresh_docs_captures.py", *session.posargs)
