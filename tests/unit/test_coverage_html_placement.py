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
run — the same reason ``--cov-fail-under`` rides that leg).
"""

import re
import shlex

import pytest

from tests._fixtures.paths import PROJECT_ROOT

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


def coverage_targets_last_pytest_leg(makefile_text: str) -> "dict[str, str]":
    """Target name -> its last ``pytest`` recipe line, for every ``coverage-*`` target.

    Comment lines are dropped and backslash continuations joined, as the
    lane scanners do; a target whose recipe holds no pytest line at all
    (``coverage``, ``coverage-ts``: pure prerequisites or npm) is not listed.
    """
    lines: "list[str]" = []
    pending = ""
    for raw in makefile_text.splitlines():
        if raw.strip().startswith("#"):
            continue
        if raw.rstrip().endswith("\\"):
            pending += raw.rstrip()[:-1] + " "
            continue
        lines.append(pending + raw)
        pending = ""
    last_leg: "dict[str, str]" = {}
    current: "str | None" = None
    for line in lines:
        target = re.match(r"^(coverage-[a-z-]+):", line)
        if target:
            current = target.group(1)
            continue
        if not line.startswith("\t"):
            current = None
            continue
        if current and re.search(r"\bpytest\b", line):
            last_leg[current] = line
    return last_leg


def test_addopts_prints_the_console_report_and_writes_no_html() -> None:
    kinds = addopts_report_kinds((PROJECT_ROOT / "pyproject.toml").read_text())
    assert "term-missing" in kinds, f"addopts lost the console report: {kinds}"
    assert not [k for k in kinds if k.startswith("html")], (
        f"addopts writes an html report ({kinds}) — every pytest invocation in the tree "
        f"would pay for a 291-file tree only `make coverage` reads; ask for it per lane"
    )


def test_every_coverage_target_writes_html_on_its_last_leg() -> None:
    legs = coverage_targets_last_pytest_leg((PROJECT_ROOT / "Makefile").read_text())
    assert len(legs) >= 5, f"expected the coverage-* python lanes, found {sorted(legs)}"
    missing = sorted(t for t, line in legs.items() if "--cov-report=html" not in line)
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
