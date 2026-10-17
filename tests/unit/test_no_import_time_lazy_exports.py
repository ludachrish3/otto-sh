"""No otto module binds a process-wide getter or a lazily exported function at import time.

Tests fake the process-wide getters (``get_repos``, ``get_lab``,
``get_completion_names``, ``get_context``, ...) and the registrars and loaders
beside them (``register_options``, ``load_lab``, ...) by patching the module
that DEFINES each one (``otto.declared.get_repos``). That patch reaches every
caller that looks the name up when it runs. It does not reach a module that
bound the name at import, by either spelling:

- ``from otto.config import load_user_settings`` (a package re-export), or
- ``from otto.declared import get_repos`` (the defining module).

Either one binds the real function once, and the module keeps calling it under
a patch every other caller sees. This test parses ``src/otto`` and fails on
both spellings at module level, for two sets of names:

- each entry of the ``_LAZY_EXPORTS`` tables of ``otto``, ``otto.config`` and
  ``otto.logger``, and of the ``_LAZY_ATTRS`` tables of ``otto.lab``,
  ``otto.context`` and ``otto.bootstrap`` (which re-exports discovery's
  ``get_env`` from ``otto.config.repo``), that is a function once the table
  chain is followed to its defining module;
- ``PROCESS_WIDE_GETTERS``: getters no lazy table exports, because their
  defining module is their public home (``otto.bootstrap``'s own), or is
  where otto reads them (discovery's, in ``otto.config.repo``), or
  because their public home re-exports them (``get_completion_names``, defined
  in ``otto.registry``, and the composed state's readers, defined in
  ``otto.declared``).

A module that binds a guarded name only to re-export it — the name is in its
``__all__`` literal and the module never reads it — is not a caller: there is
no call of its own for a patch to miss, and ``tests/unit/test_patch_targets.py``
refuses a patch of the re-export itself. It is a public path to the name,
though: a module-level ``from otto.bootstrap import get_completion_names``
binds the owner's getter as surely as importing it from ``otto.registry``, so
each such re-export is guarded too (:func:`with_reexports`).

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
from typing import NamedTuple

from tests._fixtures.paths import PROJECT_ROOT

SRC_ROOT = PROJECT_ROOT / "src" / "otto"

GUARDED_PACKAGES = ["otto", "otto.config", "otto.logger"]

# Facades whose _LAZY_ATTRS table (name -> defining module) exports process-wide
# getters; the getters themselves are guarded at their defining modules.
GUARDED_ATTRS_PACKAGES = ["otto.lab", "otto.bootstrap", "otto.context"]

# Getters that no lazy table exports, read at their defining module: patched
# there, read at call time, never bound at import (spec 2026-10-06
# repo-and-scope-inputs §9, the lazy-getter guard). otto.bootstrap is its
# getters' public home; otto.config.repo defines discovery, which the root
# re-exports in part (its _LAZY_ATTRS table). otto.invocation has no public path, but the host layer
# reads the run's policy and peer-host resolver there, and a module-level
# binding of either would go stale across runs just the same (spec 2026-10-06
# run-state contracts §3).
PROCESS_WIDE_GETTERS: dict[str, frozenset[str]] = {
    "otto.bootstrap": frozenset({"bootstrap"}),
    "otto.config.repo": frozenset({"discover", "discovered_teardown_deadline", "get_env"}),
    "otto.declared": frozenset(
        {
            "compose",
            "current_ordered_repos",
            "current_repos",
            "get_ordered_repos",
            "get_repos",
            "is_bootstrapped",
            "published_result",
        }
    ),
    "otto.registry": frozenset({"get_completion_names"}),
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
    ``otto.fleet.get_lab``. When that definition is a function, the
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
    sources = {
        _module_of(path): path.read_text(encoding="utf-8")
        for path in sorted(SRC_ROOT.rglob("*.py"))
        if path.name != "__init__.py"
    }
    return with_reexports({module: frozenset(names) for module, names in guarded.items()}, sources)


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


class Binding(NamedTuple):
    """A guarded name a module binds at import."""

    line: int
    module: str
    """The module the name is imported from."""
    name: str
    """The name as that module defines it."""


def _reexported_only(tree: ast.Module) -> set[str]:
    """The names *tree* lists in its ``__all__`` literal and never reads itself."""
    exported: set[str] = set()
    for node in tree.body:
        target = node.target if isinstance(node, ast.AnnAssign) else None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == "__all__" and node.value is not None:
            exported = set(ast.literal_eval(node.value))
    read = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return exported - read


def _module_scope_imports(body: list[ast.stmt]) -> list[ast.ImportFrom]:
    """Each ``from ... import`` in *body* that runs at import.

    Walks module-level compound statements (``if``, ``try`` and its handlers,
    ``with``, loops, ``match``) recursively, but never a ``def`` or ``class``
    body, whose bindings are not the module's, nor an ``if TYPE_CHECKING:``
    body, which binds nothing at runtime.
    """
    found: list[ast.ImportFrom] = []
    for node in body:
        if isinstance(node, ast.ImportFrom):
            found.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        elif isinstance(node, ast.If) and _is_type_checking(node.test):
            found.extend(_module_scope_imports(node.orelse))
        else:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.ExceptHandler, ast.match_case)):
                    found.extend(_module_scope_imports(child.body))
            for field in ("body", "orelse", "finalbody"):
                block = getattr(node, field, None)
                if isinstance(block, list):
                    found.extend(_module_scope_imports(block))
    return found


def with_reexports(
    guarded: dict[str, frozenset[str]], sources: dict[str, str]
) -> dict[str, frozenset[str]]:
    """*guarded* plus each guarded name a module re-exports, under the name it exports.

    *sources* maps a module to its source. A module-scope import of a guarded
    name that the module only re-exports (:func:`_reexported_only`) makes that
    module a public path to it, so importing the name from there at import time
    is a binding too. Followed until nothing new appears, so a re-export of a
    re-export counts. Package ``__init__`` files are left out by the caller:
    they are lazy and import nothing at module scope.
    """
    result = {module: set(names) for module, names in guarded.items()}
    trees = {module: ast.parse(source) for module, source in sources.items()}
    reexported = {module: _reexported_only(tree) for module, tree in trees.items()}
    changed = True
    while changed:
        changed = False
        for module, tree in trees.items():
            for node in _module_scope_imports(tree.body):
                names = result.get(_absolute(module, node), set())
                for alias in node.names:
                    bound = alias.asname or alias.name
                    known = result.get(module, set())
                    if alias.name in names and bound in reexported[module] and bound not in known:
                        result.setdefault(module, set()).add(bound)
                        changed = True
    return {module: frozenset(names) for module, names in result.items()}


class _ImportTimeBindings(ast.NodeVisitor):
    """Collect a :class:`Binding` for each guarded name bound at import."""

    def __init__(
        self, module: str, guarded: dict[str, frozenset[str]], reexported: set[str]
    ) -> None:
        self.module = module
        self.guarded = guarded
        self.reexported = reexported
        self.found: list[Binding] = []

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
        self.found.extend(
            Binding(node.lineno, source, a.name)
            for a in node.names
            if a.name in names and (a.asname or a.name) not in self.reexported
        )


def import_time_bindings(
    source: str, module: str, guarded: dict[str, frozenset[str]]
) -> list[Binding]:
    """A :class:`Binding` for each guarded name *source* binds at import to call it.

    A name the module only re-exports (:func:`_reexported_only`) is skipped.
    """
    tree = ast.parse(source)
    visitor = _ImportTimeBindings(module, guarded, _reexported_only(tree))
    visitor.visit(tree)
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
    } <= guarded["otto.bootstrap"]  # its own one, its three re-exports, and get_env
    assert "discover" not in guarded["otto.bootstrap"]  # read at its owner only
    assert {"discover", "discovered_teardown_deadline", "get_env"} <= guarded["otto.config.repo"]
    assert {
        "compose",
        "current_ordered_repos",
        "current_repos",
        "get_ordered_repos",
        "get_repos",
        "is_bootstrapped",
    } <= guarded["otto.declared"]
    assert "get_completion_names" in guarded["otto.registry"]
    assert "get_completion_names" in guarded["otto.bootstrap"]  # its public re-export
    assert {"get_lab", "do_for_all_hosts"} <= guarded["otto.fleet"]
    assert {"get_lab", "do_for_all_hosts", "fleet_of_interest"} <= guarded["otto.lab"]
    assert {"Lab", "EmptySelectionError"}.isdisjoint(guarded["otto.lab"])  # classes
    assert "load_lab" in guarded["otto.labs.loader"]  # the defining module, followed
    assert "Lab" not in guarded["otto.labs.loader"]
    assert "get_context" in guarded["otto.context"]
    assert {"get_context", "open_context"} <= guarded["otto.run_context"]  # followed
    assert "OttoContext" not in guarded["otto.run_context"]  # a class
    assert "Repo" not in guarded.get("otto.config.repo", frozenset())


def test_the_scan_flags_only_bindings_made_at_import_time():
    guarded = {
        "otto.config": frozenset({"load_user_settings"}),
        "otto.declared": frozenset({"get_repos"}),
    }
    source = (
        "import typing\n"
        "from typing import TYPE_CHECKING\n"
        "from ..config import load_user_settings\n"  # line 3: flagged, relative package
        "from otto.config import load_user_settings as load\n"  # line 4: flagged, aliased
        "from ..declared import get_repos\n"  # line 5: flagged, defining module
        "from ..config import Unrelated\n"
        "if TYPE_CHECKING:\n"
        "    from ..config import load_user_settings\n"
        "else:\n"
        "    from ..config import load_user_settings\n"  # line 10: flagged, runs at import
        "if typing.TYPE_CHECKING:\n"
        "    from ..declared import get_repos\n"
        "def f():\n"
        "    from ..config import load_user_settings\n"
        "async def g():\n"
        "    from ..declared import get_repos\n"
        "class C:\n"
        "    from ..config import load_user_settings\n"  # line 18: a class body runs at import
    )
    found = import_time_bindings(source, "otto.cli.main", guarded)
    assert found == [
        (3, "otto.config", "load_user_settings"),
        (4, "otto.config", "load_user_settings"),
        (5, "otto.declared", "get_repos"),
        (10, "otto.config", "load_user_settings"),
        (18, "otto.config", "load_user_settings"),
    ]


def test_a_name_the_module_only_re_exports_is_not_a_binding():
    """A re-export has no call of its own; a re-export the module also reads does."""
    guarded = {"otto.registry": frozenset({"get_completion_names"})}
    reexport = '__all__ = ["get_completion_names"]\nfrom .registry import get_completion_names\n'
    assert import_time_bindings(reexport, "otto.bootstrap", guarded) == []
    reads_it = reexport + "def f():\n    return get_completion_names()\n"
    assert import_time_bindings(reads_it, "otto.bootstrap", guarded) == [
        (2, "otto.registry", "get_completion_names")
    ]
    unlisted = "__all__ = []\nfrom .registry import get_completion_names\n"
    assert import_time_bindings(unlisted, "otto.bootstrap", guarded) == [
        (2, "otto.registry", "get_completion_names")
    ]
    aliased = '__all__ = ["names"]\nfrom .registry import get_completion_names as names\n'
    assert import_time_bindings(aliased, "otto.bootstrap", guarded) == []


def test_a_module_level_import_of_a_guarded_re_export_is_a_binding():
    """``from otto.bootstrap import get_completion_names`` binds the owner's getter all the same."""
    guarded = {"otto.registry": frozenset({"get_completion_names"})}
    root = '__all__ = ["get_completion_names"]\nfrom .registry import get_completion_names\n'
    aliased = with_reexports(guarded, {"otto.bootstrap": root})
    assert aliased["otto.bootstrap"] == frozenset({"get_completion_names"})
    caller = (
        "from otto.bootstrap import get_completion_names\n"
        "from ..bootstrap import get_completion_names as names\n"
        "def f():\n"
        "    return get_completion_names(), names()\n"
    )
    assert import_time_bindings(caller, "otto.cli.main", aliased) == [
        (1, "otto.bootstrap", "get_completion_names"),
        (2, "otto.bootstrap", "get_completion_names"),
    ]
    # The re-exporting module itself still passes: it has no call of its own.
    assert import_time_bindings(root, "otto.bootstrap", aliased) == []
    # A module that imports the name and reads it is a caller, not a public path.
    reads_it = root + "def f():\n    return get_completion_names()\n"
    assert "otto.bootstrap" not in with_reexports(guarded, {"otto.bootstrap": reads_it})


