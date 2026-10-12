"""Each slot has one installer: no other otto module may import or call its write API.

A slot is a table in a low module that a higher layer fills (spec 2026-10-11
placement-and-phases §2.1). Its write API is privileged: only the slot's named
installer uses it, so a slot never becomes a general service locator. Readers
use the read API from the slot's layer upward, and this file does not limit
them.

The scan reads every ``.py`` file under ``src/otto`` and resolves what each
name refers to, so a reference counts only when it resolves to the slot's HOME
module's object. It follows:

* imports: absolute and relative, aliased or not, star imports, and
  ``import_module``, ``sys.modules[...]`` with a literal name (a concatenation of
  literals included);
* re-exports across modules: a name one otto module imports from another
  resolves to whatever that module's own module-scope binding of it resolves
  to, transitively (a cycle of re-exports ends where it started). A binding
  made inside a function or class body stays there: it never exports. A
  module-scope binding to an expression the scan cannot resolve, such as a
  call's result, makes the name that module's own, even when a star import
  from the home would otherwise supply it;
* every binding form of a name: assignment (tuple unpacking element by
  element), ``:=``, ``with ... as``, a ``for`` target over a literal sequence,
  and a parameter default; plus ``vars(x)[...]`` and ``x.__dict__[...]``.

A function of the same name defined elsewhere, or imported from another
module, is a different object and is not flagged; that is what lets a later
slot privilege a common name such as ``install``. For the same reason a bare
string holding only the name (``"install"``) is not a reference: only a string
qualified with the home counts (``"<module>:<name>"``, ``"<module>.<name>"``,
as a by-name reference spells it), or one passed to ``getattr``, ``setattr``,
``hasattr`` or ``delattr`` on an object that resolves to the home. A name
reached by a computed string the scan cannot fold is out of its reach; the
dynamic import audit (``tests/unit/test_dynamic_import_audit.py``) lists every
such site.

Two kinds of reference fail. A use of the privileged write API outside the
home and the installer: an import of it, a call, an attribute read or store, a
by-name reference. And a raw write to the slot's table through one of
``Registry``'s public write methods (``TABLE_WRITES``) anywhere but the home,
the installer included: the privileged API is the only way in. The registry's
private methods (``_restore`` and the like) are not checked: they belong to
the registry engine and to the test isolation that snapshots every table.
"""

import ast
import dataclasses
from pathlib import Path

import pytest

from otto.registry import Registry
from scripts import render_module_graph as rmg
from tests._fixtures.paths import PROJECT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

SRC = PROJECT_ROOT / "src"

TABLE_WRITES = ["register", "register_many", "unregister"]
"""``Registry``'s public write methods: a raw call to one bypasses the privileged API."""

_NAMED_ATTRIBUTE_CALLS = {"getattr", "setattr", "hasattr", "delattr"}


@dataclasses.dataclass(frozen=True)
class Slot:
    """One slot: where it lives, its one installer, its privileged write API and its tables."""

    name: str
    home: str
    """The file under ``src/`` that defines the slot and its write API."""
    installer: str
    """The one file under ``src/`` that may import or call the write API."""
    privileged: list[str]
    tables: list[str]
    """The home's tables the slot fills; only the home writes them directly."""


SLOTS = [
    Slot(
        name="first-party actions",
        home="otto/instruction_catalog.py",
        installer="otto/project/actions.py",
        privileged=["declare_first_party_actions"],
        tables=["FIRST_PARTY_ACTIONS"],
    ),
]


def package_of(rel: str) -> str:
    """The package the relative imports of the file *rel* (under ``src/``) start from."""
    module = rmg.module_of(rel)
    return module if rel.endswith("/__init__.py") else module.rpartition(".")[0]


def absolute_import(package: str, module: "str | None", level: int) -> str:
    """The absolute name of ``from <level dots><module> import ...`` written in *package*."""
    if level == 0:
        return module or ""
    base = package.split(".") if package else []
    base = base[: len(base) - (level - 1)] if level - 1 <= len(base) else []
    return ".".join([*base, module] if module else base)


