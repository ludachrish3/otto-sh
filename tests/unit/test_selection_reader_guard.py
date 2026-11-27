"""The root's selection parameters are read in one place in the command-line layer.

``-l``, ``-I`` and ``-E`` reach a command as the root command's parsed
parameters ``labs``, ``include_projects`` and ``exclude_projects``.
:func:`otto.cli.invoke.selection_from_params` is their one reader, and
:func:`otto.cli.invoke.scope_selection` the seam called by every site that
reads a context's parameters for the selection. A second site that reads them
from a context's parameters (a walk with a tuple of keys, a
``params.get("labs")``) is a second copy of the selection, which a fix to the
reader would miss.

Textual clone detection does not find that, so this guard scans every module of
``src/otto/cli`` for a string constant spelling one of the three names, and
holds each to :data:`ALLOWED`. A site may be allowed only when it does not read a
context's parameters, and its entry says what it reads instead. The completion
shim (``otto._shim_complete``) mirrors the reader outside this layer, under the
differential test.
"""

import ast
from pathlib import Path

from tests._fixtures.paths import CHECKOUT_ROOT

NAMES = frozenset({"labs", "include_projects", "exclude_projects"})

ALLOWED: "dict[tuple[str, str], str]" = {
    ("otto.cli.invoke", "selection_from_params"): "the reader",
    ("otto.cli.main", "_lab_completer"): (
        "the completion cache's ``labs`` payload key (its ``completion_source`` and its "
        "``cached['labs']`` read), the lab names a source declares, not a context's parameters"
    ),
    ("otto.cli.main", "DELEGATED_NAMES_KEYS"): (
        "the completion cache's ``labs`` payload key, named among the keys the cache "
        "delegates to completers, not a context's parameters"
    ),
}
"""``(module, scope)`` → why that site may spell one of the three names.

The scope is the innermost enclosing function or class (a decorator belongs to
what it decorates), or, at module level, the name the statement assigns."""


def _scope_name(stmt: ast.stmt) -> str:
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return stmt.name
    if isinstance(stmt, ast.Assign) and stmt.targets and isinstance(stmt.targets[0], ast.Name):
        return stmt.targets[0].id
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        return stmt.target.id
    return "<module>"


def selection_literals(source: str, module: str) -> "list[tuple[str, str, int]]":
    """Return ``(module, scope, line)`` for each string constant in *source* in :data:`NAMES`."""
    tree = ast.parse(source)
    found: "list[tuple[str, str, int]]" = []

    def visit(node: ast.AST, scope: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scope = node.name
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in NAMES:
            found.append((module, scope, node.lineno))
        for child in ast.iter_child_nodes(node):
            visit(child, scope)

    for stmt in tree.body:
        visit(stmt, _scope_name(stmt))
    return found


def _cli_modules() -> "list[tuple[str, Path]]":
    src = CHECKOUT_ROOT / "src"
    return [
        (".".join(path.relative_to(src).with_suffix("").parts), path)
        for path in sorted((src / "otto" / "cli").rglob("*.py"))
    ]


def test_the_three_root_parameter_names_appear_as_literals_only_where_allowed():
    found = [
        hit
        for module, path in _cli_modules()
        for hit in selection_literals(path.read_text(encoding="utf-8"), module)
    ]
    assert any(scope == "selection_from_params" for _, scope, _ in found), (
        "the scan found no reader: it no longer sees what it guards"
    )
    stray = sorted(
        f"{m}:{line} in {scope}" for m, scope, line in found if (m, scope) not in ALLOWED
    )
    assert not stray, (
        "a module of the command-line layer spells a root selection parameter outside the "
        "reader. Read the selection with otto.cli.invoke.scope_selection(ctx); a site that "
        f"reads something else under the same name goes in ALLOWED with its reason: {stray}"
    )
    stale = sorted(f"{m}.{scope}" for m, scope in ALLOWED.keys() - {(m, s) for m, s, _ in found})
    assert not stale, f"ALLOWED names sites that no longer spell the names: {stale}"


def test_the_guard_catches_a_tuple_of_keys():
    planted = (
        "def _walk(ctx):\n"
        "    found = {}\n"
        "    for key in ('host_id', 'labs', 'include_projects'):\n"
        "        if key in ctx.params:\n"
        "            found[key] = ctx.params[key]\n"
        "    return found\n"
        "LABS = 'exclude_projects'\n"
    )
    assert selection_literals(planted, "otto.cli.planted") == [
        ("otto.cli.planted", "_walk", 3),
        ("otto.cli.planted", "_walk", 3),
        ("otto.cli.planted", "LABS", 7),
    ]
