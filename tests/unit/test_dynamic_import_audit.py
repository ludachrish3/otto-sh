"""Audit every first-party string the import system resolves: its edge, its class, its layer.

tach sees ``import`` statements only, and ``include_string_imports`` stays off
(spec 2026-10-11 placement-and-phases, §5.3). otto also imports by string, and
each such string is a real dependency tach cannot see:

* a package facade's lazy table (``_LAZY_ATTRS`` / ``_LAZY_EXPORTS``), imported
  by the package's ``__getattr__`` on first access;
* a literal ``Ref("pkg.module:attr")``, imported at a registry entry's first lookup;
* a ``Ref`` built inside a loop over a literal table of targets;
* ``importlib.import_module("literal")``;
* a literal ``"pkg.module:attr"`` loader given to ``register_cli_command``,
  imported when the command is dispatched.

A string whose target lies outside its own tach module is a *string edge*.
Every file is in a tach module (``tests/unit/test_tach_layers.py``), so every
edge is judged by the layer and cycle rules. This file parses ``src/otto`` and
imports no otto module. It fails when:

1. a string edge is missing from ``STRING_EDGES``, a listed one no longer
   exists, or one is listed under the wrong class;
2. a string edge points up the layers ``tach.toml`` declares today and is not
   grandfathered;
3. with the grandfathered entries left out, static and string edges together
   form a cycle that is not one of the static graph's own cycles, which is
   the check ``forbid_circular_dependencies`` cannot make (tach sees no
   string edge);
4. a string edge that is not grandfathered runs between the same two tach
   modules as one that is: nothing is added on a grandfathered edge;
5. a grandfathered entry is still here once the landing that removes it is the
   current one, or is listed and no longer exists.

A string computed at run time cannot be read, so every site that resolves one
is pinned in ``COMPUTED_SITES`` with its class, and a new site fails until
someone reads it and lists it.

``STRING_EDGES`` and ``COMPUTED_SITES`` change in the same commit as the code
that changes the strings. ``GRANDFATHERED`` only shrinks: a row added past
``GRANDFATHERED_WHEN_LANDED``, or an expiry pushed later, fails, because a new
exception needs the owner's approval in a spec amendment.
"""

import ast
import collections
import dataclasses
import functools
import sys
from pathlib import Path

import pytest

from scripts import render_module_graph as rmg
from scripts.render_module_graph import declared_owner, module_of
from tests._fixtures.paths import CHECKOUT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

SRC_ROOT = CHECKOUT_ROOT / "src"

LAZY_TABLES = ["_LAZY_ATTRS", "_LAZY_EXPORTS"]

# Classes of a string edge (spec 2026-10-11 placement-and-phases §5.3).
FACADE_BINDING = "facade binding"
ROOT_BY_NAME = "composition-root by-name import"
REGISTRY_BUILTIN = "first-party registry built-in"

# Classes of a computed site, each confirmed by reading the site.
FACADE_RESOLVER = "facade resolver"
"""A facade's ``__getattr__`` (a package's or a plain module's) importing an entry of its
own lazy table."""
USER_SUPPLIED = "user-supplied"
"""A target read from a project's settings or an init module's registration."""
CLI_COMMAND_TABLE = "CLI command table"
"""A command's ``"pkg.mod:attr"`` loader, from ``CLI_COMMANDS``."""
REF_OVER_LITERAL_TABLE = "registry Ref over a literal table"
"""A ``Ref`` built in a loop over a literal table; the audit reads each target as a literal."""
REF_ENGINE = "Ref engine"
"""``Ref.resolve`` itself; every target it imports was audited where its ``Ref`` was built."""
LOADED_MODULE_LOOKUP = "loaded-module lookup"
"""``import_module`` of a class's own ``__module__``: a cached lookup that imports nothing."""

# Every site that resolves a computed target: (file under src/, enclosing scope, class).
# Keyed by scope, not line, so an edit elsewhere in the file does not move it.
COMPUTED_SITES: list[tuple[str, str, str]] = [
    ("otto/__init__.py", "__getattr__", FACADE_RESOLVER),
    ("otto/_webassets/__init__.py", "__getattr__", FACADE_RESOLVER),
    ("otto/bootstrap.py", "__getattr__", FACADE_RESOLVER),
    ("otto/bootstrap.py", "bootstrap", USER_SUPPLIED),
    ("otto/check/__init__.py", "__getattr__", FACADE_RESOLVER),
    ("otto/cli/__init__.py", "__getattr__", FACADE_RESOLVER),
    ("otto/cli/main.py", "_OttoGroup._real", CLI_COMMAND_TABLE),
    ("otto/cli/registry.py", "collect_cli_commands", CLI_COMMAND_TABLE),
    ("otto/cli/registry.py", "resolve_spec_command", CLI_COMMAND_TABLE),
    ("otto/config/__init__.py", "__getattr__", FACADE_RESOLVER),
    ("otto/config/project.py", "Project.import_init_modules", USER_SUPPLIED),
    ("otto/context.py", "__getattr__", FACADE_RESOLVER),
    ("otto/coverage/__init__.py", "__getattr__", FACADE_RESOLVER),
    ("otto/coverage/fetcher/__init__.py", "__getattr__", FACADE_RESOLVER),
    ("otto/coverage/merge/__init__.py", "__getattr__", FACADE_RESOLVER),
    ("otto/coverage/store/__init__.py", "__getattr__", FACADE_RESOLVER),
    ("otto/creds/__init__.py", "__getattr__", FACADE_RESOLVER),
    ("otto/docker/__init__.py", "__getattr__", FACADE_RESOLVER),
    ("otto/env/__init__.py", "__getattr__", FACADE_RESOLVER),
    ("otto/host/__init__.py", "__getattr__", FACADE_RESOLVER),
    ("otto/host/command_frame.py", "_register_builtin_frames", REF_OVER_LITERAL_TABLE),
    ("otto/host/dev_tool.py", "_register_builtin_kinds", REF_OVER_LITERAL_TABLE),
    ("otto/host/embedded_filesystem.py", "_register_builtin_filesystems", REF_OVER_LITERAL_TABLE),
    ("otto/host/product.py", "_register_builtin_kinds", REF_OVER_LITERAL_TABLE),
    ("otto/host/shell_kind.py", "_own_hints", LOADED_MODULE_LOOKUP),
    ("otto/host/shell_kind.py", "resolve_class", USER_SUPPLIED),
    ("otto/host/survey/__init__.py", "__getattr__", FACADE_RESOLVER),
    ("otto/host/transfer/__init__.py", "__getattr__", FACADE_RESOLVER),
    ("otto/host/transfer/registry.py", "_register_builtin_backends", REF_OVER_LITERAL_TABLE),
    ("otto/init/__init__.py", "__getattr__", FACADE_RESOLVER),
    ("otto/inventory/__init__.py", "__getattr__", FACADE_RESOLVER),
    ("otto/kmodcov/__init__.py", "__getattr__", FACADE_RESOLVER),
    ("otto/lab/__init__.py", "__getattr__", FACADE_RESOLVER),
    ("otto/labs/__init__.py", "__getattr__", FACADE_RESOLVER),
    ("otto/link/__init__.py", "__getattr__", FACADE_RESOLVER),
    ("otto/logger/__init__.py", "__getattr__", FACADE_RESOLVER),
    ("otto/logger/__init__.py", "__getattr__", FACADE_RESOLVER),
    ("otto/models/__init__.py", "__getattr__", FACADE_RESOLVER),
    ("otto/monitor/__init__.py", "__getattr__", FACADE_RESOLVER),
    ("otto/params.py", "options_key", USER_SUPPLIED),
    ("otto/params.py", "register_options", USER_SUPPLIED),
    ("otto/project/__init__.py", "__getattr__", FACADE_RESOLVER),
    ("otto/registry.py", "Ref.resolve", REF_ENGINE),
    ("otto/reservations/__init__.py", "__getattr__", FACADE_RESOLVER),
    ("otto/session/__init__.py", "__getattr__", FACADE_RESOLVER),
    ("otto/suite/__init__.py", "__getattr__", FACADE_RESOLVER),
    ("otto/testing/__init__.py", "__getattr__", FACADE_RESOLVER),
    ("otto/tunnel/__init__.py", "__getattr__", FACADE_RESOLVER),
]


@dataclasses.dataclass(frozen=True)
class StringSite:
    """A literal import target in ``src/otto``."""

    path: str
    """The file, under ``src/`` (``otto/host/__init__.py``)."""
    line: int
    target: str
    """``pkg.module:attr``, or ``pkg.module`` for a module."""
    kind: str
    """``lazy-table``, ``ref``, ``ref-table``, ``import_module`` or ``cli-loader``."""

    @property
    def key(self) -> "tuple[str, str]":
        """The site as ``STRING_EDGES`` and ``GRANDFATHERED`` spell it."""
        return (self.path, self.target)

    @property
    def target_module(self) -> str:
        """The module the target names."""
        return self.target.partition(":")[0]


@dataclasses.dataclass(frozen=True)
class ComputedSite:
    """A call that imports a target computed at run time."""

    path: str
    line: int
    scope: str
    """The enclosing ``Class.function`` qualname, or ``<module>``."""
    expanded: bool
    """Whether every target came from a literal table the audit could read."""


@dataclasses.dataclass
class Extraction:
    """What the parse of ``src/otto`` found."""

    literals: "list[StringSite]"
    computed: "list[ComputedSite]"
    lazy_table_files: "list[str]"
    refused: "list[str]" = dataclasses.field(default_factory=list)
    """``Ref`` and ``import_module`` calls in a form the audit cannot read."""


