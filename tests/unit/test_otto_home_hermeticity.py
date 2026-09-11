"""No test, and no harness script, writes into the developer's real ``~/.otto``.

otto keys a per-workspace directory under its user-level home (``$OTTO_HOME``,
else ``~/.otto``) and writes the completion cache there. Anything that runs
otto with the home unpinned therefore leaves a directory behind in the real
home of whoever ran the suite. Found 2026-09-11: 8,974 such workspaces on
the dev VM (201 from one day, 10-30 a day since), the newest traced through
their recorded SUT paths to ``scripts/capture_docs_termynal.py`` — every
docs build stands up a demo repo under ``/tmp`` and ran otto against the
real home — and to in-process runs that never set the variable. Two side
effects: the real home grows without bound, and any test that reads the
DEFAULT home (``otto cache info`` with ``OTTO_HOME`` unset) walks all of it —
9 s per test here, and slower every week.

Three pins, in order of where the leak can start:

- the root conftest's ``_hermetic_otto_home`` fixture points every test at a
  per-worker directory under pytest's basetemp (asserted in-process);
- a test that deliberately UNSETS ``OTTO_HOME`` to exercise the default must
  pin ``HOME`` too, so the default resolves under its tmp_path (AST scan);
- the two out-of-process otto runners the tree owns — the e2e subprocess
  launcher and the docs capture script — set ``OTTO_HOME`` themselves.
"""

import ast
import os
from pathlib import Path

import pytest

from otto.config.home import otto_home
from tests._fixtures.paths import PROJECT_ROOT, TESTS_ROOT

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - 3.10 only, otto's floor
    import tomli as tomllib

pytestmark = pytest.mark.interpreter_agnostic


def _test_roots() -> "list[Path]":
    ini = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    return [PROJECT_ROOT / p for p in ini["tool"]["pytest"]["ini_options"]["testpaths"]]


def _env_calls(node: ast.AST, method: str, var: str) -> bool:
    """Whether *node* contains a ``<x>.<method>("<var>", ...)`` call."""
    for call in ast.walk(node):
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
            continue
        if call.func.attr != method or not call.args:
            continue
        first = call.args[0]
        if isinstance(first, ast.Constant) and first.value == var:
            return True
    return False


def unpinned_default_home_tests(source: str, label: str) -> "list[str]":
    """Functions that unset ``OTTO_HOME`` without pinning ``HOME``: the default leaks.

    Pure over source text so the red arm is testable. Scans every function
    (test or helper) because a helper that unsets the variable is the same
    leak one call away.
    """
    offenders: "list[str]" = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if _env_calls(node, "delenv", "OTTO_HOME") and not _env_calls(node, "setenv", "HOME"):
            offenders.append(f"{label}::{node.name}")
    return offenders


def function_sets_env_key(source: str, function: str, key: str) -> bool:
    """Whether *function* in *source* mentions *key* as a string or a keyword."""
    tree = ast.parse(source)
    fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == function), None)
    assert fn is not None, f"{function} not found"
    for node in ast.walk(fn):
        if isinstance(node, ast.Constant) and node.value == key:
            return True
        if isinstance(node, ast.keyword) and node.arg == key:
            return True
    return False


def test_every_test_runs_under_a_private_otto_home(tmp_path_factory: pytest.TempPathFactory):
    home = os.environ.get("OTTO_HOME")
    assert home, "OTTO_HOME is unset — the root conftest's _hermetic_otto_home fixture is gone"
    basetemp = tmp_path_factory.getbasetemp().resolve()
    assert basetemp in Path(home).resolve().parents, (
        f"OTTO_HOME={home} is not under this worker's basetemp {basetemp}; a test that "
        f"writes a completion cache would land it in a shared or real home"
    )
    assert otto_home() == Path(home), "otto_home() does not honour the pinned OTTO_HOME"


def test_unsetting_otto_home_pins_the_user_home_too() -> None:
    offenders: "list[str]" = []
    for root in _test_roots():
        for path in sorted(root.rglob("test_*.py")):
            offenders += unpinned_default_home_tests(
                path.read_text(), str(path.relative_to(PROJECT_ROOT))
            )
    assert not offenders, (
        f"these unset OTTO_HOME without pinning HOME, so otto's default home is the real "
        f"~/.otto — writes leak there and reads walk everything already in it: {offenders}"
    )


def test_the_scanner_observes_red() -> None:
    leaky = (
        "def test_a(monkeypatch):\n"
        '    monkeypatch.delenv("OTTO_HOME", raising=False)\n'
        "def test_b(monkeypatch, tmp_path):\n"
        '    monkeypatch.delenv("OTTO_HOME", raising=False)\n'
        '    monkeypatch.setenv("HOME", str(tmp_path))\n'
        "def helper(mp):\n"
        '    mp.delenv("OTTO_HOME")\n'
    )
    assert unpinned_default_home_tests(leaky, "x.py") == ["x.py::test_a", "x.py::helper"]
    assert function_sets_env_key("def f(e):\n    e.update(OTTO_HOME='x')\n", "f", "OTTO_HOME")
    assert function_sets_env_key('def f(e):\n    e["OTTO_HOME"] = "x"\n', "f", "OTTO_HOME")
    assert not function_sets_env_key("def f(e):\n    e['HOME'] = 'x'\n", "f", "OTTO_HOME")


def test_the_otto_subprocess_launcher_pins_otto_home() -> None:
    source = (TESTS_ROOT / "e2e" / "_otto_subprocess.py").read_text()
    assert function_sets_env_key(source, "otto_subprocess_env", "OTTO_HOME"), (
        "tests/e2e/_otto_subprocess.py's otto_subprocess_env no longer sets OTTO_HOME: "
        "every e2e otto child would write into the real ~/.otto"
    )


def test_the_docs_capture_script_pins_otto_home() -> None:
    source = (PROJECT_ROOT / "scripts" / "capture_docs_termynal.py").read_text()
    assert function_sets_env_key(source, "_otto_env", "OTTO_HOME"), (
        "scripts/capture_docs_termynal.py's _otto_env strips OTTO_* and sets no OTTO_HOME, "
        "so every docs build leaves a demo-repo workspace in the real ~/.otto"
    )
