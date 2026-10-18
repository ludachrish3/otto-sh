"""Nox sessions for otto-sh.

Run all default sessions:
    uv run nox

Run a single Python version's tests:
    uv run nox -s tests_unit-3.10

List available sessions:
    uv run nox --list
"""

import os
import shutil
from pathlib import Path

import nox
import nox_uv

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

# The floor and the ceiling. The floor runs everything and gates coverage
# (HOSTLESS_*_ARGS below); the ceiling runs every product test with the
# `interpreter_agnostic` family deselected and coverage off
# (HOSTLESS_CANARY_*_ARGS); the versions between run the trimmed pair
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


def _junitxml(session: nox.Session, group: str) -> str:
    """Return a `--junitxml=` arg pointing at reports/junit/<group>/<session>.xml."""
    return f"--junitxml={JUNIT_DIR / group / f'{session.name}.xml'}"


nox.options.default_venv_backend = "uv"
# `lint` runs ruff check + format --check; it is part of the default gate now
# that the strict config (select=ALL minus the deny-list) is green.
nox.options.sessions = ["lint", "tests_hostless", "typecheck", "docs"]

# Coverage floors — every one a COMBINED line+branch number, since .coveragerc
# measures branches (`branch = true`). ONE floor lives here: tests_hostless
# gates at 94.75 on the PRIMARY Python — the Makefile's CI_COVERAGE_THRESHOLD,
# and the floor `make coverage-hostless` enforces on the same selection, so
# it's the same number; tests/unit/test_coverage_floors.py holds the pair
# equal, both above their codified minimums (the Makefile comment on
# COVERAGE_THRESHOLD has the measurements), and holds this to be the ONLY
# floor in the file. Every other leg runs with coverage OFF: the canary's job
# is "does otto still work on the newest interpreter", the interior's trimmed
# selection cannot meet a floor, and tests_all's primary leg is the selection
# `make coverage` already gates at 95.5 on the same tree — the coverage stamp
# (scripts/coverage_stamp.py) is the release's evidence of that run. tests_all
# used to carry a second, lower floor of its own (92: the same run minus the
# dashboard --cov-append fold-in) that the release read; owner ruling 5
# (#566) deleted it, and with it the leg's instrumentation. Instrumenting a
# leg whose report nothing reads costs it ~14 % of wall time (the canary
# session measured 552 s with coverage, 473 s without) for no reader.

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
# on tests_hostless-3.11 (see _dashboard_harness._reap_orphaned_transports and
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
# pytest invocation (tests_hostless, tests_all) excludes `browser` instead.
#
# serial_timing (wall-clock discriminators — sibling xdist workers can
# counterfeit their slow arm as a false red) is excluded from every parallel
# invocation and re-appended by a paired `-n0` leg in the same session —
# exclusion alone would make the tests CI-invisible. The coverage gate rides
# the SERIAL args (the last leg, folding in via --cov-append), so the
# threshold judges the session's whole run. The two marker expressions are a
# hand-kept pair like DASHBOARD_MARKER_EXPR: if the resource exclusions
# change, change both — the serial leg carries EVERY clause its parallel twin
# does, `not busybox and not conformance` included, which is pinned for every
# paired leg in this file and the Makefile by
# tests/unit/test_lane_invariants.py (a serial leg that dropped them would
# fetch artifacts in a lane whose parallel half excludes them).
HOSTLESS_TEST_ARGS = (
    "tests/unit",
    "tests/e2e",
    "-m",
    (
        "not integration and not embedded and not stability and not browser "
        "and not busybox and not conformance and not kmodcov and not serial_timing"
    ),
    "--cov-fail-under=0",
)
HOSTLESS_SERIAL_ARGS = (
    "tests/unit",
    "tests/e2e",
    "-m",
    (
        "serial_timing and not integration and not embedded and not stability and not browser "
        "and not busybox and not conformance and not kmodcov"
    ),
    "-n0",
    "--cov-append",
    "--cov-fail-under=94.75",
)

# The CANARY Python's pair: the bookend selection minus the family, coverage
# off. Same paths (the e2e tier included — the newest interpreter runs every
# product test the primary does), the same clauses plus `not
# interpreter_agnostic` (the family runs on the primary only, #566 Q2), and
# `--no-cov` in place of the gating flags.
# tests/unit/test_python_matrix_tiering.py pins that this pair is the bookend
# pair minus exactly those two things.
HOSTLESS_CANARY_TEST_ARGS = (
    "tests/unit",
    "tests/e2e",
    "-m",
    (
        "not integration and not embedded and not stability and not browser "
        "and not busybox and not conformance and not kmodcov and not serial_timing "
        "and not interpreter_agnostic"
    ),
    "--no-cov",
)
HOSTLESS_CANARY_SERIAL_ARGS = (
    "tests/unit",
    "tests/e2e",
    "-m",
    (
        "serial_timing and not integration and not embedded and not stability and not browser "
        "and not busybox and not conformance and not kmodcov and not interpreter_agnostic"
    ),
    "-n0",
    "--no-cov",
)