def owner_of(module: str, declared: "list[str]") -> str:
    """The tach module that owns *module*: its longest declared prefix.

    A module no tach module owns is owned by its top-level package
    (``otto.env.manage`` by ``otto.env``), the module ``tach.toml`` will
    declare for it.
    """
    return declared_owner(module, declared) or ".".join(module.split(".")[:2])


def _name_of_call(node: ast.Call) -> "str | None":
    """The called name: ``f`` of ``f(...)`` and ``attr`` of ``x.attr(...)``."""
    func = node.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)


def _call_kind(node: ast.Call) -> "str | None":
    kinds = {"Ref": "ref", "import_module": "import_module", "register_cli_command": "cli-loader"}
    return kinds.get(_name_of_call(node) or "")


# The parameter that names the target: Ref's dataclass field, import_module's first
# argument, register_cli_command's loader (its second, after the command's name).
_TARGET_KEYWORD = {"ref": "target", "import_module": "name", "cli-loader": "loader"}


def _target_argument(node: ast.Call, kind: str) -> "ast.expr | None":
    """The call's target, given positionally or by keyword; ``None`` for any other form.

    A starred or ``**`` argument, a second argument (``import_module``'s
    ``package``, a relative import), or both spellings at once cannot be read,
    so the audit refuses the call rather than guess. ``register_cli_command``
    takes the command's name and options beside its loader, so only a starred
    or ``**`` argument hides its loader.
    """
    keyword = _TARGET_KEYWORD[kind]
    if any(isinstance(arg, ast.Starred) for arg in node.args):
        return None
    if kind == "cli-loader":
        if any(item.arg is None for item in node.keywords):
            return None
        named = [item.value for item in node.keywords if item.arg == keyword]
        given = [*node.args[1:2], *named]
    elif any(item.arg != keyword for item in node.keywords):
        return None
    else:
        given = [*node.args, *(item.value for item in node.keywords)]
    return given[0] if len(given) == 1 else None


def _builds_a_string(node: ast.AST) -> bool:
    """Whether *node* builds a string: an f-string, ``+`` or ``%``, or ``.format``."""
    return isinstance(node, (ast.JoinedStr, ast.BinOp)) or (
        isinstance(node, ast.Call) and _name_of_call(node) == "format"
    )


def _literal(node: ast.AST) -> "str | None":
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _evaluate(expr: ast.AST, env: "dict[str, str]") -> "str | None":
    """Evaluate a name, a literal or an f-string of names against one table row."""
    if (value := _literal(expr)) is not None:
        return value
    if isinstance(expr, ast.Name):
        return env.get(expr.id)
    if isinstance(expr, ast.JoinedStr):
        pieces = []
        for part in expr.values:
            if isinstance(part, ast.FormattedValue):
                if part.conversion != -1 or part.format_spec is not None:
                    return None
                if not isinstance(part.value, ast.Name) or part.value.id not in env:
                    return None
                pieces.append(env[part.value.id])
            elif (text := _literal(part)) is not None:
                pieces.append(text)
        return "".join(pieces)
    return None


def _rebinds(loop: ast.For, names: "list[str | None]") -> bool:
    """Whether the loop's body assigns to one of its own loop variables."""
    return any(
        isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id in names
        for statement in loop.body
        for node in ast.walk(statement)
    )


def _table_rows(loop: ast.For) -> "list[dict[str, str]] | None":
    """Each row of ``for a, b in [(...), ...]`` as {name: literal}, or ``None`` if no table.

    Conservative: a body that rebinds a loop variable makes the rows untrustworthy,
    so the loop is no table.
    """
    if not isinstance(loop.iter, (ast.List, ast.Tuple)):
        return None
    if isinstance(loop.target, ast.Name):
        names: list[str | None] = [loop.target.id]
    elif isinstance(loop.target, ast.Tuple):
        names = [e.id if isinstance(e, ast.Name) else None for e in loop.target.elts]
    else:
        return None
    if _rebinds(loop, names):
        return None
    rows = []
    for element in loop.iter.elts:
        values = element.elts if isinstance(element, ast.Tuple) else [element]
        if len(values) != len(names):
            return None
        row = {}
        for name, value in zip(names, values, strict=True):
            if name is not None and (text := _literal(value)) is not None:
                row[name] = text
        rows.append(row)
    return rows


