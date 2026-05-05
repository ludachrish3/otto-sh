"""Nox sessions for otto-sh.

Run all default sessions:
    uv run nox

Run a single Python version's tests:
    uv run nox -s tests-3.12

List available sessions:
    uv run nox --list
"""

from __future__ import annotations

import nox
import nox_uv

PYTHON_VERSIONS = ["3.10", "3.11", "3.12", "3.13", "3.14"]

nox.options.default_venv_backend = "uv"
# `lint` is intentionally opt-in (`nox -s lint`) until the existing ruff
# violations under tests/ and src/otto/ have been swept; running it by
# default would block the matrix on pre-existing style debt.
nox.options.sessions = ["tests", "typecheck", "docs"]

UNIT_TEST_ARGS = (
    "tests/unit",
    "-m",
    "not integration and not hops",
    "--cov-fail-under=80",
)


@nox_uv.session(python=PYTHON_VERSIONS, uv_groups=["dev"])
def tests(session: nox.Session) -> None:
    """Run unit tests (no Vagrant VMs) under each supported Python."""
    session.run("pytest", *UNIT_TEST_ARGS, *session.posargs)


@nox_uv.session(python=PYTHON_VERSIONS, uv_groups=["dev"])
def tests_all(session: nox.Session) -> None:
    """Run the full suite — unit + integration + hops — under each supported Python.

    Requires the dev VM with Vagrant hosts up; not run in CI. Pytest's
    default ``testpaths`` and addopts (from pyproject.toml) cover the full
    tree, so no marker filter is passed here. Coverage threshold matches
    ``make coverage`` (85%).
    """
    session.run("pytest", "--cov-fail-under=85", *session.posargs)


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
    session.run("sphinx-build", "-W", "-b", "html", "docs/", "docs/_build/html")
    session.run("sphinx-build", "-b", "doctest", "docs/", "docs/_build/doctest")
