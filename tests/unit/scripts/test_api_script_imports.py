"""The public-surface scripts import each other without a cycle, and each imports alone.

#590 is an import-cycle series: its own tooling must not ship one. The graph
counts every ``from scripts import x`` / ``from scripts.x import y`` /
``import scripts.x`` in a module, function-local imports included -- a cycle
that only closes inside ``main`` still binds the two modules to each other.
"""

import ast
import subprocess
import sys

import pytest

from tests._fixtures.paths import PROJECT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

SCRIPTS = PROJECT_ROOT / "scripts"
ROOTS = sorted(
    [p.stem for p in SCRIPTS.glob("api_*.py")] + ["check_breaking_marks"],
)


def _script_imports(module: str) -> "set[str]":
    """Return the ``scripts`` modules *module* imports, anywhere in its source."""
    tree = ast.parse((SCRIPTS / f"{module}.py").read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == "scripts":
                out.update(alias.name for alias in node.names)
            elif node.module.startswith("scripts."):
                out.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):
            out.update(
                alias.name.split(".")[1]
                for alias in node.names
                if alias.name.startswith("scripts.")
            )
    return {name for name in out if (SCRIPTS / f"{name}.py").is_file()}


def _graph() -> "dict[str, set[str]]":
    graph: dict[str, set[str]] = {}
    todo = list(ROOTS)
    while todo:
        module = todo.pop()
        if module in graph:
            continue
        graph[module] = _script_imports(module)
        todo.extend(graph[module])
    return graph


def _cycle(graph: "dict[str, set[str]]") -> "list[str]":
    """Return one import cycle in *graph* as a path that starts and ends on one module."""
    done: set[str] = set()

    def walk(module: str, path: "list[str]") -> "list[str]":
        if module in path:
            return [*path[path.index(module) :], module]
        if module in done:
            return []
        for target in sorted(graph[module]):
            found = walk(target, [*path, module])
            if found:
                return found
        done.add(module)
        return []

    for module in sorted(graph):
        found = walk(module, [])
        if found:
            return found
    return []


def test_the_api_scripts_import_graph_has_no_cycle():
    graph = _graph()
    assert {"api_teaching", "api_agreement", "check_breaking_marks"} <= set(graph)
    assert _cycle(graph) == []


def test_the_cycle_finder_finds_a_planted_cycle():
    assert _cycle({"a": {"b"}, "b": {"c"}, "c": {"a"}}) == ["a", "b", "c", "a"]
    assert _cycle({"a": {"b"}, "b": set()}) == []


@pytest.mark.parametrize("module", ROOTS)
def test_each_api_script_imports_alone_in_a_fresh_interpreter(module):
    proc = subprocess.run(
        [sys.executable, "-c", f"import scripts.{module}"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