class _Collector(ast.NodeVisitor):
    def __init__(self, path: str, found: Extraction) -> None:
        self.path = path
        self.found = found
        self.scope: list[str] = []
        self.loops: list[ast.For] = []

    def _enter(self, node: "ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef") -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._enter(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._enter(node)

    def visit_For(self, node: ast.For) -> None:
        self.loops.append(node)
        self.generic_visit(node)
        self.loops.pop()

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._lazy_table(target, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._lazy_table(node.target, node.value)
        self.generic_visit(node)

    def _lazy_table(self, target: ast.AST, value: "ast.AST | None") -> None:
        if self.scope or not isinstance(target, ast.Name) or target.id not in LAZY_TABLES:
            return
        if not isinstance(value, ast.Dict):
            return
        self.found.lazy_table_files.append(self.path)
        for key, entry in zip(value.keys, value.values, strict=True):
            name = _literal(key) if key is not None else None
            if isinstance(entry, ast.Tuple) and len(entry.elts) == 2:
                module, attr = _literal(entry.elts[0]), _literal(entry.elts[1])
                target_text = f"{module}:{attr}"
            elif target.id == "_LAZY_ATTRS":
                target_text = f"{_literal(entry)}:{name}"
            else:
                target_text = str(_literal(entry))
            self.found.literals.append(
                StringSite(self.path, entry.lineno, target_text, "lazy-table")
            )

    def visit_Call(self, node: ast.Call) -> None:
        kind = _call_kind(node)
        argument = _target_argument(node, kind) if kind is not None else None
        if kind is not None and argument is None:
            self.found.refused.append(
                f"{self.path}:{node.lineno} {kind} call in a form the audit cannot read: "
                f"{ast.unparse(node)}"
            )
        elif kind is not None and argument is not None:
            if (text := _literal(argument)) is not None:
                self.found.literals.append(StringSite(self.path, node.lineno, text, kind))
            elif kind == "cli-loader" and not _builds_a_string(argument):
                # A function or a Typer app: an object to register, not a string to import.
                pass
            else:
                rows = _table_rows(self.loops[-1]) if self.loops else None
                values = [_evaluate(argument, row) for row in rows] if rows else []
                expanded = bool(values) and all(v is not None for v in values)
                if expanded:
                    self.found.literals.extend(
                        StringSite(self.path, node.lineno, str(v), f"{kind}-table") for v in values
                    )
                scope = ".".join(self.scope) or "<module>"
                self.found.computed.append(ComputedSite(self.path, node.lineno, scope, expanded))
        self.generic_visit(node)


def strings_in(text: str, path: str, found: "Extraction | None" = None) -> Extraction:
    """Parse one file's *text* for its import strings, adding them to *found*.

    *path* is the file under ``src/`` (``otto/monitor/collector.py``); each
    site found carries it. ``tests/unit/test_monitor_service_reads_no_lab.py``
    reads the monitor's files through this too, so both guards see the same strings.
    """
    found = found if found is not None else Extraction([], [], [])
    _Collector(path, found).visit(ast.parse(text, path))
    return found


def extract(src_root: Path = SRC_ROOT) -> Extraction:
    """Parse every ``.py`` file under *src_root*/otto for its import strings."""
    found = Extraction([], [], [])
    for file in sorted((src_root / "otto").rglob("*.py")):
        strings_in(file.read_text(encoding="utf-8"), file.relative_to(src_root).as_posix(), found)
    return found


# Calls that import by a name the audit does not read, by the module that owns
# them. None is used under src/otto; a first use is read and taught to the
# extractor, or refused. Each is matched only as its module's: ``runpy.run_path``
# or a ``run_path`` imported from runpy, never another object's ``run_path``.
# ``find_spec`` only locates a module and imports nothing, so it is not here.
UNREAD_IMPORTERS = {
    "importlib": ["reload"],
    "importlib.util": ["spec_from_file_location"],
    "pkgutil": ["resolve_name"],
    "runpy": ["run_module", "run_path"],
}
# A module spec's loader method, whatever the loader is called.
UNREAD_LOADER_METHODS = ["exec_module"]

# What a lazy table may be asked without being changed.
LAZY_TABLE_READS = ["get", "keys", "values", "items", "copy", "__contains__", "__getitem__"]


def _type_positions(tree: ast.AST) -> "set[int]":
    """The nodes that name a type rather than pass a value.

    Annotations, ``isinstance``/``issubclass``'s second argument, a class's
    bases, a ``case`` pattern's class, a subscript's parameters
    (``list[Ref]``), the value of a ``TypeAlias`` or a ``type`` statement,
    ``cast``'s type and a ``TypeVar``'s bound and constraints: ``Ref`` there
    constructs nothing. A bare ``R: TypeAlias = Ref`` still binds ``Ref`` to
    another name, which the binding check names.
    """
    type_statement = getattr(ast, "TypeAlias", None)  # Python 3.12+
    roots: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.arg) and node.annotation is not None:
            roots.append(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns:
            roots.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            roots.append(node.annotation)
            alias = getattr(node.annotation, "id", None) or getattr(node.annotation, "attr", None)
            if alias == "TypeAlias" and node.value is not None:
                roots.append(node.value)
        elif isinstance(node, ast.ClassDef):
            roots += node.bases
        elif isinstance(node, ast.MatchClass):
            roots.append(node.cls)
        elif isinstance(node, ast.Subscript):
            roots.append(node.slice)
        elif type_statement is not None and isinstance(node, type_statement):
            roots.append(node.value)
        elif isinstance(node, ast.Call):
            called = _name_of_call(node)
            if called in ("isinstance", "issubclass"):
                roots += node.args[1:2]
            elif called == "cast":
                roots += node.args[:1]
            elif called == "TypeVar":
                roots += node.args[1:]
                roots += [item.value for item in node.keywords if item.arg == "bound"]
    return {id(inner) for root in roots for inner in ast.walk(root)}


def _lazy_table_writes(tree: ast.Module, path: str) -> "list[str]":
    """Every write to a lazy table other than its one module-level literal.

    The extractor reads the literal only, so an entry added by a subscript, a
    method, ``|=``, a second binding or a ``del`` would hide its target.
    """
    literal: dict[str, int] = {}
    for statement in tree.body:
        targets = statement.targets if isinstance(statement, ast.Assign) else []
        if isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
        for target in targets:
            if (
                isinstance(target, ast.Name)
                and target.id in LAZY_TABLES
                and isinstance(statement.value, ast.Dict)
            ):
                literal.setdefault(target.id, id(target))
    parent = {id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    writes = []
    for node in ast.walk(tree):
        stored = not isinstance(getattr(node, "ctx", ast.Load()), ast.Load)
        name = _table_name(node)
        # An item set or deleted, or a method other than a read, on the table.
        held = _table_name(getattr(node, "value", None))
        through = (isinstance(node, ast.Subscript) and stored) or (
            isinstance(node, ast.Attribute) and not stored and node.attr not in LAZY_TABLE_READS
        )
        # The table's own name bound or deleted, here (not its literal) or in another module.
        bound = (
            name is not None
            and stored
            and (isinstance(node, ast.Attribute) or literal.get(name) != id(node))
        )
        if (through and held is not None) or bound:
            writes.append(
                f"{path}:{node.lineno} mutates lazy table {held if through else name} "
                "outside its literal; put the entry in the literal"
            )
        elif name is not None and not stored and _passes_on(node, parent):
            writes.append(
                f"{path}:{node.lineno} passes lazy table {name} around; a write through "
                "another name would hide its entry, so read it in place"
            )
    return writes


def _table_name(node: "ast.AST | None") -> "str | None":
    """The lazy table *node* names, as ``_LAZY_ATTRS`` or ``pkg._LAZY_ATTRS``."""
    name = getattr(node, "id", None) or getattr(node, "attr", None)
    return name if isinstance(node, (ast.Name, ast.Attribute)) and name in LAZY_TABLES else None


# Builtins that only read the table they are given.
LAZY_TABLE_READERS = ["sorted", "list", "len", "set", "iter", "dict", "tuple", "frozenset"]


def _passes_on(node: ast.AST, parent: "dict[int, ast.AST]") -> bool:
    """Whether the table *node* names leaves its own name: bound, passed, returned or held."""
    up = parent.get(id(node))
    if isinstance(up, ast.keyword):
        up = parent.get(id(up))
    if isinstance(up, ast.Call):
        return node is not up.func and _name_of_call(up) not in LAZY_TABLE_READERS
    if isinstance(up, (ast.Assign, ast.AnnAssign, ast.NamedExpr, ast.Return, ast.Yield)):
        return up.value is node
    return isinstance(up, (ast.List, ast.Tuple, ast.Set, ast.Dict))


def _unread_imports(tree: ast.Module, path: str) -> "list[str]":
    """Every use of ``UNREAD_IMPORTERS``, through its module or imported from it."""
    modules = {module: module for module in UNREAD_IMPORTERS}
    names: dict[str, str] = {}
    used = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {a.asname: a.name for a in node.names if a.asname and a.name in modules}
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                local = alias.asname or alias.name
                if alias.name in UNREAD_IMPORTERS.get(node.module, []):
                    names[local] = alias.name
                    used.append([node.lineno, alias.name])
                elif f"{node.module}.{alias.name}" in UNREAD_IMPORTERS:
                    modules[local] = f"{node.module}.{alias.name}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and (
            node.attr in UNREAD_LOADER_METHODS
            or node.attr in UNREAD_IMPORTERS.get(modules.get(ast.unparse(node.value), ""), [])
        ):
            used.append([node.lineno, node.attr])
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in names:
            used.append([node.lineno, names[node.id]])
    return [
        f"{path}:{line} uses {name}, which imports by name, which the audit does not read: "
        "read it and list its target"
        for line, name in used
    ]


def spelling_problems(src_root: Path = SRC_ROOT) -> "list[str]":
    """Imports by string the extractor cannot read.

    The extractor knows a call by its name, ``Ref`` or ``import_module``, and a
    lazy table by its one literal. So a renamed import of either, ``__import__``,
    ``Ref`` or ``import_module`` passed around as a value, a write to a lazy
    table outside its literal, or a call of ``UNREAD_IMPORTERS`` would hide a
    string from it.
    """
    problems = []
    watched = {"Ref", "import_module", "importlib", "register_cli_command"}
    importers = {"import_module", "__import__", "Ref"}
    for file in sorted((src_root / "otto").rglob("*.py")):
        path = file.relative_to(src_root).as_posix()
        tree = ast.parse(file.read_text(encoding="utf-8"), str(file))
        called = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
        typed = _type_positions(tree)
        problems += _lazy_table_writes(tree, path)
        problems += _unread_imports(tree, path)
        for node in ast.walk(tree):
            name = getattr(node, "id", None) or getattr(node, "attr", None)
            if isinstance(node, ast.Call) and _name_of_call(node) == "__import__":
                problems.append(f"{path}:{node.lineno} calls __import__")
            elif (
                isinstance(node, (ast.Name, ast.Attribute))
                and name in importers
                and id(node) not in called
                and id(node) not in typed
            ):
                problems.append(f"{path}:{node.lineno} passes {name} around uncalled")
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                value = node.value
                bound = getattr(value, "id", None) or getattr(value, "attr", None)
                if isinstance(value, (ast.Name, ast.Attribute)) and bound in watched:
                    problems.append(f"{path}:{node.lineno} binds {bound} to another name")
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                problems += [
                    f"{path}:{node.lineno} imports {alias.name} as {alias.asname}"
                    for alias in node.names
                    if alias.name in watched and alias.asname not in (None, alias.name)
                ]
    # By file, then line; on one line a statement's own finding comes first.
    return sorted(problems, key=lambda p: (p.split(":")[0], int(p.split(":")[1].split(" ")[0])))


def computed_problems(found: Extraction, listed: "list[tuple[str, str, str]]") -> "list[str]":
    """Every computed site listed once, and each class's shape holds."""
    seen = collections.Counter((site.path, site.scope) for site in found.computed)
    expected = collections.Counter((path, scope) for path, scope, _ in listed)
    problems = [
        f"unlisted computed site {path} in {scope}: read it and add it to COMPUTED_SITES"
        for (path, scope) in sorted((seen - expected).elements())
    ]
    problems += [
        f"listed computed site {path} in {scope} is not in the code: delete it"
        for (path, scope) in sorted((expected - seen).elements())
    ]
    pending = collections.defaultdict(list)
    for path, scope, klass in listed:
        pending[(path, scope)].append(klass)
    # Each occurrence is checked on its own: one scope may hold two sites.
    for site in sorted(found.computed, key=lambda s: (s.path, s.line)):
        classes = pending.get((site.path, site.scope))
        if not classes:
            continue
        klass = classes.pop(0)
        where = f"{site.path}:{site.line} in {site.scope}"
        if klass == FACADE_RESOLVER and not (
            site.scope == "__getattr__" and site.path in found.lazy_table_files
        ):
            problems.append(f"{where} is no facade __getattr__ over a lazy table")
        if klass == REF_OVER_LITERAL_TABLE and not site.expanded:
            problems.append(
                f"{where}: unverified computed target; not every row of a literal "
                "table reaches it unchanged"
            )
    return problems


@functools.cache
def _found() -> Extraction:
    return extract()


def test_the_audit_sees_strings() -> None:
    found = _found()
    assert found.literals, "no import string found under src/otto (extractor broken?)"
    assert found.lazy_table_files, "no lazy table found under src/otto (extractor broken?)"


def test_every_computed_site_is_read_and_listed() -> None:
    problems = computed_problems(_found(), COMPUTED_SITES)
    assert not problems, "\n".join(problems)


def test_computed_sites_must_be_listed_and_fit_their_class() -> None:
    site = ComputedSite("otto/x.py", 9, "f", expanded=False)
    found = Extraction([], [site], [])
    assert "unlisted computed site" in computed_problems(found, [])[0]
    gone = computed_problems(Extraction([], [], []), [("otto/x.py", "f", USER_SUPPLIED)])
    assert "is not in the code" in gone[0]
    not_a_table = computed_problems(found, [("otto/x.py", "f", REF_OVER_LITERAL_TABLE)])
    assert "unverified computed target" in not_a_table[0]
    not_a_resolver = computed_problems(found, [("otto/x.py", "f", FACADE_RESOLVER)])
    assert "no facade __getattr__" in not_a_resolver[0]


def test_the_extractor_reads_every_string_shape(tmp_path: Path) -> None:
    package = tmp_path / "otto" / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        '_LAZY_ATTRS: dict[str, str] = {"Thing": "otto.other.mod"}\n'
        '_LAZY_EXPORTS = {"pair": ("otto.other", "pair"), "sub": "otto.pkg.sub"}\n'
        "def __getattr__(name):\n"
        "    import importlib\n"
        "    return getattr(importlib.import_module(_LAZY_ATTRS[name]), name)\n"
    )
    (package / "reg.py").write_text(
        "import importlib\n"
        'A = Ref("otto.other.a:A")\n'
        "def builtins():\n"
        '    for kind, module in [("x", "otto.other.x"), ("y", "otto.other.y")]:\n'
        '        register(kind, Ref(f"{module}:make"))\n'
        "def load(mod):\n"
        '    importlib.import_module("otto.other.b")\n'
        "    importlib.import_module(mod)\n"
        "def by_keyword():\n"
        '    Ref(target="otto.other.kw:K")\n'
        '    importlib.import_module(name="otto.other.kwm")\n'
        "def commands(app):\n"
        '    register_cli_command("x", "otto.other.cli:x_app", help="X.")\n'
        '    register_cli_command("y", loader="otto.other.cli:y_app", lab_free=True)\n'
        '    register_cli_command("z", app)\n'
        "def built(name):\n"
        '    register_cli_command("b", "otto.other." + name + ":app")\n'
        '    register_cli_command("c", "{}:app".format(name))\n'
        '    register_cli_command("d", "%s:app" % name)\n'
    )
    found = extract(tmp_path)
    assert sorted((s.path, s.target, s.kind) for s in found.literals) == [
        ("otto/pkg/__init__.py", "otto.other.mod:Thing", "lazy-table"),
        ("otto/pkg/__init__.py", "otto.other:pair", "lazy-table"),
        ("otto/pkg/__init__.py", "otto.pkg.sub", "lazy-table"),
        ("otto/pkg/reg.py", "otto.other.a:A", "ref"),
        ("otto/pkg/reg.py", "otto.other.b", "import_module"),
        ("otto/pkg/reg.py", "otto.other.cli:x_app", "cli-loader"),
        ("otto/pkg/reg.py", "otto.other.cli:y_app", "cli-loader"),
        ("otto/pkg/reg.py", "otto.other.kw:K", "ref"),
        ("otto/pkg/reg.py", "otto.other.kwm", "import_module"),
        ("otto/pkg/reg.py", "otto.other.x:make", "ref-table"),
        ("otto/pkg/reg.py", "otto.other.y:make", "ref-table"),
    ]
    assert sorted((s.path, s.scope, s.expanded) for s in found.computed) == [
        ("otto/pkg/__init__.py", "__getattr__", False),
        ("otto/pkg/reg.py", "built", False),
        ("otto/pkg/reg.py", "built", False),
        ("otto/pkg/reg.py", "built", False),
        ("otto/pkg/reg.py", "builtins", True),
        ("otto/pkg/reg.py", "load", False),
    ]
    assert found.lazy_table_files == ["otto/pkg/__init__.py", "otto/pkg/__init__.py"]
    assert found.refused == []
    loaders = [site for site in found.literals if site.kind == "cli-loader"]
    assert {class_of(site) for site in loaders} == {REGISTRY_BUILTIN}
    assert owner_of(module_of("otto/env/manage.py"), ["otto.config"]) == "otto.env"
    assert owner_of("otto.config.project", ["otto.config", "otto.config.scope"]) == "otto.config"


def test_no_import_spelling_escapes_the_audit() -> None:
    problems = spelling_problems() + _found().refused
    assert not problems, "\n".join(problems)


def test_the_spelling_check_names_each_hidden_spelling(tmp_path: Path) -> None:
    (tmp_path / "otto").mkdir()
    (tmp_path / "otto" / "sly.py").write_text(
        "import importlib as il\n"
        "from importlib import import_module as load\n"
        "from otto.registry import Ref as R\n"
        "from importlib import import_module\n"
        "m = __import__('otto.x')\n"
        "n = builtins.__import__('otto.y')\n"
        "load = importlib.import_module\n"
        "loaders = map(import_module, [])\n"
        "R = Ref\n"
        "from otto.cli.registry import register_cli_command as add\n"
    )
    assert spelling_problems(tmp_path) == [
        "otto/sly.py:1 imports importlib as il",
        "otto/sly.py:2 imports import_module as load",
        "otto/sly.py:3 imports Ref as R",
        "otto/sly.py:5 calls __import__",
        "otto/sly.py:6 calls __import__",
        "otto/sly.py:7 binds import_module to another name",
        "otto/sly.py:7 passes import_module around uncalled",
        "otto/sly.py:8 passes import_module around uncalled",
        "otto/sly.py:9 binds Ref to another name",
        "otto/sly.py:9 passes Ref around uncalled",
        "otto/sly.py:10 imports register_cli_command as add",
    ]


def test_the_spelling_check_names_a_write_to_a_lazy_table(tmp_path: Path) -> None:
    package = tmp_path / "otto" / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        '_LAZY_ATTRS = {"A": "otto.a"}\n'
        '_LAZY_ATTRS["B"] = "otto.testing"\n'
        '_LAZY_ATTRS.update({"C": "otto.c"})\n'
        '_LAZY_ATTRS.setdefault("D", "otto.d")\n'
        '_LAZY_ATTRS |= {"E": "otto.e"}\n'
        '_LAZY_EXPORTS = {"F": "otto.f"}\n'
        '_LAZY_EXPORTS = {"G": "otto.g"}\n'
        "def __getattr__(name):\n"
        "    return _LAZY_ATTRS.get(name), _LAZY_ATTRS[name], sorted(_LAZY_EXPORTS)\n"
        'del _LAZY_ATTRS["A"]\n'
    )
    write = "outside its literal; put the entry in the literal"
    assert spelling_problems(tmp_path) == [
        f"otto/pkg/__init__.py:2 mutates lazy table _LAZY_ATTRS {write}",
        f"otto/pkg/__init__.py:3 mutates lazy table _LAZY_ATTRS {write}",
        f"otto/pkg/__init__.py:4 mutates lazy table _LAZY_ATTRS {write}",
        f"otto/pkg/__init__.py:5 mutates lazy table _LAZY_ATTRS {write}",
        f"otto/pkg/__init__.py:7 mutates lazy table _LAZY_EXPORTS {write}",
        f"otto/pkg/__init__.py:10 mutates lazy table _LAZY_ATTRS {write}",
    ]


