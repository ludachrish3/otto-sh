"""Nox sessions for otto-sh.

Run all default sessions:
    uv run nox

Run a single Python version's tests:
    uv run nox -s tests_unit-3.10

List available sessions:
    uv run nox --list
"""

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

# Coverage floors. tests_hostless gates at 90 — the Makefile's
# CI_COVERAGE_THRESHOLD, and the floor `make coverage-hostless` enforces on the
# same test selection, so it's the same number. tests_all gates at 92, BELOW
# `make coverage`'s 95 (COVERAGE_THRESHOLD): `make coverage` folds the
# dashboard browser process's Python coverage in via --cov-append, which these
# browser-excluded nox sessions don't, so their achievable number is lower.
# Keep tests_hostless in step with CI_COVERAGE_THRESHOLD; revisit tests_all if
# COVERAGE_THRESHOLD or that fold-in changes.

# browser (Playwright) tests always run as their own pytest process — sync
# Playwright keeps an event loop running in the worker main thread for the
# whole session, which breaks pytest-asyncio tests that share the process.
# `dashboard` (below) is that dedicated process; every other session whose
# paths/markers could otherwise co-select browser + async tests in one
# pytest invocation (tests_hostless, tests_all) excludes `browser` instead.
HOSTLESS_TEST_ARGS = (
    "tests/unit",
    "tests/e2e",
    "-m",
    "not integration and not embedded and not stability and not browser",
    "--cov-fail-under=90",
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


@nox_uv.session(python=PYTHON_VERSIONS, uv_groups=["dev"])
def tests_unit(session: nox.Session) -> None:
    """Run the unit *level* tier (tests/unit only; no testbed) under each Python."""
    session.run(
        "pytest",
        "tests/unit",
        "-m",
        "not stability",
        _junitxml(session, "nox-unit"),
        *session.posargs,
    )


@nox_uv.session(python=PYTHON_VERSIONS, uv_groups=["dev"])
def tests_integration(session: nox.Session) -> None:
    """Run the unit + integration *level* tiers (tests/unit + tests/integration).

    Cumulative directory-based level: needs the full lab (the integration tier
    includes the Linux-VM, Zephyr, and Docker tests). No coverage gate — a single
    environment exercises only a slice of otto.
    """
    session.run(
        "pytest",
        "tests/unit",
        "tests/integration",
        "-m",
        "not stability",
        _junitxml(session, "nox-integration"),
        *session.posargs,
    )


@nox_uv.session(python=PYTHON_VERSIONS, uv_groups=["dev"])
def tests_hostless(session: nox.Session) -> None:
    """Run the no-testbed set (tests/unit + no-VM e2e) — the CI gate.

    Identical selection to the former ``tests_unit``: every test that needs no
    testbed across ``tests/unit`` and ``tests/e2e``. This is what
    ``.github/workflows/ci.yml`` runs and what ``nox.options.sessions`` defaults
    to. Auto-includes any future no-testbed e2e test.
    """
    session.run("pytest", *HOSTLESS_TEST_ARGS, _junitxml(session, "nox-hostless"), *session.posargs)


@nox_uv.session(python=[PRIMARY_PYTHON], uv_groups=["dev"])
def tests_unit_repeat(session: nox.Session) -> None:
    """Repeat the whole ``tests/unit`` tree in one process to catch state leaks.

    Many unit tests mutate process-global state and rely on it being pristine:
    they register into the ``otto.registry.Registry`` singletons (``INSTRUCTIONS``,
    ``LOADER_CLASSES``, ``CLI_COMMANDS``, ``SUITES`` …) via ``@instruction`` /
    ``register_*`` / ``OttoSuite`` auto-registration, import packages/repos from
    a ``tmp_path``, or ``monkeypatch.delitem`` an ``otto.cli.*`` submodule from
    ``sys.modules``. A test that leaves any of that behind is invisible to a
    single CI pass but breaks the second time it runs in the same process —
    which is why such leaks only ever surfaced in the nightly ``--count`` matrix,
    several nights after landing (issue #108, and the earlier SUITES leak).

    Running the whole no-VM ``tests/unit`` tree twice in one process
    (``--count=2 --repeat-scope=session``, single-process via a cleared
    ``addopts``) trips any such regression at PR time instead of waiting for the
    nightly. Single-process is deliberate: under ``-n auto`` the repeated items
    scatter across workers, so within-module collisions become probabilistic and
    cross-module leaks (a later test broken by an earlier one) are masked
    entirely — exactly how the nightly missed the ``test_root_group`` →
    ``test_listing`` desync. One Python is sufficient (isolation leakage is
    interpreter-version-independent — the primary, 3.10); clearing ``addopts``
    drops the repo-wide
    ``-n auto`` / coverage / ``--doctest-modules`` so the repeat is single-process
    (strictest accumulation) and quick. Supersedes the former
    ``tests_suite_repeat`` (``tests/unit/suite`` ⊂ ``tests/unit``).
    """
    session.run(
        "pytest",
        "tests/unit",
        "-o",
        "addopts=",
        "--count=2",
        "--repeat-scope=session",
        _junitxml(session, "nox-unit-repeat"),
        *session.posargs,
    )


@nox_uv.session(python=PYTHON_VERSIONS, uv_groups=["dev"])
def tests_unix(session: nox.Session) -> None:
    """Run the Unix-VM integration suite (incl. multi-hop) under each Python.

    Requires the dev VM with Vagrant hosts up; not run in CI. No coverage gate
    is enforced — a single environment exercises only a slice of otto.
    """
    session.run(
        "pytest",
        "-m",
        "integration and not embedded",
        _junitxml(session, "nox-unix"),
        *session.posargs,
    )


@nox_uv.session(python=PYTHON_VERSIONS, uv_groups=["dev"])
def tests_embedded(session: nox.Session) -> None:
    """Run the embedded (Zephyr) suite under each supported Python.

    Requires the Vagrant lab (zephyr VM) up; not run in CI. Serialization is
    handled by the per-device xdist_group + console lock in
    tests/integration/host/conftest.py, so no -n0 is forced. No coverage gate.
    """
    session.run(
        "pytest",
        "-m",
        "embedded",
        _junitxml(session, "nox-embedded"),
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
    (the SIGSTOP-wedge test stops tomato's sshd; any other worker's fresh
    ssh to tomato then times out) — they own the bed only in the dedicated
    `make stability-tunnel` lane. Coverage threshold is 92% — below
    ``make coverage``'s 95% because this browser-excluded session omits the
    dashboard --cov-append fold-in (see the module-level coverage-floor
    note).
    """
    session.run(
        "pytest",
        "-m",
        "not browser and not stability",
        "--cov-fail-under=92",
        _junitxml(session, "nox"),
        *session.posargs,
    )


@nox_uv.session(python=[PRIMARY_PYTHON], uv_groups=["dev"])
@nox.parametrize("browser", ["chromium", "firefox", "webkit"])
def dashboard(session: nox.Session, browser: str) -> None:
    """Run the monitor-dashboard browser e2e suite for one engine.

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

    This suite drives the built React dashboard (`src/otto/monitor/static/
    dist/`) through a real `MonitorServer` — there's no legacy static
    fallback since the Task 9 cutover, so `dist/` must already exist before
    this session runs. Building it needs Node/npm (`make web`, which installs
    web/'s dependencies itself), which nox-uv's per-session venvs (Python-only,
    one per `PYTHON_VERSIONS` entry) have no way to provision. Rather than bolt
    a Node toolchain onto a nox session, that step lives directly in
    `.github/workflows/ci.yml`'s `dashboard` job, ahead of the nox call;
    `make dashboard` (the local/dev entrypoint) carries the same prerequisite —
    and, unlike this session, rebuilds a dist that has gone stale against
    web/src/ rather than only one that is missing.
    Each engine's browser system libraries are installed by that job via
    `playwright install --with-deps <browser>` ahead of this session.
    """
    session.run("playwright", "install", browser)
    session.run(
        "pytest",
        "tests/e2e/monitor/dashboard",
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


@nox_uv.session(uv_groups=["dev"])
def lint(session: nox.Session) -> None:
    """Run ruff lint + format checks."""
    session.run("ruff", "check", ".")
    session.run("ruff", "format", "--check", ".")


@nox_uv.session(uv_groups=["dev"])
def typecheck(session: nox.Session) -> None:
    """Run ty type checker on src/."""
    session.run("ty", "check")


@nox_uv.session(uv_groups=["dev"])
def docs(session: nox.Session) -> None:
    """Build HTML docs (warnings as errors) and run Sphinx doctests."""
    # Fast RST structural pre-check (title/underline, etc.) before the slow build.
    session.run("doc8", "docs/")
    session.run("python", "scripts/lint_markdown_doctests.py", "docs/")
    # -E (fresh env) + -a (write all) so the build matches a clean checkout.
    session.run("sphinx-build", "-E", "-a", "-W", "-b", "html", "docs/", "docs/_build/html")
    session.run("sphinx-build", "-E", "-b", "doctest", "docs/", "docs/_build/doctest")
    session.run("pytest", "-p", "no:cacheprovider", "-o", "addopts=--doctest-modules", "src/otto")
