"""No otto module binds a lazily exported function at import time.

Three packages declare a ``_LAZY_EXPORTS`` table: ``otto``, ``otto.config`` and
``otto.logger``. Their function entries are the process-wide getters
(``get_repos``, ``get_lab``, ``get_completion_names``, ``get_context``, ...)
and the registrars and loaders beside them (``register_options``, ``load_lab``,
...), and tests fake them by patching the module that DEFINES each one
(``otto.config.bootstrapped.get_repos``). That patch reaches every caller that
looks the name up when it runs. It does not reach a module that bound the name
at import, by either spelling:

- ``from otto.config import get_repos`` (the package re-export), or
- ``from otto.config.bootstrapped import get_repos`` (the defining module).

Either one binds the real function once, and the module keeps calling it under
a patch every other caller sees. This test parses ``src/otto`` and fails on
both spellings at module level, for each entry of the three tables that is a
function once the table chain is followed to its defining module. The tables
are re-derived from source so this test imports no otto module;
``tests/_fixtures/_lazy_exports.py`` does the same derivation at run time, by
importing the packages. Classes (``Repo``, ``OttoContext``) are not checked:
binding a class at import defeats no patch a test makes. Imports under
``if TYPE_CHECKING:`` bind nothing at runtime and are skipped, as are function
bodies and each package's own ``__init__``. A table entry that names a
submodule rather than an attribute (``otto.logger``'s ``management``) is not a
binding of a function and is not checked.

The packages with a ``_LAZY_ATTRS`` table are deliberately out of scope. A
module-level ``from ..link import find_link`` there is patched on the
CONSUMER (``otto.cli.link.find_link``), which is correct and leaks nothing;
the only forbidden patch target is the lazy package's own namespace, which
``tests/unit/test_patch_targets.py`` and the root conftest's teardown guard
enforce.

See also: the contributor rules in ``docs/contributing.md``, "Patching lazily
exported names".
"""

import ast
import functools
from pathlib import Path

from tests._fixtures.paths import PROJECT_ROOT

SRC_ROOT = PROJECT_ROOT / "src" / "otto"

GUARDED_PACKAGES = ["otto", "otto.config", "otto.logger"]

# A site that genuinely cannot read the name at call time, keyed on its path
# under src/otto, with the reason. Only a decorator, a default argument or a
# module-level constant qualifies.
ALLOWED = {
    ("project/options.py", "options"): (
        "applied as the @options class decorator, which runs at import; imported "
        "from otto.params, where it is defined"
    ),
    ("examples/options.py", "options"): (
        "a user-facing sample showing the public `from otto import options` spelling, "
        "applied as the @options class decorator"
    ),
}


def _source_of(module: str) -> Path:
    """The source file of the otto module *module*: ``a/b.py`` or ``a/b/__init__.py``."""
    base = SRC_ROOT.joinpath(*module.split(".")[1:])
    plain = base.with_suffix(".py")
    return plain if plain.is_file() else base / "__init__.py"


def _module_of(path: Path) -> str:
    return ".".join(["otto", *path.relative_to(SRC_ROOT).with_suffix("").parts])


def _lazy_exports_table(package: str) -> dict:
    """*package*'s ``_LAZY_EXPORTS`` literal, read from its source without importing it."""
    for node in ast.parse(_source_of(package).read_text(encoding="utf-8")).body:
        target = node.target if isinstance(node, ast.AnnAssign) else None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == "_LAZY_EXPORTS":
            return ast.literal_eval(node.value)
    raise AssertionError(f"{package} declares no _LAZY_EXPORTS literal")


def _is_function(module: str, name: str) -> bool:
    """Whether *module* defines *name* with a top-level ``def`` / ``async def``."""
    tree = ast.parse(_source_of(module).read_text(encoding="utf-8"))
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        for node in tree.body
    )


@functools.cache
def guarded_names() -> dict[str, frozenset[str]]:
    """Module -> the functions no other otto module may bind from it at import time.

    Each entry of a guarded package's table is followed through the tables to
    the module that finally defines it: ``otto.get_lab`` names
    ``otto.config.get_lab``, which names ``otto.config.fleet.get_lab``. When
    that definition is a function, the package maps to the entry's name and
    the defining module to the function's name; a class maps to nothing.
    """
    tables = {
        package: {
            name: tuple(target)
            for name, target in _lazy_exports_table(package).items()
            if not isinstance(target, str)  # a str value names a submodule
        }
        for package in GUARDED_PACKAGES
    }
    guarded: dict[str, set[str]] = {package: set() for package in tables}
    for package, table in tables.items():
        for name, target in table.items():
            module, attr = target
            while attr in tables.get(module, {}):
                module, attr = tables[module][attr]
            if _is_function(module, attr):
                guarded[package].add(name)
                guarded.setdefault(module, set()).add(attr)
    return {module: frozenset(names) for module, names in guarded.items()}