def test_the_spelling_check_names_a_lazy_table_reached_another_way(tmp_path: Path) -> None:
    (tmp_path / "otto").mkdir()
    (tmp_path / "otto" / "reach.py").write_text(
        '_LAZY_ATTRS = {"A": "otto.a"}\n'
        "alias = _LAZY_ATTRS\n"
        'dict.update(_LAZY_ATTRS, {"B": "otto.b"})\n'
        'pkg._LAZY_ATTRS["C"] = "otto.c"\n'
        'pkg._LAZY_EXPORTS.update(D="otto.d")\n'
        "pkg._LAZY_ATTRS = {}\n"
        "def give():\n"
        "    return _LAZY_ATTRS\n"
        "names = sorted(_LAZY_ATTRS) + list(_LAZY_ATTRS) + [len(_LAZY_ATTRS)]\n"
        'read = pkg._LAZY_ATTRS.get("A"), pkg._LAZY_ATTRS["A"], set(pkg._LAZY_EXPORTS)\n'
    )
    write = "outside its literal; put the entry in the literal"
    passed = "around; a write through another name would hide its entry, so read it in place"
    assert spelling_problems(tmp_path) == [
        f"otto/reach.py:2 passes lazy table _LAZY_ATTRS {passed}",
        f"otto/reach.py:3 passes lazy table _LAZY_ATTRS {passed}",
        f"otto/reach.py:4 mutates lazy table _LAZY_ATTRS {write}",
        f"otto/reach.py:5 mutates lazy table _LAZY_EXPORTS {write}",
        f"otto/reach.py:6 mutates lazy table _LAZY_ATTRS {write}",
        f"otto/reach.py:8 passes lazy table _LAZY_ATTRS {passed}",
    ]


def test_the_spelling_check_names_ref_used_as_a_value(tmp_path: Path) -> None:
    (tmp_path / "otto").mkdir()
    (tmp_path / "otto" / "vals.py").write_text(
        "from otto.registry import Ref\n"
        "import otto.registry as registry\n"
        'def typed(x: Ref, y: "Ref") -> Ref | None:\n'
        "    held: list[Ref] = []\n"
        "    if isinstance(x, Ref) or issubclass(type(x), (Ref, int)):\n"
        '        Ref("otto.a:b")\n'
        "    match x:\n"
        "        case Ref(target=t):\n"
        "            return held\n"
        "class Sub(Ref):\n"
        "    pass\n"
        "loaders = map(Ref, [])\n"
        "table = [Ref, registry.Ref]\n"
        "def give():\n"
        "    return Ref\n"
        'make = functools.partial(Ref, target="otto.c:d")\n'
        "Refs = list[Ref]\n"
        "RefList: TypeAlias = list[Ref]\n"
        "held = typing.cast(Ref, x)\n"
        'T = TypeVar("T", bound=Ref)\n'
        'U = TypeVar("U", Ref, int)\n'
        "R: TypeAlias = Ref\n"
    )
    assert spelling_problems(tmp_path) == [
        "otto/vals.py:12 passes Ref around uncalled",
        "otto/vals.py:13 passes Ref around uncalled",
        "otto/vals.py:13 passes Ref around uncalled",
        "otto/vals.py:15 passes Ref around uncalled",
        "otto/vals.py:16 passes Ref around uncalled",
        "otto/vals.py:22 binds Ref to another name",
    ]
    if sys.version_info >= (3, 12):
        (tmp_path / "otto" / "vals.py").write_text("type RefAlias = Ref | None\n")
        assert spelling_problems(tmp_path) == []


