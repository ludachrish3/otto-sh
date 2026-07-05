"""Nox sessions for otto-sh.

Run all default sessions:
    uv run nox

Run a single Python version's tests:
    uv run nox -s tests_unit-3.12

List available sessions:
    uv run nox --list
"""

from pathlib import Path

import nox
import nox_uv

PYTHON_VERSIONS = ["3.10", "3.11", "3.12", "3.13", "3.14"]

# JUnit XML is written into a per-target subdirectory of reports/junit/ named
# after the `make` target that drives the session (nox-unit, nox-unix,
# nox-embedded, nox), matching the layout the standalone Makefile test targets
# use. Each Python version is a separate pytest process, so the file inside the
# subdir keeps the full session name (e.g. reports/junit/nox-unit/tests_unit-3.12.xml)
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

# Coverage floors mirror the Makefile: the no-testbed CI gate (tests_hostless)
# gates at 85 (CI_COVERAGE_THRESHOLD); every full-suite path at 92
# (COVERAGE_THRESHOLD). Keep these in sync with the Makefile if either moves.

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
    "--cov-fail-under=85",
)


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
    instead. Coverage threshold matches ``make coverage`` (92%).
    """
    session.run(
        "pytest",
        "-m",
        "not browser",
        "--cov-fail-under=92",
        _junitxml(session, "nox"),
        *session.posargs,
    )


@nox_uv.session(python=["3.12"], uv_groups=["dev"])
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

    This suite drives the built React dashboard (`src/otto/monitor/static/
    dist/`) through a real `MonitorServer` — there's no legacy static
    fallback since the Task 9 cutover, so `dist/` must already exist before
    this session runs. Building it needs Node/npm (`make web-install` +
    `make web`), which nox-uv's per-session venvs (Python-only, one per
    `PYTHON_VERSIONS` entry) have no way to provision. Rather than bolt a
    Node toolchain onto a nox session, that step lives directly in
    `.github/workflows/ci.yml`'s `dashboard` job, ahead of the nox call;
    `make dashboard` (the local/dev entrypoint) carries the same prerequisite.
    Each engine's browser system libraries are installed by that job via
    `playwright install --with-deps <browser>` ahead of this session.
    """
    session.run("playwright", "install", browser)
    session.run(
        "pytest",
        "tests/e2e/monitor/dashboard",
        "-m",
        "browser",
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