def _absolute(module: str, node: ast.ImportFrom) -> str:
    """The absolute module *node* imports from, as written in *module*."""
    if node.level == 0:
        return node.module or ""
    # A package's __init__ is named "<pkg>.__init__" here, so one level up is
    # the package itself, just as it is for a sibling module's ".".
    base = ".".join(module.split(".")[: -node.level])
    return f"{base}.{node.module}" if node.module else base


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


class _ImportTimeBindings(ast.NodeVisitor):
    """Collect ``(line, source module, name)`` for each guarded name bound at import."""

    def __init__(self, module: str, guarded: dict[str, frozenset[str]]) -> None:
        self.module = module
        self.guarded = guarded
        self.found: list[tuple[int, str, str]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """A function body runs at call time: nothing in it is checked."""

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """A coroutine body runs at call time: nothing in it is checked."""

    def visit_If(self, node: ast.If) -> None:
        if _is_type_checking(node.test):
            for stmt in node.orelse:
                self.visit(stmt)
            return
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        source = _absolute(self.module, node)
        # A package's own __init__ is the re-export machinery, not a caller.
        if source == self.module.removesuffix(".__init__"):
            return
        names = self.guarded.get(source, frozenset())
        self.found.extend((node.lineno, source, a.name) for a in node.names if a.name in names)


def import_time_bindings(
    source: str, module: str, guarded: dict[str, frozenset[str]]
) -> list[tuple[int, str, str]]:
    """``(line, source module, name)`` for each guarded name *source* binds at import."""
    visitor = _ImportTimeBindings(module, guarded)
    visitor.visit(ast.parse(source))
    return visitor.found


def test_the_scan_knows_the_names_it_guards():
    """A table read that came back short would let every check below pass vacuously."""
    guarded = guarded_names()
    assert set(GUARDED_PACKAGES) <= guarded.keys()
    assert {"get_repos", "get_lab", "get_completion_names"} <= guarded["otto.config"]
    assert {"get_context", "options", "get_lab", "register_options"} <= guarded["otto"]
    assert "Repo" not in guarded["otto.config"]  # a class, not a function
    assert "OttoContext" not in guarded["otto"]
    assert "management" not in guarded["otto.logger"]  # names a submodule
    assert {"get_repos", "get_completion_names"} <= guarded["otto.config.bootstrapped"]
    assert {"get_lab", "do_for_all_hosts"} <= guarded["otto.config.fleet"]
    assert "get_context" in guarded["otto.context"]
    assert "Repo" not in guarded.get("otto.config.repo", frozenset())


def test_the_scan_flags_only_bindings_made_at_import_time():
    guarded = {
        "otto.config": frozenset({"get_repos"}),
        "otto.config.bootstrapped": frozenset({"get_repos"}),
    }
    source = (
        "import typing\n"
        "from typing import TYPE_CHECKING\n"
        "from ..config import get_repos\n"  # line 3: flagged, relative package
        "from otto.config import get_repos as repos\n"  # line 4: flagged, aliased
        "from ..config.bootstrapped import get_repos\n"  # line 5: flagged, defining module
        "from ..config import Unrelated\n"
        "if TYPE_CHECKING:\n"
        "    from ..config import get_repos\n"
        "else:\n"
        "    from ..config import get_repos\n"  # line 10: flagged, runs at import
        "if typing.TYPE_CHECKING:\n"
        "    from ..config.bootstrapped import get_repos\n"
        "def f():\n"
        "    from ..config import get_repos\n"
        "async def g():\n"
        "    from ..config.bootstrapped import get_repos\n"
        "class C:\n"
        "    from ..config import get_repos\n"  # line 18: a class body runs at import
    )
    found = import_time_bindings(source, "otto.cli.main", guarded)
    assert found == [
        (3, "otto.config", "get_repos"),
        (4, "otto.config", "get_repos"),
        (5, "otto.config.bootstrapped", "get_repos"),
        (10, "otto.config", "get_repos"),
        (18, "otto.config", "get_repos"),
    ]


def test_no_otto_module_binds_a_lazily_exported_function_at_import_time():
    guarded = guarded_names()
    offenders = []
    allowed_seen = set()
    for path in sorted(SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(SRC_ROOT).as_posix()
        source = path.read_text(encoding="utf-8")
        for line, _module, name in import_time_bindings(source, _module_of(path), guarded):
            if (rel, name) in ALLOWED:
                allowed_seen.add((rel, name))
                continue
            offenders.append(
                f"src/otto/{rel}:{line} {name} -> import it inside the function that uses it"
            )
    assert not offenders, (
        "a module-level import binds a lazily exported function, so a patch where it is "
        "defined never reaches this caller:\n  " + "\n  ".join(offenders)
    )
    stale = sorted(f"src/otto/{rel} {name}" for rel, name in ALLOWED.keys() - allowed_seen)
    assert not stale, "ALLOWED entries that no longer bind anything; drop them:\n  " + (
        "\n  ".join(stale)
    )