def test_the_spelling_check_names_each_unread_importer(tmp_path: Path) -> None:
    (tmp_path / "otto").mkdir()
    (tmp_path / "otto" / "byname.py").write_text(
        "import importlib.util\n"
        "import runpy\n"
        "from pkgutil import resolve_name\n"
        "from importlib import reload as again\n"
        'resolve_name("otto.x:y")\n'
        'runpy.run_module("otto.x")\n'
        'runpy.run_path("x.py")\n'
        "importlib.reload(again)\n"
        'spec = importlib.util.spec_from_file_location("m", "p.py")\n'
        "spec.loader.exec_module(module)\n"
        'importlib.util.find_spec("otto")\n'
        "import runpy as rp\n"
        'rp.run_path("y.py")\n'
        "from importlib import util\n"
        'util.spec_from_file_location("n", "q.py")\n'
        "settings.reload()\n"
        "reload = False\n"
        "config.run_path\n"
        "def run_module():\n"
        "    return resolve_name\n"
    )
    unread = "imports by name, which the audit does not read: read it and list its target"
    assert spelling_problems(tmp_path) == [
        f"otto/byname.py:3 uses resolve_name, which {unread}",
        f"otto/byname.py:4 uses reload, which {unread}",
        f"otto/byname.py:5 uses resolve_name, which {unread}",
        f"otto/byname.py:6 uses run_module, which {unread}",
        f"otto/byname.py:7 uses run_path, which {unread}",
        f"otto/byname.py:8 uses reload, which {unread}",
        f"otto/byname.py:8 uses reload, which {unread}",
        f"otto/byname.py:9 uses spec_from_file_location, which {unread}",
        f"otto/byname.py:10 uses exec_module, which {unread}",
        f"otto/byname.py:13 uses run_path, which {unread}",
        f"otto/byname.py:15 uses spec_from_file_location, which {unread}",
        f"otto/byname.py:20 uses resolve_name, which {unread}",
    ]


def test_the_extractor_refuses_what_it_cannot_read(tmp_path: Path) -> None:
    (tmp_path / "otto").mkdir()
    (tmp_path / "otto" / "odd.py").write_text(
        "import importlib\n"
        "def odd(args, kw):\n"
        "    Ref(*args)\n"
        "    importlib.import_module(**kw)\n"
        '    importlib.import_module(".rel", package="otto")\n'
        "def rebound():\n"
        '    for module in ["otto.a", "otto.b"]:\n'
        '        module = "otto.models.host"\n'
        '        Ref(f"{module}:X")\n'
        "def converted():\n"
        '    for module in ["otto.a"]:\n'
        '        Ref(f"{module!r}:X")\n'
        "def commands(args, kw):\n"
        "    register_cli_command(*args)\n"
        '    register_cli_command("w", **kw)\n'
        '    register_cli_command("v", "otto.a:v", loader="otto.b:v")\n'
    )
    found = extract(tmp_path)
    assert [line.split(" ")[0] for line in found.refused] == [
        "otto/odd.py:3",
        "otto/odd.py:4",
        "otto/odd.py:5",
        "otto/odd.py:14",
        "otto/odd.py:15",
        "otto/odd.py:16",
    ]
    assert found.literals == []
    assert sorted((s.scope, s.expanded) for s in found.computed) == [
        ("converted", False),
        ("rebound", False),
    ]


# The composition root, the one file whose literal import_module is a class of its own.
COMPOSITION_ROOT = "otto/bootstrap.py"