@dataclasses.dataclass(frozen=True)
class Binding:
    """A name bound to an expression, by whatever statement binds it."""

    name: str
    value: ast.expr


@dataclasses.dataclass(frozen=True, order=True)
class Reference:
    """A line of a file and what it refers to: a privileged name, or ``<table>.<method>``."""

    line: int
    what: str


def _literal(node: "ast.expr | None") -> "str | None":
    """The string *node* always evaluates to (a literal, or literals concatenated), if any."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal(node.left), _literal(node.right)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.JoinedStr):
        parts = [_literal(v) for v in node.values]
        return None if None in parts else "".join(p for p in parts if p is not None)
    return None


def _bound(target: ast.expr, value: "ast.expr | None") -> "list[Binding]":
    """Each binding that binding *value* to *target* makes; tuple targets pair up."""
    if isinstance(target, ast.Name) and value is not None:
        return [Binding(target.id, value)]
    if (
        isinstance(target, (ast.Tuple, ast.List))
        and isinstance(value, (ast.Tuple, ast.List))
        and len(target.elts) == len(value.elts)
    ):
        return [pair for t, v in zip(target.elts, value.elts, strict=True) for pair in _bound(t, v)]
    return []


def _module_scope(tree: ast.Module) -> "list[ast.AST]":
    """The nodes of *tree* that run in its module scope: not inside a function or class body.

    A ``def``, ``class`` or ``lambda`` node itself is in it (it binds its name
    there); what it contains is not.
    """
    found: list[ast.AST] = []
    pending: list[ast.AST] = [tree]
    while pending:
        node = pending.pop()
        found.append(node)
        if node is tree or not isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
        ):
            pending.extend(ast.iter_child_nodes(node))
    return found


@dataclasses.dataclass
class _Scope:
    """What the names of one scope are bound to: dotted names, and the modules star-imported."""

    bindings: "dict[str, set[str]]" = dataclasses.field(default_factory=dict)
    stars: "set[str]" = dataclasses.field(default_factory=set)

    def bind(self, name: str, targets: "set[str]") -> None:
        if targets:
            self.bindings.setdefault(name, set()).update(targets)


def _bindings_in(nodes: "list[ast.AST]") -> "list[Binding]":
    """Every binding *nodes* make, whatever statement makes it."""
    found: list[Binding] = []
    for node in nodes:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                found.extend(_bound(target, node.value))
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)):
            found.extend(_bound(node.target, node.value))
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            found.extend(_bound(node.optional_vars, node.context_expr))
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)) and isinstance(
            node.iter, (ast.Tuple, ast.List, ast.Set)
        ):
            for item in node.iter.elts:
                found.extend(_bound(node.target, item))
        elif isinstance(node, ast.arguments):
            positional = [*node.posonlyargs, *node.args]
            defaulted = positional[len(positional) - len(node.defaults) :]
            found.extend(
                Binding(arg.arg, default)
                for arg, default in zip(defaulted, node.defaults, strict=True)
            )
            found.extend(
                Binding(arg.arg, default)
                for arg, default in zip(node.kwonlyargs, node.kw_defaults, strict=True)
                if default is not None
            )
    return found


class _Resolver:
    """What each expression of one file refers to, as a set of dotted names."""

    def __init__(self, tree: ast.Module, package: str) -> None:
        self.tree = tree
        self.package = package
        self.imported: list[Reference] = []
        """Every dotted name a ``from ... import`` names, with its line."""
        self.tree_resolver: _TreeResolver | None = None
        everywhere = list(ast.walk(tree))
        self.file = self._scope(everywhere, record=True)
        """Every binding anywhere in the file: how the file itself is read."""
        module_scope = _module_scope(tree)
        self.exports = self._scope(module_scope, record=False)
        """The module-scope bindings only: what another module importing a name gets."""
        self.defines = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        } | {b.name for b in _bindings_in(module_scope) if b.name not in self.exports.bindings}
        """The names the module defines itself: a re-export never rewrites one."""

    def _scope(self, nodes: "list[ast.AST]", *, record: bool) -> _Scope:
        """The bindings *nodes* make; *record* keeps each ``from ... import`` in ``imported``."""
        scope = _Scope()
        for node in nodes:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        scope.bind(alias.asname, {alias.name})
                    else:
                        head = alias.name.partition(".")[0]
                        scope.bind(head, {head})
            elif isinstance(node, ast.ImportFrom):
                source = absolute_import(self.package, node.module, node.level)
                for alias in node.names:
                    if alias.name == "*":
                        scope.stars.add(source)
                        continue
                    target = f"{source}.{alias.name}"
                    if record:
                        self.imported.append(Reference(node.lineno, target))
                    scope.bind(alias.asname or alias.name, {target})
        bindings = _bindings_in(nodes)
        # Until nothing changes, so a name bound from a name bound later resolves too.
        while True:
            before = sum(len(v) for v in scope.bindings.values())
            for binding in bindings:
                scope.bind(binding.name, self.local(binding.value, scope))
            if sum(len(v) for v in scope.bindings.values()) == before:
                return scope

    def local(self, node: "ast.expr | None", scope: "_Scope | None" = None) -> "set[str]":
        """What *node* refers to through *scope* (by default the whole file's) only."""
        scope = self.file if scope is None else scope
        if isinstance(node, ast.Name):
            return scope.bindings.get(node.id, set()) | {f"{s}.{node.id}" for s in scope.stars}
        if isinstance(node, ast.Attribute):
            return {f"{q}.{node.attr}" for q in self.local(node.value, scope)}
        if isinstance(node, ast.Subscript):
            key = _literal(node.slice)
            if key is None:
                return set()
            owner = node.value
            if isinstance(owner, ast.Call) and isinstance(owner.func, ast.Name):
                if owner.func.id == "vars" and owner.args:
                    return {f"{q}.{key}" for q in self.local(owner.args[0], scope)}
                return set()
            if isinstance(owner, ast.Attribute) and owner.attr == "__dict__":
                return {f"{q}.{key}" for q in self.local(owner.value, scope)}
            return {key} if "sys.modules" in self.local(owner, scope) else set()
        if (
            isinstance(node, ast.Call)
            and "importlib.import_module" in self.local(node.func, scope)
            and node.args
            and _literal(node.args[0]) is not None
        ):
            return {str(_literal(node.args[0]))}
        return set()

    def resolve(self, node: "ast.expr | None") -> "set[str]":
        """What *node* refers to, with every re-export through a scanned module followed."""
        found = self.local(node)
        return found if self.tree_resolver is None else self.tree_resolver.canonical(found)


class _TreeResolver:
    """Every scanned module's resolver, so a re-exported name resolves to its origin."""

    def __init__(self, src: Path) -> None:
        self.files: dict[str, str] = {}
        self.modules: dict[str, _Resolver] = {}
        for path in sorted(src.joinpath("otto").rglob("*.py")):
            rel = path.relative_to(src).as_posix()
            resolver = _Resolver(ast.parse(path.read_text(encoding="utf-8")), package_of(rel))
            resolver.tree_resolver = self
            self.files[rel] = rmg.module_of(rel)
            self.modules[rmg.module_of(rel)] = resolver

    def _through(self, name: str) -> "set[str] | None":
        """*name* one re-export closer to its origin, or ``None`` when it is the origin."""
        parts = name.split(".")
        for cut in range(len(parts) - 1, 0, -1):
            module = self.modules.get(".".join(parts[:cut]))
            if module is None:
                continue
            head, rest = parts[cut], parts[cut + 1 :]
            if head in module.defines or ".".join(parts[: cut + 1]) in self.modules:
                return None
            exports = module.exports
            origins = exports.bindings.get(head) or {f"{s}.{head}" for s in exports.stars}
            return {".".join([origin, *rest]) for origin in origins} or None
        return None

    def canonical(self, names: "set[str]") -> "set[str]":
        """*names*, each followed through re-exports to where it is defined."""
        done: set[str] = set()
        seen: set[str] = set()
        pending = list(names)
        while pending:
            name = pending.pop()
            if name in seen:
                continue
            seen.add(name)
            onward = self._through(name)
            if onward is None:
                done.add(name)
            else:
                pending.extend(onward)
        # A cycle of re-exports has no origin; keep the names it passed through.
        return done or names


def references(tree: _TreeResolver, rel: str, slot: Slot) -> "list[Reference]":
    """Every line where the file *rel* refers to *slot*'s privileged API, in line order.

    A reference's ``what`` is the privileged name, or ``<table>.<method>`` for a
    raw write to one of the slot's tables.
    """
    home = rmg.module_of(slot.home)
    resolver = tree.modules[tree.files[rel]]
    privileged = {f"{home}.{name}": name for name in slot.privileged}
    tables = {f"{home}.{table}": table for table in slot.tables}
    found: set[Reference] = {
        Reference(imported.line, privileged[target])
        for imported in resolver.imported
        for target in tree.canonical({imported.what})
        if target in privileged
    }

    def named(owner: "ast.expr", attr: str) -> "list[str]":
        resolved = resolver.resolve(owner)
        dotted = tree.canonical({f"{q}.{attr}" for q in resolved})
        hits = [privileged[q] for q in dotted if q in privileged]
        if attr in TABLE_WRITES:
            hits.extend(f"{tables[q]}.{attr}" for q in resolved if q in tables)
        return hits

    for node in ast.walk(resolver.tree):
        if isinstance(node, (ast.Name, ast.Subscript)):
            found.update(
                Reference(node.lineno, privileged[q])
                for q in resolver.resolve(node) & privileged.keys()
            )
        elif isinstance(node, ast.Attribute):
            found.update(Reference(node.lineno, hit) for hit in named(node.value, node.attr))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in _NAMED_ATTRIBUTE_CALLS
            and len(node.args) >= 2
            and _literal(node.args[1]) is not None
        ):
            found.update(
                Reference(node.lineno, hit)
                for hit in named(node.args[0], str(_literal(node.args[1])))
            )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value.strip()
            for target, name in privileged.items():
                if text in {target, f"{home}:{name}"}:
                    found.add(Reference(node.lineno, name))
    return sorted(found)


