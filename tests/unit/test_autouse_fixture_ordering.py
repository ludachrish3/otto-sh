"""No autouse conftest fixture may request the shared ``monkeypatch`` fixture.

``monkeypatch`` is one shared instance per test, instantiated at the point of
its FIRST requester and torn down in reverse fixture order — so whichever
fixture asks for it first decides when every ``monkeypatch.setattr`` in the
test unwinds. A root autouse fixture is instantiated before every deeper
fixture; requesting ``monkeypatch`` there makes the shared instance the first
fixture of every test and its undo the LAST thing that runs. A test whose
``monkeypatch.setattr`` targets an attribute that a deeper conftest's
``mock.patch`` fixture owns then has its saved "original" — that fixture's
Mock — restored AFTER the patch has already put the real function back, and
the Mock outlives the test: every later test on the xdist worker sees it.

Seen 2026-09-11, the first cut of the root conftest's ``_hermetic_otto_home``
fixture: ``tests/unit/cli``'s ``create_output_dir`` Mock leaked into the logger
and dry-run tests on three coverage-gate runs out of three, and the leaking
test was named only by replaying the worker's sequence single-process with a
per-test probe. Two fixes landed: the root fixture owns a private
``pytest.MonkeyPatch``, and the two cli fixtures shape the conftest's Mock
instead of monkeypatching over it. This pins the trigger — the shape is
legal pytest, and nothing else says no to it.

Scope: every ``conftest.py`` under pytest's test roots, plus ``tests/_fixtures``
(fixtures shared by more than one tree). Function- and yield-fixtures alike;
an autouse fixture that takes ``monkeypatch`` under any parameter position is
an offender.
"""

import ast
from pathlib import Path

import pytest

from tests._fixtures.paths import PROJECT_ROOT, TESTS_ROOT

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - 3.10 only, otto's floor
    import tomli as tomllib

pytestmark = pytest.mark.interpreter_agnostic


def _is_autouse(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for deco in node.decorator_list:
        if not isinstance(deco, ast.Call):
            continue
        for kw in deco.keywords:
            if kw.arg == "autouse" and isinstance(kw.value, ast.Constant) and kw.value.value:
                return True
    return False


def autouse_fixtures_requesting_monkeypatch(source: str, label: str) -> "list[str]":
    """Autouse fixtures in *source* whose parameters include ``monkeypatch``."""
    offenders: "list[str]" = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _is_autouse(node):
            continue
        params = [a.arg for a in node.args.args + node.args.kwonlyargs]
        if "monkeypatch" in params:
            offenders.append(f"{label}::{node.name}")
    return offenders


def _scanned_files() -> "list[Path]":
    ini = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    roots = [PROJECT_ROOT / p for p in ini["tool"]["pytest"]["ini_options"]["testpaths"]]
    files = [TESTS_ROOT / "conftest.py"]
    for root in roots:
        files += sorted(root.rglob("conftest.py"))
    files += sorted((TESTS_ROOT / "_fixtures").glob("*.py"))
    return files


def test_no_autouse_fixture_requests_the_shared_monkeypatch() -> None:
    offenders: "list[str]" = []
    for path in _scanned_files():
        offenders += autouse_fixtures_requesting_monkeypatch(
            path.read_text(), str(path.relative_to(PROJECT_ROOT))
        )
    assert not offenders, (
        f"autouse fixtures requesting the shared `monkeypatch`: {offenders}. That pins the "
        f"shared instance as the first fixture of every test, so a test's monkeypatch over a "
        f"deeper fixture's mock.patch unwinds AFTER the patch and resurrects the Mock. Use "
        f"`with pytest.MonkeyPatch.context() as private:` inside the fixture instead."
    )


def test_the_scanner_observes_red() -> None:
    source = (
        "import pytest\n"
        "@pytest.fixture(autouse=True)\n"
        "def bad(monkeypatch, tmp_path):\n"
        "    yield\n"
        "@pytest.fixture(autouse=True)\n"
        "def bad_kwonly(*, monkeypatch):\n"
        "    yield\n"
        "@pytest.fixture(autouse=False)\n"
        "def fine_not_autouse(monkeypatch):\n"
        "    yield\n"
        "@pytest.fixture(autouse=True)\n"
        "def fine_private(tmp_path):\n"
        "    with pytest.MonkeyPatch.context() as mp:\n"
        "        yield\n"
        "@pytest.fixture\n"
        "def fine_plain(monkeypatch):\n"
        "    yield\n"
    )
    assert autouse_fixtures_requesting_monkeypatch(source, "x.py") == [
        "x.py::bad",
        "x.py::bad_kwonly",
    ]
    assert _scanned_files()[0].name == "conftest.py"
