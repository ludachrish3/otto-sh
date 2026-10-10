"""No otto module binds a process-wide getter or a lazily exported function at import time.

Tests fake the process-wide getters (``get_repos``, ``get_lab``,
``get_completion_names``, ``get_context``, ...) and the registrars and loaders
beside them (``register_options``, ``load_lab``, ...) by patching the module
that DEFINES each one (``otto.bootstrap.get_repos``). That patch reaches every
caller that looks the name up when it runs. It does not reach a module that
bound the name at import, by either spelling:

- ``from otto.config import load_user_settings`` (a package re-export), or
- ``from otto.bootstrap import get_repos`` (the defining module).

Either one binds the real function once, and the module keeps calling it under
a patch every other caller sees. This test parses ``src/otto`` and fails on
both spellings at module level, for two sets of names:

- each entry of the ``_LAZY_EXPORTS`` tables of ``otto``, ``otto.config`` and
  ``otto.logger``, and of the ``_LAZY_ATTRS`` table of ``otto.lab``, that is a
  function once the table chain is followed to its defining module;
- ``PROCESS_WIDE_GETTERS``: getters no lazy table exports, because their
  defining module is their public home (``otto.bootstrap``'s accessors).

The tables are re-derived from source so this test imports no otto module;
``tests/_fixtures/_lazy_exports.py`` does the same derivation at run time, by
importing the packages. Classes (``Repo``, ``OttoContext``) are not checked:
binding a class at import defeats no patch a test makes. Imports under
``if TYPE_CHECKING:`` bind nothing at runtime and are skipped, as are function
bodies and each package's own ``__init__``. A table entry that names a
submodule rather than an attribute (``otto.logger``'s ``management``) is not a
binding of a function and is not checked.

The other packages with a ``_LAZY_ATTRS`` table are deliberately out of scope. A
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

# Facades whose _LAZY_ATTRS table (name -> defining module) exports process-wide
# getters; the getters themselves are guarded at their defining modules.
GUARDED_ATTRS_PACKAGES = ["otto.lab"]

# Getters that no lazy table exports, read at their defining module: patched
# there, read at call time, never bound at import (spec 2026-10-06
# repo-and-scope-inputs §9, the lazy-getter guard). otto.bootstrap is its
# getters' public home. otto.invocation has no public path, but the host layer
# reads the run's policy and peer-host resolver there, and a module-level
# binding of either would go stale across runs just the same (spec 2026-10-06
# run-state contracts §3).
PROCESS_WIDE_GETTERS: dict[str, frozenset[str]] = {
    "otto.bootstrap": frozenset(
        {
            "bootstrap",
            "get_repos",
            "get_ordered_repos",
            "get_env",
            "is_bootstrapped",
            "get_completion_names",
        }
    ),
    "otto.invocation": frozenset({"current_policy", "installed_policy", "installed_resolver"}),
}

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


def _table_literal(package: str, table: str) -> dict:
    """*package*'s *table* literal, read from its source without importing it."""
    for node in ast.parse(_source_of(package).read_text(encoding="utf-8")).body:
        target = node.target if isinstance(node, ast.AnnAssign) else None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == table:
            return ast.literal_eval(node.value)
    raise AssertionError(f"{package} declares no {table} literal")


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
    the module that finally defines it: ``otto.lab.get_lab`` names
    ``otto.config.fleet.get_lab``. When that definition is a function, the
    package maps to the entry's name and the defining module to the
    function's name; a class maps to nothing.
    """
    tables = {
        package: {
            name: tuple(target)
            for name, target in _table_literal(package, "_LAZY_EXPORTS").items()
            if not isinstance(target, str)  # a str value names a submodule
        }
        for package in GUARDED_PACKAGES
    }
    tables.update(
        {
            package: {
                name: (module, name)
                for name, module in _table_literal(package, "_LAZY_ATTRS").items()
            }
            for package in GUARDED_ATTRS_PACKAGES
        }
    )
    guarded: dict[str, set[str]] = {package: set() for package in tables}
    for package, table in tables.items():
        for name, target in table.items():
            module, attr = target
            while attr in tables.get(module, {}):
                module, attr = tables[module][attr]
            if _is_function(module, attr):
                guarded[package].add(name)
                guarded.setdefault(module, set()).add(attr)
    for module, names in PROCESS_WIDE_GETTERS.items():
        not_functions = sorted(name for name in names if not _is_function(module, name))
        assert not not_functions, f"PROCESS_WIDE_GETTERS: {module} defines no {not_functions}"
        guarded.setdefault(module, set()).update(names)
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
    assert set(GUARDED_PACKAGES + GUARDED_ATTRS_PACKAGES) <= guarded.keys()
    assert {"load_user_settings", "user_settings_path"} <= guarded["otto.config"]
    assert {"get_context", "options", "get_lab", "register_options"} <= guarded["otto"]
    assert "Repo" not in guarded["otto.config"]  # a class, not a function
    assert "OttoContext" not in guarded["otto"]
    assert "management" not in guarded["otto.logger"]  # names a submodule
    assert {
        "bootstrap",
        "get_repos",
        "get_ordered_repos",
        "get_env",
        "is_bootstrapped",
        "get_completion_names",
    } <= guarded["otto.bootstrap"]
    assert {"get_lab", "do_for_all_hosts"} <= guarded["otto.config.fleet"]
    assert {"get_lab", "do_for_all_hosts", "fleet_of_interest"} <= guarded["otto.lab"]
    assert {"Lab", "EmptySelectionError"}.isdisjoint(guarded["otto.lab"])  # classes
    assert "get_context" in guarded["otto.context"]
    assert "Repo" not in guarded.get("otto.config.repo", frozenset())


def test_the_scan_flags_only_bindings_made_at_import_time():
    guarded = {
        "otto.config": frozenset({"load_user_settings"}),
        "otto.bootstrap": frozenset({"get_repos"}),
    }
    source = (
        "import typing\n"
        "from typing import TYPE_CHECKING\n"
        "from ..config import load_user_settings\n"  # line 3: flagged, relative package
        "from otto.config import load_user_settings as load\n"  # line 4: flagged, aliased
        "from ..bootstrap import get_repos\n"  # line 5: flagged, defining module
        "from ..config import Unrelated\n"
        "if TYPE_CHECKING:\n"
        "    from ..config import load_user_settings\n"
        "else:\n"
        "    from ..config import load_user_settings\n"  # line 10: flagged, runs at import
        "if typing.TYPE_CHECKING:\n"
        "    from ..bootstrap import get_repos\n"
        "def f():\n"
        "    from ..config import load_user_settings\n"
        "async def g():\n"
        "    from ..bootstrap import get_repos\n"
        "class C:\n"
        "    from ..config import load_user_settings\n"  # line 18: a class body runs at import
    )
    found = import_time_bindings(source, "otto.cli.main", guarded)
    assert found == [
        (3, "otto.config", "load_user_settings"),
        (4, "otto.config", "load_user_settings"),
        (5, "otto.bootstrap", "get_repos"),
        (10, "otto.config", "load_user_settings"),
        (18, "otto.config", "load_user_settings"),
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
        "a module-level import binds a process-wide getter or a lazily exported function, "
        "so a patch where it is defined never reaches this caller:\n  " + "\n  ".join(offenders)
    )
    stale = sorted(f"src/otto/{rel} {name}" for rel, name in ALLOWED.keys() - allowed_seen)
    assert not stale, "ALLOWED entries that no longer bind anything; drop them:\n  " + (
        "\n  ".join(stale)
    )
