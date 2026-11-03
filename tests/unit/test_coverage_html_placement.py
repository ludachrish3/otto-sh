"""The HTML coverage report is written by the ``make coverage-*`` targets, and only there.

``pyproject.toml``'s ``addopts`` used to carry ``--cov-report html``, so EVERY
pytest invocation in the tree — each nox leg, each ad-hoc run, each of the
ten legs a `make release` drives — finished by writing the 291-file,
29 MB tree under ``reports/coverage/html`` that only ``make coverage``'s
reader ever opens. Measured 2026-09-11: 6 s per invocation on a warm local
disk from a small data file, more from a full run's per-test contexts, and
291 file writes per leg on a network filesystem. The console report
(``term-missing``) stays global: it is what a developer reads at the end of
any run.

pytest-cov's ``--cov-report=`` (empty) suppresses reporting only when it is
the SOLE report option, so a lane cannot opt OUT of a global html entry —
the report has to opt IN per lane instead. This pins that arrangement:
``addopts`` names no html report, and every ``coverage-*`` target's LAST
pytest leg asks for one (the last leg, because the serial ``-n0`` leg folds
in via ``--cov-append`` and only the final data file holds the lane's whole
run — the same reason ``--cov-fail-under`` rides that leg). The one
exception is pinned by name: a ``coverage-*`` target whose every pytest leg
carries ``--no-cov`` measures nothing, so it owes no report (``coverage-bed``,
the uninstrumented bed half of the coverage selection, #797).
"""

import re
import shlex

import pytest

from tests._fixtures.make_recipes import makefile_live_recipe
from tests._fixtures.paths import CHECKOUT_ROOT
from tests.unit.test_tier_marker_invariants import _makefile_pytest_invocations

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - 3.10 only, otto's floor
    import tomli as tomllib

pytestmark = pytest.mark.interpreter_agnostic


def addopts_report_kinds(pyproject_text: str) -> "list[str]":
    """The ``--cov-report`` kinds ``addopts`` carries, in order."""
    ini = tomllib.loads(pyproject_text)
    tokens = shlex.split(ini["tool"]["pytest"]["ini_options"]["addopts"])
    kinds: "list[str]" = []
    for i, token in enumerate(tokens):
        if token == "--cov-report" and i + 1 < len(tokens):
            kinds.append(tokens[i + 1])
        elif token.startswith("--cov-report="):
            kinds.append(token.split("=", 1)[1])
    return kinds


def coverage_targets_pytest_legs(makefile_text: str) -> "dict[str, list[list[str]]]":
    """Target name -> its ``pytest`` argv, in order, for every ``coverage-*`` target with one.

    Read with the lane scanners' own recipe readers (``makefile_live_recipe`` drops
    comment and ``$(SAY)`` lines, ``_makefile_pytest_invocations`` joins continuations
    and splits the argv), so this module decides nothing about the Makefile's shape
    twice. A target whose recipe holds no pytest line at all (``coverage``,
    ``coverage-ts``: pure prerequisites or npm) is not listed.
    """
    legs: "dict[str, list[list[str]]]" = {}
    for target in re.findall(r"^(coverage-[a-z-]+):", makefile_text, re.MULTILINE):
        live = makefile_live_recipe(makefile_text, target)
        argvs = _makefile_pytest_invocations("\n".join(live))
        if argvs:
            legs[target] = argvs
    return legs


def coverage_targets_last_pytest_leg(makefile_text: str) -> "dict[str, list[str]]":
    """Target name -> its last ``pytest`` argv, for every ``coverage-*`` target."""
    return {t: legs[-1] for t, legs in coverage_targets_pytest_legs(makefile_text).items()}


def uninstrumented_coverage_targets(makefile_text: str) -> "set[str]":
    """The ``coverage-*`` targets whose every pytest leg carries ``--no-cov``.

    Pass/fail lanes that measure nothing and so owe no report; the test below
    pins the set by name so it cannot grow unnoticed.
    """
    return {
        t
        for t, legs in coverage_targets_pytest_legs(makefile_text).items()
        if all("--no-cov" in argv for argv in legs)
    }


def test_addopts_prints_the_console_report_and_writes_no_html() -> None:
    kinds = addopts_report_kinds((CHECKOUT_ROOT / "pyproject.toml").read_text())
    assert "term-missing" in kinds, f"addopts lost the console report: {kinds}"
    assert not [k for k in kinds if k.startswith("html")], (
        f"addopts writes an html report ({kinds}) — every pytest invocation in the tree "
        f"would pay for a 291-file tree only `make coverage` reads; ask for it per lane"
    )


def test_the_uninstrumented_coverage_targets_are_exactly_the_bed_half() -> None:
    """`coverage-bed` is the one `coverage-*` lane that measures nothing (#797); a second
    one is a decision to take here, by name, not a silent exemption."""
    text = (CHECKOUT_ROOT / "Makefile").read_text()
    assert uninstrumented_coverage_targets(text) == {"coverage-bed"}


def test_every_coverage_target_writes_html_on_its_last_leg() -> None:
    text = (CHECKOUT_ROOT / "Makefile").read_text()
    legs = coverage_targets_last_pytest_leg(text)
    assert len(legs) >= 5, f"expected the coverage-* python lanes, found {sorted(legs)}"
    for target in uninstrumented_coverage_targets(text):
        legs.pop(target)
    missing = sorted(t for t, argv in legs.items() if "--cov-report=html" not in argv)
    assert not missing, (
        f"these coverage-* targets no longer write reports/coverage/html on their final "
        f"pytest leg: {missing}"
    )


def test_the_scanners_observe_red() -> None:
    assert addopts_report_kinds(
        '[tool.pytest.ini_options]\naddopts = "--cov-report term-missing --cov-report=html:x"\n'
    ) == ["term-missing", "html:x"]
    makefile = (
        "coverage-a: dep ## help\n"
        '\t@echo "one"\n'
        "\t@$(TIMEOUT) uv run pytest -m x \\\n"
        "\t\t--cov-report=html\n"
        "coverage-b:\n"
        "\t@uv run pytest tests/unit\n"
        "\t# @uv run pytest tests/unit --cov-report=html\n"
        "\t@uv run pytest tests/unit -n0 --cov-append\n"
        "other:\n"
        "\t@uv run pytest --cov-report=html\n"
    )
    legs = coverage_targets_last_pytest_leg(makefile)
    assert set(legs) == {"coverage-a", "coverage-b"}
    assert "--cov-report=html" in legs["coverage-a"]  # the continuation was joined
    assert "--cov-report=html" not in legs["coverage-b"]  # the comment did not count
    assert uninstrumented_coverage_targets(makefile) == set()
    bed = "coverage-c:\n\t@uv run pytest -m x --no-cov\n\t@uv run pytest -m y -n0 %s\n"
    assert uninstrumented_coverage_targets(makefile + bed % "") == set(), (
        "one instrumented leg is enough"
    )
    assert uninstrumented_coverage_targets(makefile + bed % "--no-cov") == {"coverage-c"}
