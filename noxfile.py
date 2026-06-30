"""Nox sessions for otto-sh.

Run all default sessions:
    uv run nox

Run a single Python version's tests:
    uv run nox -s tests_unit-3.12

List available sessions:
    uv run nox --list
"""

from __future__ import annotations

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
nox.options.sessions = ["lint", "tests_unit", "typecheck", "docs"]

# Coverage floors mirror the Makefile: the *-unit paths gate at 85
# (CI_COVERAGE_THRESHOLD), every full-suite path at 92 (COVERAGE_THRESHOLD).
# Keep these in sync with the Makefile if either threshold moves.
UNIT_TEST_ARGS = (
    "tests/unit",
    "tests/e2e",
    "-m",
    "not integration and not embedded",
    "--cov-fail-under=85",
)


@nox_uv.session(python=PYTHON_VERSIONS, uv_groups=["dev"])
def tests_unit(session: nox.Session) -> None:
    """Run unit tests (no Vagrant VMs) under each supported Python."""
    session.run("pytest", *UNIT_TEST_ARGS, _junitxml(session, "nox-unit"), *session.posargs)


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
    default ``testpaths`` and addopts (from pyproject.toml) cover the full
    tree, so no marker filter is passed here. Coverage threshold matches
    ``make coverage`` (92%).
    """
    session.run("pytest", "--cov-fail-under=92", _junitxml(session, "nox"), *session.posargs)


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