# The landings of spec 2026-10-11 placement-and-phases §6, in order. Each
# landing sets CURRENT_LANDING to its own name in its commit.
LANDINGS = ["P1a", "P1b", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9a", "P9b"]
CURRENT_LANDING = "P9b"

# What the declared graph shows once each landing is in: the modules with a
# layer (spec 2026-10-11 placement-and-phases §5.2, "modules layered") and the
# modules in an import cycle (§6, "cycle after"). They tie CURRENT_LANDING to
# the tree (landing_problems): between its landing's row and the next one the
# tree may stand anywhere, as it does mid-landing, but once it reaches the next
# row CURRENT_LANDING must be bumped, and it may never fall short of its own row.
# The last landing's row is exact. P3 and P4 share both counts (43 and 13), so
# no count separates them; nothing grandfathered expires in P4, so a landing
# left named P3 through P4 loses nothing, and P5's counts then demand the bump.
# P9a and P9b share the cycle count (0) but not the layered one (62 and 76).
# P7 layers otto.labs alone: the lab model stayed with its loader in otto.labs, so no
# module was carved for it (spec §3.7 and §5.2 as amended by P7), and its row is 55.
# P8 layers otto.reservations alone: the active run is a slot in otto.invocation, a module
# that already had its layer, so none was carved for it (spec §3.8 and §5.2 as amended by
# P8), and its row is 56.
# P9a layers the five modules that leave the cycle and the fleet's own module, otto.fleet
# (spec §3.9 and §5.2 as amended by P9a), and its row is 62.
# P9b layers the last twelve: the root package, otto.lab, otto.env, otto._shim and
# otto.__main__, the run context's home otto.run_context, and session's six files
# (spec §3.9, §3.12 and §5.2 as amended by P9b), and its row was 74. Two modules joined the
# foundation layer after it, otto.vendoring and otto.coverage.embedded_gcov (the shared
# copy/compare core and the package that ships the embedded-gcov patch), so the row was 76.
# A third, otto.config.corpus_snapshot (the one stat classification, which the json
# sources' fingerprints share with the completion cache), makes it 77.
LAYERED_AT = {
    "P1a": 19,
    "P1b": 35,
    "P2": 39,
    "P3": 43,
    "P4": 43,
    "P5": 48,
    "P6": 54,
    "P7": 55,
    "P8": 56,
    "P9a": 62,
    "P9b": 77,
}
CYCLE_AT = {
    "P1a": 22,
    "P1b": 16,
    "P2": 13,
    "P3": 13,
    "P4": 13,
    "P5": 11,
    "P6": 7,
    "P7": 6,
    "P8": 5,
    "P9a": 0,
    "P9b": 0,
}

# Every string edge: (file under src/, target, class). A target is
# "pkg.module:attr", or "pkg.module" for a module. Duplicates are separate sites.
STRING_EDGES: list[tuple[str, str, str]] = [
    # The root facade, the module otto at entry.
    ("otto/__init__.py", "otto.params:options", FACADE_BINDING),
    ("otto/__init__.py", "otto.params:register_options", FACADE_BINDING),
    ("otto/__init__.py", "otto.cli:app", FACADE_BINDING),
    ("otto/__init__.py", "otto.fleet:all_hosts", FACADE_BINDING),
    ("otto/__init__.py", "otto.fleet:get_host", FACADE_BINDING),
    ("otto/__init__.py", "otto.fleet:get_lab", FACADE_BINDING),
    ("otto/__init__.py", "otto.labs.loader:load_lab", FACADE_BINDING),
    ("otto/__init__.py", "otto.fleet:run_on_all_hosts", FACADE_BINDING),
    ("otto/__init__.py", "otto.context:OttoContext", FACADE_BINDING),
    ("otto/__init__.py", "otto.context:get_context", FACADE_BINDING),
    ("otto/__init__.py", "otto.context:open_context", FACADE_BINDING),
    ("otto/__init__.py", "otto.context:try_get_context", FACADE_BINDING),
    ("otto/__init__.py", "otto.result:Result", FACADE_BINDING),
    ("otto/__init__.py", "otto.result:CommandResult", FACADE_BINDING),
    ("otto/__init__.py", "otto.result:Results", FACADE_BINDING),
    ("otto/__init__.py", "otto.cli.registry:register_cli_command", FACADE_BINDING),
    ("otto/__init__.py", "otto.cli.registry:cli_command", FACADE_BINDING),
    ("otto/__init__.py", "otto.host.login_proxy:Cred", FACADE_BINDING),
    ("otto/__init__.py", "otto.host.login_proxy:register_login_proxy", FACADE_BINDING),
    ("otto/__init__.py", "otto.host.session_setup:SetupContext", FACADE_BINDING),
    ("otto/__init__.py", "otto.host.session_setup:register_session_setup", FACADE_BINDING),
    ("otto/__init__.py", "otto.host.app_shell:AppShell", FACADE_BINDING),
    ("otto/__init__.py", "otto.host.app_shell:Parsed", FACADE_BINDING),
    ("otto/__init__.py", "otto.project.actions:ProjectActions", FACADE_BINDING),
    ("otto/__init__.py", "otto.project.actions:register_project_actions", FACADE_BINDING),
    ("otto/__init__.py", "otto.project.state:InstallState", FACADE_BINDING),
    ("otto/__init__.py", "otto.result:ShellResult", FACADE_BINDING),
    ("otto/__init__.py", "otto.suite.run:run_tests", FACADE_BINDING),
    ("otto/__init__.py", "otto.suite.run:RunOptions", FACADE_BINDING),
    ("otto/__init__.py", "otto.utils:Status", FACADE_BINDING),
    # The composition root's lazy re-export of discovery's public name, from its owner.
    ("otto/bootstrap.py", "otto.config.project:get_env", FACADE_BINDING),
    # The run context's facade: the invocation leaf's four names and its home's ten.
    ("otto/context.py", "otto.invocation:ContextBinding", FACADE_BINDING),
    ("otto/context.py", "otto.invocation:HostResolver", FACADE_BINDING),
    ("otto/context.py", "otto.invocation:RunPolicy", FACADE_BINDING),
    ("otto/context.py", "otto.invocation:Variant", FACADE_BINDING),
    ("otto/context.py", "otto.run_context:HostOverrides", FACADE_BINDING),
    ("otto/context.py", "otto.run_context:OttoContext", FACADE_BINDING),
    ("otto/context.py", "otto.run_context:ProjectContextView", FACADE_BINDING),
    ("otto/context.py", "otto.run_context:get_context", FACADE_BINDING),
    ("otto/context.py", "otto.run_context:open_context", FACADE_BINDING),
    ("otto/context.py", "otto.run_context:reset_context", FACADE_BINDING),
    ("otto/context.py", "otto.run_context:reset_variant", FACADE_BINDING),
    ("otto/context.py", "otto.run_context:set_context", FACADE_BINDING),
    ("otto/context.py", "otto.run_context:set_variant", FACADE_BINDING),
    ("otto/context.py", "otto.run_context:try_get_context", FACADE_BINDING),
    ("otto/context.py", "otto.run_context:variant", FACADE_BINDING),
    # The otto.config facade's settings types, from the foundation scope module.
    ("otto/config/__init__.py", "otto.config.scope:DockerCompose", FACADE_BINDING),
    ("otto/config/__init__.py", "otto.config.scope:DockerImage", FACADE_BINDING),
    ("otto/config/__init__.py", "otto.config.scope:DockerSettings", FACADE_BINDING),
    ("otto/config/__init__.py", "otto.config.scope:MonitorSettings", FACADE_BINDING),
    # ... and the user settings file's names, from their module of its own.
    ("otto/config/__init__.py", "otto.config.user_settings:CacheSetting", FACADE_BINDING),
    ("otto/config/__init__.py", "otto.config.user_settings:cache_enabled", FACADE_BINDING),
    (
        "otto/config/__init__.py",
        "otto.config.user_settings:clear_user_settings_memo",
        FACADE_BINDING,
    ),
    ("otto/config/__init__.py", "otto.config.user_settings:load_user_settings", FACADE_BINDING),
    ("otto/config/__init__.py", "otto.config.user_settings:user_settings_path", FACADE_BINDING),
    (
        "otto/coverage/__init__.py",
        "otto.config.coverage_settings:CoverageConfigError",
        FACADE_BINDING,
    ),
    ("otto/host/__init__.py", "otto.result:CommandResult", FACADE_BINDING),
    ("otto/host/__init__.py", "otto.result:Results", FACADE_BINDING),
    # The otto.lab facade, at entry.
    ("otto/lab/__init__.py", "otto.labs.loader:Lab", FACADE_BINDING),
    ("otto/lab/__init__.py", "otto.labs.loader:load_lab", FACADE_BINDING),
    ("otto/lab/__init__.py", "otto.fleet:get_lab", FACADE_BINDING),
    ("otto/lab/__init__.py", "otto.fleet:get_host", FACADE_BINDING),
    ("otto/lab/__init__.py", "otto.fleet:all_hosts", FACADE_BINDING),
    ("otto/lab/__init__.py", "otto.fleet:do_for_all_hosts", FACADE_BINDING),
    ("otto/lab/__init__.py", "otto.fleet:run_on_all_hosts", FACADE_BINDING),
    ("otto/lab/__init__.py", "otto.fleet:fleet_of_interest", FACADE_BINDING),
    ("otto/lab/__init__.py", "otto.config.scope:EmptySelectionError", FACADE_BINDING),
    # The otto.link facade's bindings into its three foundation modules.
    ("otto/link/__init__.py", "otto.link.impairer:IMPAIRERS", FACADE_BINDING),
    ("otto/link/__init__.py", "otto.link.impairer:build_impairer", FACADE_BINDING),
    ("otto/link/__init__.py", "otto.link.impairer:register_impairer", FACADE_BINDING),
    ("otto/link/__init__.py", "otto.link.netem:NetEmImpairer", FACADE_BINDING),
    ("otto/link/__init__.py", "otto.link.params:ImpairmentParams", FACADE_BINDING),
    ("otto/link/__init__.py", "otto.link.params:LinkImpairer", FACADE_BINDING),
    ("otto/link/__init__.py", "otto.link.params:ScopedState", FACADE_BINDING),
    ("otto/link/__init__.py", "otto.link.params:Selector", FACADE_BINDING),
    ("otto/link/__init__.py", "otto.link.params:canonical_key", FACADE_BINDING),
    ("otto/link/__init__.py", "otto.link.params:equivalent", FACADE_BINDING),
    ("otto/link/__init__.py", "otto.link.params:parse_percent", FACADE_BINDING),
    ("otto/link/__init__.py", "otto.link.params:parse_rate", FACADE_BINDING),
    ("otto/link/__init__.py", "otto.link.params:parse_time_ms", FACADE_BINDING),
    # The impairer registry names its built-in by reference, foundation to foundation.
    ("otto/link/impairer.py", "otto.link.netem:NetEmImpairer", REGISTRY_BUILTIN),
    ("otto/models/__init__.py", "otto.models.base:OttoModel", FACADE_BINDING),
    ("otto/models/__init__.py", "otto.models.base:CredSpec", FACADE_BINDING),
    ("otto/models/__init__.py", "otto.utils:MIN_INTERVAL_SECONDS", FACADE_BINDING),
    ("otto/models/__init__.py", "otto.utils:validate_interval", FACADE_BINDING),
    # The host specs the otto.models facade still exports, from their home in otto.host.
    ("otto/models/__init__.py", "otto.host.specs:ToolchainSpec", FACADE_BINDING),
    ("otto/models/__init__.py", "otto.host.option_specs:FtpOptionsSpec", FACADE_BINDING),
    ("otto/models/__init__.py", "otto.host.option_specs:NcOptionsSpec", FACADE_BINDING),
    ("otto/models/__init__.py", "otto.host.option_specs:ScpOptionsSpec", FACADE_BINDING),
    ("otto/models/__init__.py", "otto.host.option_specs:SftpOptionsSpec", FACADE_BINDING),
    ("otto/models/__init__.py", "otto.host.option_specs:SnmpOptionsSpec", FACADE_BINDING),
    ("otto/models/__init__.py", "otto.host.option_specs:SshOptionsSpec", FACADE_BINDING),
    ("otto/models/__init__.py", "otto.host.option_specs:TelnetOptionsSpec", FACADE_BINDING),
    ("otto/models/__init__.py", "otto.host.option_specs:TftpOptionsSpec", FACADE_BINDING),
    ("otto/monitor/__init__.py", "otto.monitor.errors:MonitorInputError", FACADE_BINDING),
    ("otto/monitor/__init__.py", "otto.monitor.errors:MonitorTlsError", FACADE_BINDING),
    ("otto/monitor/__init__.py", "otto.monitor.errors:NoMonitorableHostsError", FACADE_BINDING),
    ("otto/monitor/__init__.py", "otto.monitor.errors:ReviewSourceError", FACADE_BINDING),
    ("otto/monitor/__init__.py", "otto.monitor.events:MonitorEvent", FACADE_BINDING),
    ("otto/monitor/__init__.py", "otto.monitor.parsers:DEFAULT_PARSERS", FACADE_BINDING),
    ("otto/monitor/__init__.py", "otto.monitor.parsers:MetricParser", FACADE_BINDING),
    ("otto/monitor/__init__.py", "otto.monitor.tls:resolve_monitor_tls", FACADE_BINDING),
    # The otto.session facade, at entry, over its six files, each a module of its own.
    (
        "otto/session/__init__.py",
        "otto.session.activation:check_instruction_active",
        FACADE_BINDING,
    ),
    ("otto/session/__init__.py", "otto.session.dependencies:check_dependencies", FACADE_BINDING),
    ("otto/session/__init__.py", "otto.session.errors:DependencyRefusedError", FACADE_BINDING),
    ("otto/session/__init__.py", "otto.session.errors:InstructionInactiveError", FACADE_BINDING),
    ("otto/session/__init__.py", "otto.session.errors:LabBuildError", FACADE_BINDING),
    ("otto/session/__init__.py", "otto.session.errors:LoggingLevelsConflictError", FACADE_BINDING),
    ("otto/session/__init__.py", "otto.session.errors:ProjectSelectionError", FACADE_BINDING),
    ("otto/session/__init__.py", "otto.session.errors:ProjectLoadError", FACADE_BINDING),
    ("otto/session/__init__.py", "otto.session.lab:build_lab", FACADE_BINDING),
    ("otto/session/__init__.py", "otto.session.lab:merge_host_preferences", FACADE_BINDING),
    ("otto/session/__init__.py", "otto.session.logs:install_logging", FACADE_BINDING),
    ("otto/session/__init__.py", "otto.session.logs:merge_logging_levels", FACADE_BINDING),
    ("otto/session/__init__.py", "otto.session.projects:DemotedProject", FACADE_BINDING),
    ("otto/session/__init__.py", "otto.session.projects:ProjectSelection", FACADE_BINDING),
    ("otto/session/__init__.py", "otto.session.projects:ProjectCheck", FACADE_BINDING),
    ("otto/session/__init__.py", "otto.session.projects:RequiredProject", FACADE_BINDING),
    ("otto/session/__init__.py", "otto.session.projects:check_project_overlap", FACADE_BINDING),
    ("otto/session/__init__.py", "otto.session.projects:check_projects", FACADE_BINDING),
    ("otto/session/__init__.py", "otto.session.projects:select_projects", FACADE_BINDING),
]

# Transitional exceptions: (file under src/, target, the landing that removes it).
# Shrink-only (spec 2026-10-11 placement-and-phases §5.3, "Grandfathered entries").
GRANDFATHERED: list[tuple[str, str, str]] = []

# GRANDFATHERED as the audit landed it, row for row: the spec's table. A row may
# leave GRANDFATHERED or go earlier; one added, or one going later, fails.
GRANDFATHERED_WHEN_LANDED: list[tuple[str, str, str]] = [
    ("otto/host/__init__.py", "otto.models.host:EmbeddedHostSpec", "P3"),
    ("otto/host/__init__.py", "otto.models.host:HostSpec", "P3"),
    ("otto/host/__init__.py", "otto.models.host:PowerControlSpec", "P3"),
    ("otto/host/__init__.py", "otto.models.host:UnixHostSpec", "P3"),
    ("otto/host/os_profile.py", "otto.models.host:UnixHostSpec", "P3"),
    ("otto/host/os_profile.py", "otto.models.host:EmbeddedHostSpec", "P3"),
    ("otto/host/os_profile.py", "otto.models.host:EmbeddedHostSpec", "P3"),
    ("otto/bootstrap.py", "otto.project.actions", "P9b"),
    ("otto/bootstrap.py", "otto.project.commands", "P9b"),
]


def class_of(site: StringSite) -> "str | None":
    """The class a string edge's kind and place give it, or ``None`` when it has none."""
    if site.kind == "lazy-table":
        return FACADE_BINDING
    if site.kind in ("ref", "ref-table", "cli-loader", "cli-loader-table"):
        return REGISTRY_BUILTIN
    if site.kind == "import_module" and site.path == COMPOSITION_ROOT:
        return ROOT_BY_NAME
    return None


def string_edges(sites: "list[StringSite]", declared: "list[str]") -> "list[StringSite]":
    """The sites whose target lies outside the tach module that owns their file."""
    return [
        site
        for site in sites
        if owner_of(module_of(site.path), declared) != owner_of(site.target_module, declared)
    ]


def _ends(site: StringSite, declared: "list[str]") -> "tuple[str, str]":
    return owner_of(module_of(site.path), declared), owner_of(site.target_module, declared)


def _without(sites: "list[StringSite]", keys: "list[tuple[str, str]]") -> "list[StringSite]":
    """*sites* less one site per key in *keys* (a multiset difference)."""
    remaining = collections.Counter(keys)
    kept = []
    for site in sites:
        if remaining[site.key] > 0:
            remaining[site.key] -= 1
        else:
            kept.append(site)
    return kept


# The classes of spec 2026-10-11 placement-and-phases §5.3. A user-supplied
# target is computed, so no literal string edge has that class today.
EDGE_CLASSES = [FACADE_BINDING, ROOT_BY_NAME, REGISTRY_BUILTIN, USER_SUPPLIED]


def listing_problems(
    edges: "list[StringSite]", listed: "list[tuple[str, str, str]]"
) -> "list[str]":
    """Rule 1: every string edge listed once, under its class, and nothing listed that is gone.

    A string edge with no class, or a row listed under a class the spec does
    not name, fails even when the row matches it.
    """
    found = collections.Counter((s.path, s.target, class_of(s) or "no class") for s in edges)
    expected = collections.Counter(listed)
    problems = [
        f"unlisted string edge {path} -> {target} ({klass}): add it to STRING_EDGES"
        for (path, target, klass) in sorted((found - expected).elements())
    ]
    problems += [
        f"listed string edge {path} -> {target} ({klass}) is not in the code: delete it"
        for (path, target, klass) in sorted((expected - found).elements())
    ]
    problems += [
        f"string edge {s.path}:{s.line} -> {s.target} ({s.kind}) has no class: "
        "read it and give its kind a class in class_of"
        for s in edges
        if class_of(s) is None
    ]
    problems += [
        f"listed string edge {path} -> {target} has class {klass!r}, "
        f"which is none of {EDGE_CLASSES}"
        for (path, target, klass) in listed
        if klass not in EDGE_CLASSES
    ]
    return problems


def upward_problems(
    edges: "list[StringSite]", graph: rmg.ModuleGraph, grandfathered: "list[tuple[str, str]]"
) -> "list[str]":
    """Rule 2: no string edge points up the declared layers unless it is grandfathered."""
    declared = graph.modules
    return [
        f"{site.path}:{site.line} -> {site.target} points up the layers "
        f"({graph.layer_of[src]} -> {graph.layer_of[dst]})"
        for site in _without(edges, grandfathered)
        for src, dst in [_ends(site, declared)]
        if graph.is_upward(src, dst)
    ]


def combined_cycle_problems(
    edges: "list[StringSite]", graph: rmg.ModuleGraph, grandfathered: "list[tuple[str, str]]"
) -> "list[str]":
    """Rule 3: static and string edges together close no cycle the static graph does not.

    Each problem names the string edges inside the new cycle, the ones to question.
    """
    declared = graph.modules
    combined = {module: set(deps) for module, deps in graph.depends_on.items()}
    counted = _without(edges, grandfathered)
    for site in counted:
        src, dst = _ends(site, declared)
        if src in combined and dst in combined:
            combined[src].add(dst)
    static = {frozenset(c) for c in rmg.cycles(graph)}
    joined = rmg.ModuleGraph({m: sorted(deps) for m, deps in combined.items()})
    problems = []
    for c in sorted({frozenset(c) for c in rmg.cycles(joined)} - static, key=sorted):
        inside = [
            f"{site.path}:{site.line} -> {site.target}"
            for site in counted
            if set(_ends(site, declared)) <= c
        ]
        problems.append(
            "static and string edges together close a cycle the static graph does not: "
            f"{sorted(c)}; the string edges inside it: {', '.join(inside)}"
        )
    return problems


def grandfathered_pair_problems(
    edges: "list[StringSite]", declared: "list[str]", grandfathered: "list[tuple[str, str]]"
) -> "list[str]":
    """Rule 4: no new string edge between two modules a grandfathered edge joins."""
    held = {_ends(site, declared) for site in edges if site.key in grandfathered}
    return [
        f"{site.path}:{site.line} -> {site.target} adds to the grandfathered edge "
        f"{' -> '.join(_ends(site, declared))}"
        for site in _without(edges, grandfathered)
        if _ends(site, declared) in held
    ]


def expiry_problems(
    edges: "list[StringSite]", entries: "list[tuple[str, str, str]]", current: str
) -> "list[str]":
    """Rule 5: a grandfathered entry exists in the code and is gone by its landing."""
    present = collections.Counter(site.key for site in edges)
    if current not in LANDINGS:
        return [f"the current landing {current!r} is not one of LANDINGS {LANDINGS}"]
    problems = []
    for path, target, expiry in entries:
        if present[(path, target)] == 0:
            problems.append(f"grandfathered {path} -> {target} is not in the code: delete it")
        else:
            present[(path, target)] -= 1
        if expiry not in LANDINGS:
            problems.append(
                f"grandfathered {path} -> {target} goes in {expiry!r}, "
                f"which is not one of LANDINGS {LANDINGS}"
            )
        elif LANDINGS.index(current) >= LANDINGS.index(expiry):
            problems.append(f"grandfathered {path} -> {target} was due to go in {expiry}")
    return problems


def grandfathered_growth(
    entries: "list[tuple[str, str, str]]", landed: "list[tuple[str, str, str]]"
) -> "list[str]":
    """The rows of *entries* that *landed* does not cover: added, or going later.

    Each row needs its own landed row with the same file and target and an
    expiry no earlier; the latest expiries are matched first.
    """

    def order(landing: str) -> int:
        return LANDINGS.index(landing) if landing in LANDINGS else len(LANDINGS)

    slots = collections.defaultdict(list)
    for path, target, expiry in landed:
        slots[(path, target)].append(order(expiry))
    amend = (
        "needs the owner's approval in a spec amendment (spec 2026-10-11 placement-and-phases §5.3)"
    )
    problems = []
    for path, target, expiry in sorted(entries, key=lambda row: -order(row[2])):
        free = slots[(path, target)]
        if not free:
            problems.append(f"grandfathered {path} -> {target} is a new exception: it {amend}")
        elif order(expiry) > max(free):
            problems.append(
                f"grandfathered {path} -> {target} now goes in {expiry}, later than "
                f"{LANDINGS[max(free)]}: a later expiry {amend}"
            )
        if free:
            free.remove(max(free))
    return problems


@functools.cache
def _graph() -> rmg.ModuleGraph:
    return rmg.load_graph()


def _grandfathered_keys() -> "list[tuple[str, str]]":
    return [(path, target) for path, target, _ in GRANDFATHERED]


def _real_edges() -> "list[StringSite]":
    return string_edges(_found().literals, _graph().modules)


def test_the_audit_sees_string_edges() -> None:
    assert _real_edges(), "no string edge found under src/otto (owner mapping broken?)"


def test_every_string_edge_is_listed_under_its_class() -> None:
    problems = listing_problems(_real_edges(), STRING_EDGES)
    assert not problems, "\n".join(problems)


def test_no_string_edge_points_up_the_layers() -> None:
    graph = _graph()
    problems = upward_problems(_real_edges(), graph, _grandfathered_keys())
    assert not problems, "\n".join(problems)


def test_string_edges_close_no_cycle_the_static_graph_does_not() -> None:
    graph = _graph()
    problems = combined_cycle_problems(_real_edges(), graph, _grandfathered_keys())
    assert not problems, "\n".join(problems)


def test_nothing_is_added_on_a_grandfathered_edge() -> None:
    graph = _graph()
    problems = grandfathered_pair_problems(_real_edges(), graph.modules, _grandfathered_keys())
    assert not problems, "\n".join(problems)


def test_grandfathered_entries_exist_and_go_on_time() -> None:
    problems = expiry_problems(_real_edges(), GRANDFATHERED, CURRENT_LANDING)
    assert not problems, "\n".join(problems)


def landing_problems(layered: int, in_cycle: int, current: str) -> "list[str]":
    """Whether a tree with these counts stands at *current*, the landing it names.

    It does from *current*'s row (``LAYERED_AT``, ``CYCLE_AT``) up to, but not
    including, the next row that differs: more modules layered and fewer in the
    cycle are a landing in progress. Reaching a later row means the name was
    not bumped; falling short of its own row means it was bumped too early or a
    layer went astray. The last landing has no next row, so its row is exact.
    """
    if current not in LANDINGS:
        return [f"the current landing {current!r} is not one of LANDINGS {LANDINGS}"]
    fixes = (
        "Fix one of: (1) bump CURRENT_LANDING; (2) finish layering, or undo a stray "
        "layer; (3) if the real tree legitimately differs from the spec's row, amend "
        "spec 2026-10-11 placement-and-phases §5.2/§6 and LAYERED_AT/CYCLE_AT together."
    )
    state = f"the tree has {layered} layered modules and {in_cycle} in a cycle"

    def row(landing: str) -> "list[int]":
        return [LAYERED_AT[landing], CYCLE_AT[landing]]

    def reached(landing: str) -> bool:
        return layered >= LAYERED_AT[landing] and in_cycle <= CYCLE_AT[landing]

    if current == LANDINGS[-1]:
        if [layered, in_cycle] == row(current):
            return []
        return [f"{state}; {current}, the last landing, has exactly {row(current)}. {fixes}"]
    if not reached(current):
        return [f"{state}, short of {current}'s row {row(current)}. {fixes}"]
    later = [
        landing
        for landing in LANDINGS[LANDINGS.index(current) + 1 :]
        if row(landing) != row(current) and reached(landing)
    ]
    if not later:
        return []
    bump = f"bump CURRENT_LANDING to {later[-1]}"
    return [f"{state}, which reaches {later[-1]}'s row {row(later[-1])}: {bump}. {fixes}"]


def test_the_tree_is_at_the_current_landing() -> None:
    assert list(LAYERED_AT) == LANDINGS
    assert list(CYCLE_AT) == LANDINGS
    graph = _graph()
    layered = len(graph.layer_of)
    in_cycle = sum(len(members) for members in rmg.cycles(graph))
    problems = landing_problems(layered, in_cycle, CURRENT_LANDING)
    assert not problems, "\n".join(problems)


def test_the_landing_anchor_allows_a_landing_in_progress_only() -> None:
    assert landing_problems(19, 22, "P1a") == []
    assert landing_problems(27, 19, "P1a") == [], "mid-landing, between P1a and P1b"
    assert landing_problems(35, 17, "P1a") == [], "P1b's layers, not yet its cycle"
    reached = landing_problems(35, 16, "P1a")
    assert len(reached) == 1, reached
    assert "reaches P1b's row [35, 16]: bump CURRENT_LANDING to P1b" in reached[0]
    far = landing_problems(40, 13, "P1b")
    assert "bump CURRENT_LANDING to P2" in far[0]
    short = landing_problems(18, 22, "P1a") + landing_problems(19, 23, "P1a")
    assert len(short) == 2, short
    assert all("short of P1a's row [19, 22]" in line for line in short)
    for line in [*reached, *short]:
        assert "(1) bump CURRENT_LANDING" in line
        assert "(2) finish layering, or undo a stray layer" in line
        assert "(3) if the real tree legitimately differs" in line
    assert landing_problems(43, 13, "P3") == [], "P3 and P4 share a row"
    assert landing_problems(43, 13, "P4") == []
    assert "bump CURRENT_LANDING to P5" in landing_problems(48, 11, "P3")[0]
    assert landing_problems(70, 0, "P9a") == []
    assert "bump CURRENT_LANDING to P9b" in landing_problems(77, 0, "P9a")[0]
    assert landing_problems(77, 0, "P9b") == []
    for counts in [[76, 0], [78, 0], [77, 1]]:
        assert "the last landing, has exactly [77, 0]" in landing_problems(*counts, "P9b")[0]
    assert "'P0' is not one of LANDINGS" in landing_problems(19, 22, "P0")[0]


def test_the_grandfathered_list_only_shrinks() -> None:
    problems = grandfathered_growth(GRANDFATHERED, GRANDFATHERED_WHEN_LANDED)
    assert not problems, "\n".join(problems)


# The rules on a toy graph: each one can fire, and each exemption holds.

TOY = rmg.ModuleGraph(
    {"pkg.high": ["pkg.low"], "pkg.low": []},
    layers=["top", "bottom"],
    layer_of={"pkg.high": "top", "pkg.low": "bottom"},
)
UP = StringSite("pkg/low/__init__.py", 3, "pkg.high:thing", "lazy-table")
UP_KEY = ("pkg/low/__init__.py", "pkg.high:thing")


def test_rule_1_names_an_unlisted_edge_a_stale_entry_and_a_wrong_class() -> None:
    assert listing_problems([UP], [(*UP_KEY, FACADE_BINDING)]) == []
    assert "unlisted string edge" in listing_problems([UP], [])[0]
    assert "is not in the code" in listing_problems([], [(*UP_KEY, FACADE_BINDING)])[0]
    wrong = listing_problems([UP], [(*UP_KEY, REGISTRY_BUILTIN)])
    assert len(wrong) == 2, wrong
    stray = StringSite("pkg/low/__init__.py", 3, "pkg.high", "import_module")
    assert "(no class)" in listing_problems([stray], [])[0]
    no_class = listing_problems([stray], [(*stray.key, "no class")])
    assert len(no_class) == 2, no_class
    assert "-> pkg.high (import_module) has no class" in no_class[0]
    assert "has class 'no class', which is none of" in no_class[1]


def test_rule_2_refuses_an_upward_edge_unless_grandfathered_or_unlayered() -> None:
    assert "points up the layers (bottom -> top)" in upward_problems([UP], TOY, [])[0]
    assert upward_problems([UP], TOY, [UP_KEY]) == []
    unlayered = rmg.ModuleGraph(TOY.depends_on, layers=TOY.layers, layer_of={"pkg.low": "bottom"})
    assert upward_problems([UP], unlayered, []) == []


def test_rule_3_refuses_a_cycle_only_strings_close() -> None:
    assert "close a cycle" in combined_cycle_problems([UP], TOY, [])[0]
    assert combined_cycle_problems([UP], TOY, [UP_KEY]) == []
    ring = rmg.ModuleGraph({"a": ["b"], "b": ["c"], "c": ["a"]})
    inside = StringSite("a/__init__.py", 1, "c:x", "lazy-table")
    assert combined_cycle_problems([inside], ring, []) == []
    # A string edge that pulls a module into an existing cycle enlarges it.
    below = rmg.ModuleGraph({"a": ["b"], "b": ["c"], "c": ["a"], "d": ["a"]})
    closing = StringSite("a/__init__.py", 2, "d:y", "lazy-table")
    enlarged = combined_cycle_problems([inside, closing], below, [])
    assert len(enlarged) == 1, enlarged
    assert enlarged[0].endswith(
        "does not: ['a', 'b', 'c', 'd']; the string edges inside it: "
        "a/__init__.py:1 -> c:x, a/__init__.py:2 -> d:y"
    )


def test_rule_4_refuses_an_addition_on_a_grandfathered_edge() -> None:
    more = StringSite("pkg/low/__init__.py", 4, "pkg.high:other", "lazy-table")
    problems = grandfathered_pair_problems([UP, more], TOY.modules, [UP_KEY])
    assert len(problems) == 1, problems
    assert "pkg.high:other" in problems[0]
    assert grandfathered_pair_problems([UP], TOY.modules, [UP_KEY]) == []


def test_rule_5_refuses_an_entry_past_its_landing_or_gone_from_the_code() -> None:
    assert expiry_problems([UP], [(*UP_KEY, "P3")], "P2") == []
    assert "was due to go in P3" in expiry_problems([UP], [(*UP_KEY, "P3")], "P3")[0]
    assert "is not in the code" in expiry_problems([], [(*UP_KEY, "P3")], "P2")[0]
    unknown = expiry_problems([UP], [(*UP_KEY, "P10")], "P2")
    assert "goes in 'P10', which is not one of LANDINGS" in unknown[0]
    assert "'P0' is not one of LANDINGS" in expiry_problems([UP], [(*UP_KEY, "P3")], "P0")[0]


def test_the_grandfathered_list_refuses_a_new_row_or_a_later_expiry() -> None:
    landed = [(*UP_KEY, "P3"), (*UP_KEY, "P5")]
    assert grandfathered_growth(landed, landed) == []
    assert grandfathered_growth([(*UP_KEY, "P2")], landed) == [], "an earlier expiry is fine"
    assert grandfathered_growth([(*UP_KEY, "P5")], landed) == [], "one row may leave"
    later = grandfathered_growth([(*UP_KEY, "P5"), (*UP_KEY, "P5")], landed)
    assert len(later) == 1, later
    assert (
        "later than P3: a later expiry needs the owner's approval in a spec amendment" in later[0]
    )
    added = grandfathered_growth([*landed, (*UP_KEY, "P2")], landed)
    assert len(added) == 1, added
    assert "is a new exception: it needs the owner's approval in a spec amendment" in added[0]
    elsewhere = grandfathered_growth([("pkg/x.py", "pkg.high:thing", "P3")], landed)
    assert "is a new exception" in elsewhere[0]