def misuses(src: Path, slots: "list[Slot]") -> "list[str]":
    """Every use of a slot's write API outside its home and installer, and every raw table write."""
    tree = _TreeResolver(src)
    problems: list[str] = []
    for rel in tree.files:
        for slot in slots:
            if rel == slot.home:
                continue
            for ref in references(tree, rel, slot):
                if "." in ref.what:
                    problems.append(
                        f"{rel}:{ref.line} calls {ref.what}, a raw write to the {slot.name} "
                        f"slot; only {slot.home} writes it, through {', '.join(slot.privileged)} "
                        "(spec 2026-10-11 placement-and-phases §2.1)"
                    )
                elif rel != slot.installer:
                    problems.append(
                        f"{rel}:{ref.line} uses {ref.what}, the {slot.name} slot's write API; only "
                        f"{slot.installer} may (spec 2026-10-11 placement-and-phases §2.1)"
                    )
    return problems


def test_only_the_installer_uses_a_slot_s_write_api() -> None:
    problems = misuses(SRC, SLOTS)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("slot", SLOTS, ids=[slot.name for slot in SLOTS])
def test_the_home_defines_the_api_and_the_installer_uses_it(slot: Slot) -> None:
    """Anti-vacuity: a renamed API, table or method, or a moved file, would pass the scan."""
    names = set(slot.privileged)
    home = ast.parse(SRC.joinpath(slot.home).read_text(encoding="utf-8"))
    defined = {
        node.name
        for node in home.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    }
    assert defined == names, f"{slot.home} defines {sorted(defined)} of {sorted(names)}"
    assigned = {
        node.target.id
        for node in home.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    assert set(slot.tables) <= assigned, f"{slot.home} assigns none of {slot.tables}"
    assert all(callable(getattr(Registry, method, None)) for method in TABLE_WRITES)
    used = {ref.what for ref in references(_TreeResolver(SRC), slot.installer, slot)}
    assert used == names, f"{slot.installer} uses {sorted(used)} of {sorted(names)}"


def _scan(tmp_path: Path, files: "dict[str, str]") -> "set[str]":
    """The files under a scratch ``otto`` package that the scan flags."""
    for name, text in files.items():
        path = tmp_path / "otto" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return {line.partition(":")[0] for line in misuses(tmp_path, SLOTS)}


def test_the_scan_reads_every_spelling(tmp_path: Path) -> None:
    slot = SLOTS[0]
    home = rmg.module_of(slot.home)
    sibling = home.rpartition(".")[2]
    api = slot.privileged[0]
    table = slot.tables[0]
    installer = rmg.module_of(slot.installer)
    head, _, tail = home.partition(".")
    spellings = {
        "imported.py": f"from {home} import {api}\n",
        "aliased.py": f"from {home} import {api} as d\n",
        "attribute.py": f"import {home} as i\ni.{api}(object)\n",
        "dotted_attribute.py": f"import {home}\n{home}.{api}(object)\n",
        "from_package.py": f"from . import {sibling} as c\nc.{api}(object)\n",
        "string.py": f"import {home} as i\ngetattr(i, {api!r})\n",
        "import_module.py": (
            f"from importlib import import_module\ngetattr(import_module({home!r}), {api!r})\n"
        ),
        "import_module_joined.py": (
            f"import importlib\ngetattr(importlib.import_module({head + '.'!r} + {tail!r}), "
            f"{api!r})\n"
        ),
        "sys_modules.py": f"import sys\ngetattr(sys.modules[{home!r}], {api!r})\n",
        "vars.py": f"import {home} as i\nvars(i)[{api!r}] = None\n",
        "dunder_dict.py": f"import {home} as i\ni.__dict__[{api!r}](object)\n",
        "qualified.py": f"TARGET = {f'{home}:{api}'!r}\n",
        "dotted.py": f"TARGET = {f'{home}.{api}'!r}\n",
        # A definition counts once it replaces the home's object: one that
        # merely shares the name is another object (the test below).
        "defined.py": f"import {home} as i\n\n\ndef {api}(cls):\n    return cls\n\n\n"
        f"i.{api} = {api}\n",
        "nested.py": f"def f():\n    from .{sibling} import {api}\n",
        "deep/nested.py": f"def f():\n    from ..{sibling} import {api}\n",
        "star.py": f"from {home} import *\n{api}(object)\n",
        # Re-exports: the installer holds both names, and another module may
        # borrow them from it, directly or through a further relay.
        "reexport_api.py": f"from {installer} import {api}\n{api}(object)\n",
        "reexport_relayed.py": "from .relay import write\nwrite(object)\n",
        "relay.py": f"from {installer} import {api} as write\n",
        "reexport_table.py": f"from .project.actions import {table}\n{table}.unregister('otto')\n",
        "raw_register.py": f"from {home} import {table}\n{table}.register('otto', None)\n",
        "raw_unregister.py": f"import {home} as i\ni.{table}.unregister('otto')\n",
        "raw_getattr.py": f"from {home} import {table} as t\ngetattr(t, 'register_many')([])\n",
        # Every way to bind the table to a new name.
        "raw_alias.py": f"from {home} import {table}\nt = {table}\nt.unregister('otto')\n",
        "raw_unpacked.py": f"from {home} import {table}\nt, u = {table}, 1\nt.unregister('otto')\n",
        "raw_walrus.py": f"from {home} import {table}\nif (t := {table}):\n    t.unregister('o')\n",
        "raw_with.py": f"from {home} import {table}\nwith {table} as t:\n    t.unregister('o')\n",
        "raw_for.py": f"from {home} import {table}\nfor t in [{table}]:\n    t.unregister('o')\n",
        "raw_default.py": (
            f"from {home} import {table}\n\n\ndef f(t={table}):\n    t.unregister('o')\n"
        ),
        "raw_keyword_default.py": (
            f"from {home} import {table}\n\n\ndef f(*, t={table}):\n    t.unregister('o')\n"
        ),
    }
    flagged = _scan(tmp_path, {**spellings, "project/actions.py": _installer_fixture(slot)})
    assert flagged == {f"otto/{name}" for name in spellings}


def _installer_fixture(slot: Slot) -> str:
    """A scratch installer that imports the API and the table, as the real one does."""
    home = rmg.module_of(slot.home)
    names = ", ".join([*slot.privileged, *slot.tables])
    return f"from {home} import {names}\n{slot.privileged[0]}(object)\n"


def test_the_installer_may_use_the_api_but_not_write_the_table_raw(tmp_path: Path) -> None:
    slot = SLOTS[0]
    home = rmg.module_of(slot.home)
    installer = slot.installer.removeprefix("otto/")
    calls_the_api = f"from {home} import {slot.privileged[0]}\n{slot.privileged[0]}(object)\n"
    assert _scan(tmp_path, {installer: calls_the_api}) == set()
    raw = f"from {home} import {slot.tables[0]}\n{slot.tables[0]}.unregister('otto')\n"
    assert _scan(tmp_path, {installer: calls_the_api + raw}) == {f"otto/{installer}"}


def test_the_scan_leaves_same_named_objects_of_other_modules_alone(tmp_path: Path) -> None:
    """The scan resolves names: only the home module's objects are privileged."""
    slot = SLOTS[0]
    home = rmg.module_of(slot.home)
    api = slot.privileged[0]
    table = slot.tables[0]
    clean = {
        "own_definition.py": f"def {api}(cls):\n    return cls\n\n\n{api}(object)\n",
        "method.py": f"class Actions:\n    def {api}(self):\n        return self.{api}\n",
        "other_module.py": f"from otto.elsewhere import {api}\n{api}(object)\n",
        "other_attribute.py": f"import otto.elsewhere as e\ne.{api}(object)\ngetattr(e, {api!r})\n",
        "other_string.py": f"TARGET = 'otto.elsewhere:{api}'\nNAME = {api!r}\n",
        "other_table.py": f"from otto.elsewhere import {table}\n{table}.register('x', None)\n",
        "reader.py": f"from {home} import {table}\n{table}.peek('otto')\n{table}.repo('otto')\n",
        "home_module.py": f"from otto import {home.rpartition('.')[2]} as c\nc.{table}.items()\n",
        # A module that defines its own same-named function and is borrowed from.
        "elsewhere.py": f"def {api}(cls):\n    return cls\n\n\n{table} = {{}}\n",
        "borrows.py": f"from .elsewhere import {api}, {table}\n{api}(object)\n{table}.clear()\n",
        # A module's own table of the same name, whose function imports the
        # slot's table locally: only module-scope bindings export, so a
        # module borrowing the name gets the module's own dict.
        "own_table.py": (
            f"{table} = {{}}\n\n\ndef f():\n    from {home} import {table}\n\n"
            f"    return {table}.peek('otto')\n"
        ),
        "borrows_own_table.py": f"from .own_table import {table}\n{table}.register('a', None)\n",
        # A name a module binds itself at module scope is its own, even when a
        # star import from the home would otherwise supply it.
        "own_table_star.py": f"from {home} import *\n\n{table} = {{}}\n",
        "borrows_star.py": f"from .own_table_star import {table}\n{table}.register('a', None)\n",
        # A cycle of re-exports resolves to no origin, and so to nothing privileged.
        "cycle_a.py": f"from .cycle_b import {api}\n{api}(object)\n",
        "cycle_b.py": f"from .cycle_a import {api}\n",
    }
    assert _scan(tmp_path, clean) == set()