# The INTERIOR Pythons' pair (every PYTHON_VERSIONS entry not in
# BOOKEND_PYTHONS). Measured 2026-09-11: a full hostless leg is ~1,420 s of CPU
# per interior Python, and 65 % of it is work whose subject is not otto under
# THIS interpreter — the tests/e2e tier (real `otto` subprocesses, real `pip`
# venv builds) and the repo-policy guards that re-collect the whole tree in a
# subprocess. Those prove the same thing on every Python, so the interior legs
# run the tests/unit tier with `interpreter_agnostic` deselected and coverage
# off; the primary keeps the full selection above; the canary keeps the paths
# but drops the family too. Each middle expression carries EVERY clause of its
# bookend twin plus the trim, and the primary pair never mentions the marker
# (a mention there would stop those tests running anywhere in the matrix) —
# both pinned by tests/unit/test_python_matrix_tiering.py, and the constant
# pairing itself by tests/unit/test_lane_invariants.py's serial-leg scanner.
HOSTLESS_MIDDLE_TEST_ARGS = (
    "tests/unit",
    "-m",
    (
        "not integration and not embedded and not stability and not browser "
        "and not busybox and not conformance and not kmodcov and not serial_timing "
        "and not interpreter_agnostic"
    ),
    "--no-cov",
)
HOSTLESS_MIDDLE_SERIAL_ARGS = (
    "tests/unit",
    "-m",
    (
        "serial_timing and not integration and not embedded and not stability and not browser "
        "and not busybox and not conformance and not kmodcov and not interpreter_agnostic"
    ),
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
    "not stability and not busybox and not conformance and not kmodcov and not serial_timing",
)
UNIT_SERIAL_ARGS = (
    "tests/unit",
    "-m",
    "serial_timing and not stability and not busybox and not conformance and not kmodcov",
    "-n0",
    "--cov-append",
)
UNIT_NONPRIMARY_TEST_ARGS = (
    "tests/unit",
    "-m",
    (
        "not stability and not busybox and not conformance and not kmodcov and not serial_timing "
        "and not interpreter_agnostic"
    ),
    "--no-cov",
)
UNIT_NONPRIMARY_SERIAL_ARGS = (
    "tests/unit",
    "-m",
    (
        "serial_timing and not stability and not busybox and not conformance and not kmodcov "
        "and not interpreter_agnostic"
    ),
    "-n0",
    "--no-cov",
)
INTEGRATION_TEST_ARGS = (
    "tests/unit",
    "tests/integration",
    "-m",
    "not stability and not busybox and not conformance and not kmodcov and not serial_timing",
)
INTEGRATION_SERIAL_ARGS = (
    "tests/unit",
    "tests/integration",
    "-m",
    "serial_timing and not stability and not busybox and not conformance and not kmodcov",
    "-n0",
    "--cov-append",
)
INTEGRATION_NONPRIMARY_TEST_ARGS = (
    "tests/unit",
    "tests/integration",
    "-m",
    (
        "not stability and not busybox and not conformance and not kmodcov and not serial_timing "
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
        "and not interpreter_agnostic"
    ),
    "-n0",
    "--no-cov",
)
TESTS_ALL_TEST_ARGS = (
    "-m",
    (
        "not browser and not stability and not busybox and not conformance and not kmodcov "
        "and not serial_timing"
    ),
    "--no-cov",
)
TESTS_ALL_SERIAL_ARGS = (
    "-m",
    (
        "serial_timing and not browser and not stability and not busybox and not conformance "
        "and not kmodcov"
    ),
    "-n0",
    "--no-cov",
)
TESTS_ALL_NONPRIMARY_TEST_ARGS = (
    "-m",
    (
        "not browser and not stability and not busybox and not conformance and not kmodcov "
        "and not serial_timing and not interpreter_agnostic"
    ),
    "--no-cov",
)
TESTS_ALL_NONPRIMARY_SERIAL_ARGS = (
    "-m",
    (
        "serial_timing and not browser and not stability and not busybox and not conformance "
        "and not kmodcov and not interpreter_agnostic"
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
def tests_hostless(session: nox.Session) -> None:
    """Run the no-testbed set (tests/unit + no-VM e2e) — the CI gate.

    Identical selection to the former ``tests_unit``: every test that needs no
    testbed across ``tests/unit`` and ``tests/e2e``. This is what
    ``.github/workflows/ci.yml`` runs and what ``nox.options.sessions`` defaults
    to. Auto-includes any future no-testbed e2e test.

    Tiered by interpreter: the PRIMARY_PYTHON runs the full pair with the
    coverage gate, the CANARY_PYTHON the same paths with the
    `interpreter_agnostic` family deselected and coverage off, and
    the interior versions the trimmed HOSTLESS_MIDDLE pair (see its comment for
    what is trimmed and why). The junit group names are shared, so
    reports/junit/nox-hostless/<session>.xml is one file per Python either way.
    """
    # Each branch spells its bundles by NAME: the lane scanners in
    # tests/unit/test_tier_marker_invariants.py expand `*MODULE_CONSTANT` and
    # drop anything else, so a bundle passed through a local would read as a
    # path-less, marker-less `pytest` — and be judged as reaching every tier.
    parallel_junit = _junitxml(session, "nox-hostless")
    serial_junit = _junitxml(session, "nox-hostless-serial")
    if session.python == PRIMARY_PYTHON:
        session.run("pytest", *HOSTLESS_TEST_ARGS, parallel_junit, *session.posargs, env=_LEAK_ENV)
        session.run("pytest", *HOSTLESS_SERIAL_ARGS, serial_junit, *session.posargs, env=_LEAK_ENV)
    elif session.python == CANARY_PYTHON:
        session.run(
            "pytest", *HOSTLESS_CANARY_TEST_ARGS, parallel_junit, *session.posargs, env=_LEAK_ENV
        )
        session.run(
            "pytest", *HOSTLESS_CANARY_SERIAL_ARGS, serial_junit, *session.posargs, env=_LEAK_ENV
        )
    else:
        session.run(
            "pytest", *HOSTLESS_MIDDLE_TEST_ARGS, parallel_junit, *session.posargs, env=_LEAK_ENV
        )
        session.run(
            "pytest", *HOSTLESS_MIDDLE_SERIAL_ARGS, serial_junit, *session.posargs, env=_LEAK_ENV
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
    ``addopts``) trips any such regression at PR time instead of waiting for the
    nightly. The ``interpreter_agnostic`` guards are left out: they hold no
    process-global state, and repeating them is most of the job's wall clock
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
        # expensive shape left in the family, for a question they cannot answer. CI
        # measured this job at 22 min, the longest in the workflow. Pinned by
        # tests/unit/test_python_matrix_tiering.py.
        "-m",
        "not stability and not busybox and not conformance and not kmodcov "
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
        "and not serial_timing",
        _junitxml(session, "nox-unix"),
        *session.posargs,
    )
    session.run(
        "pytest",
        "-m",
        "serial_timing and integration and not embedded and not stability and not chaos "
        "and not kmodcov",
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
        "embedded and not stability and not chaos and not kmodcov",
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
    `file://` lane and, since Task 10, the served-CSP lane against a real
    HTTP server; see `tests/_fixtures/_csp_server.py`). This is what puts the
    coverage-report suite's browser matrix into CI: this session is what
    `.github/workflows/ci.yml`'s `dashboard` job runs per engine.

    Both suites drive a BUILT React bundle (`src/otto/_webassets/monitor/dist/`
    and `src/otto/_webassets/covapp/dist/` respectively) —
    there's no legacy static fallback since the Task 9 cutover, so both
    `dist/` dirs must already exist before this session runs. Building them
    needs Node/npm (`make web`, which installs web/'s dependencies itself),
    which nox-uv's per-session venvs (Python-only, one per `PYTHON_VERSIONS`
    entry) have no way to provision. Rather than bolt a Node toolchain onto a
    nox session, that step lives directly in `.github/workflows/ci.yml`'s
    `dashboard` job, ahead of the nox call; `make dashboard` (the local/dev
    entrypoint) carries the same prerequisite —
    and, unlike this session, rebuilds a dist that has gone stale against
    web/src/ rather than only one that is missing.
    Each engine's browser system libraries are installed by that job via
    `playwright install --with-deps <browser>` ahead of this session.
    """
    session.run("playwright", "install", browser)
    session.run(
        "pytest",
        "tests/e2e/monitor/dashboard",
        "tests/e2e/cov/report_browser",
        "-m",
        DASHBOARD_MARKER_EXPR,
        "--browser",
        browser,
        # CI-only lane: unlike `make dashboard` (which writes coverage data for
        # `make coverage` to fold in via --cov-append), this job stands alone
        # with nothing to combine into, so coverage stays off entirely.
        "--no-cov",
        _junitxml(session, f"dashboard-{browser}"),
        *session.posargs,
    )


@nox.session(python=[PRIMARY_PYTHON])
def wheel_smoke(session: nox.Session) -> None:
    """Smoke the built wheel in a venv of runtime dependencies only (scripts/wheel_smoke.py).

    Every other session installs the `dev` group into an editable otto, so a
    development dependency or an auto-loaded plugin can hide what an
    installed otto lacks (#593: pytest-cov's `--no-cov`). This one installs
    `dist/*.whl` with `--no-deps` over the locked runtime dependencies
    (`uv export --no-dev`) — what `pip install otto-sh` resolves, pinned —
    and runs the smoke from a scratch directory, so neither `src/` nor the
    checkout's tests are importable.

    It needs a built wheel. Building one needs the web assets (the build
    backend refuses an asset-less wheel), so the Node step stays outside
    this Python-only session, as for `dashboard`: `make wheel-smoke` builds
    first, and so does CI's `wheel-smoke` job.
    """
    wheels = sorted(Path("dist").glob("*.whl"))
    if len(wheels) != 1:
        session.error(f"expected one wheel in dist/, found {len(wheels)}: run `make wheel-smoke`")
    runtime = Path(session.create_tmp()) / "runtime-requirements.txt"
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
    session.install("-r", str(runtime))
    session.install("--no-deps", str(wheels[0].resolve()))
    script = Path("scripts/wheel_smoke.py").resolve()
    session.chdir(session.create_tmp())
    session.run("python", str(script))


@nox_uv.session(uv_groups=["dev", "lint"])
def lint(session: nox.Session) -> None:
    """Run ruff lint + format checks, then the architecture gates.

    The architecture legs mirror `make lint-arch`'s tach and ast-grep steps:
    tach validates the module dependency contracts in tach.toml (a ratchet
    baseline — see its header); ast-grep enforces the scope-sensitive pattern
    rules in .ast-grep/rules/ and `ast-grep test` checks those rules against
    the pinned snippets in .ast-grep/rule-tests/ (a rule that stops matching
    its own motivating example is a silent gate loss).
    The tach layer canary runs the installed tach on a throwaway project and
    fails if tach.toml's layer switch stops meaning what tach.toml assumes; the
    monitor boundary canary runs the monitor boundary test's fixtures through
    it and fails if the test no longer counts every import tach counts. Both
    need tach, so they ride here and in lint-arch, not in a pytest lane.
    `make lint-arch`'s `check-breaking` leg (scripts/check_breaking_marks.py)
    is deliberately NOT run here — it needs a resolved commit range, which
    this session has no opinion about; CI's lint-python job runs it as its
    own step, after this session, with the range computed from the
    workflow's push/pull_request event. Its parse-only pytest legs — the
    layer test, the import-string audit and the
    lazy-export import rules — are not repeated here either: they are
    ordinary unit tests, so the `tests_hostless` session already runs them.
    """
    session.run("ruff", "check", ".")
    session.run("ruff", "format", "--check", ".")
    session.run("tach", "check", "--exact")
    session.run("python", "scripts/tach_layer_canary.py")
    session.run("python", "scripts/monitor_boundary_tach_canary.py")
    session.run("ast-grep", "scan", "src/otto", "web/src", "tests")
    session.run("ast-grep", "test", "--skip-snapshot-tests")


@nox_uv.session(uv_groups=["dev"])
def typecheck(session: nox.Session) -> None:
    """Run ty type checker on src/."""
    session.run("ty", "check")


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


@nox_uv.session(uv_groups=["dev"])
def docs(session: nox.Session) -> None:
    """Build HTML docs (warnings as errors) and run Sphinx doctests."""
    # Fast RST structural pre-check (title/underline, etc.) before the slow build.
    session.run("doc8", "docs/")
    session.run("python", "scripts/lint_markdown_doctests.py", "docs/")
    session.run("python", "scripts/lint_docs_versions.py", "docs/")
    session.run("python", "scripts/check_docs_dependency_table.py")
    session.run("python", "scripts/check_docs_wheel_matrix.py")
    session.run("python", "scripts/refresh_docs_captures.py", "--check", "--labless")
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