class TestAReExportUnderAModuleLevelStatement:
    """A re-export made inside a module-level ``if``/``try`` body runs at import all the same."""

    def bindings(self, root: str) -> list[Binding]:
        guarded = {"otto.registry": frozenset({"get_completion_names"})}
        aliased = with_reexports(guarded, {"otto.bootstrap": root})
        caller = "from otto.bootstrap import get_completion_names\n"
        return import_time_bindings(caller, "otto.cli.main", aliased)

    def test_an_if_body(self):
        root = (
            '__all__ = ["get_completion_names"]\n'
            "if True:\n"
            "    from .registry import get_completion_names\n"
        )
        assert self.bindings(root) == [(1, "otto.bootstrap", "get_completion_names")]

    def test_a_try_body(self):
        root = (
            '__all__ = ["get_completion_names"]\n'
            "try:\n"
            "    pass\n"
            "except ImportError:\n"
            "    pass\n"
            "else:\n"
            "    with open(__file__):\n"
            "        from .registry import get_completion_names\n"
        )
        assert self.bindings(root) == [(1, "otto.bootstrap", "get_completion_names")]

    def test_a_function_or_class_body_stays_clean(self):
        for body in ("def f():\n", "class C:\n"):
            root = (
                f'__all__ = ["get_completion_names"]\n{body}'
                "    from .registry import get_completion_names\n"
            )
            assert self.bindings(root) == [], body

    def test_a_type_checking_body_stays_clean(self):
        root = (
            '__all__ = ["get_completion_names"]\n'
            "if TYPE_CHECKING:\n"
            "    from .registry import get_completion_names\n"
        )
        assert self.bindings(root) == []


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
