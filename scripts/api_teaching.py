"""Check that every otto name the docs and shipped examples teach is declared public.

Spec ``docs/superpowers/specs/2026-10-04-public-api-manifest-design.md``
§3 (the teaching boundary) and §6 (the docs validator). A *scope* is one
docs page or one ``.py`` file; its code blocks are read in order and a name
bound in one stays bound in the next. A ``.py`` file's whole source comes
first, then the code blocks of its docstrings. A function, lambda, class
or comprehension body is a child scope: it reads what encloses it, but what
it binds (parameters, local imports, loop variables) stays inside it. Its
imports are still checked.

A ``literalinclude``-d file is validated as its own file and lends the page
nothing. :func:`check_corpus` walks the corpus plus every include target,
each once: a ``.py`` target is a whole-file scope like any shipped example;
any other target (TOML, JSON, a capture) has its quoted object paths checked
line by line.

A name ``doctest_global_setup`` injects is not *taught*: a page that uses
one without importing it first is a ``setup-only-name`` finding, because a
reader copying the example would get a ``NameError``.

There is no exception list: what the walker cannot analyse becomes a
finding, never a skip. A Python block ``ast.parse`` rejects (a placeholder
like ``<your host>``, a TOML snippet in a ``::`` literal) is read statement
by statement: every statement that parses alone is checked like any other
code, and every line that does not still has its attribute uses of bound
otto modules, its quoted ``"otto.a:Name"`` object paths and its setup-only
names checked as text.

The finding kinds:

* ``undeclared-namespace`` -- ``import otto.x`` of a module that is not a
  declared namespace.
* ``undeclared-import`` -- ``from otto.x import N`` where ``N`` is neither a
  declared member of ``otto.x`` nor itself a declared namespace.
* ``undeclared-attribute`` -- ``m.N`` on a bound otto module ``m`` whose
  declared members do not include ``N``.
* ``undeclared-object-path`` -- a ``"otto.x:N"`` string whose ``N`` is not a
  declared member of ``otto.x``. A resolver ``getattr``-s the member and
  never imports a submodule, so a declared namespace ``otto.x.N`` does not
  excuse it.
* ``star-import`` -- ``from otto.x import *``.
* ``dynamic-import`` -- ``import_module("otto...")`` or ``__import__``.
* ``setup-only-name`` -- a name only ``doctest_global_setup`` binds.
* ``taught-private-member`` -- taught code overrides, reads, writes or passes as a
  constructor keyword an underscore member of an otto class (dump spec §7.3). The
  validator follows explicit imports, simple aliases, local subclasses and names bound
  by constructing an otto class; a supported dunder is public and never reported.
* ``unfollowable-otto-base`` -- a class base that names an otto class or module but is
  not a name, an attribute chain or a subscript of one (``class M(pick(UnixHost))``):
  the check cannot follow it, so it says so instead of passing it.
* ``unparseable-import`` -- an otto import statement that will not parse
  even on its own.
* ``unparseable`` -- a ``.py`` file in the walk that will not parse (its
  statements are still checked one by one, as a block that will not parse
  is), or a text check that could not be parsed.
* ``include-missing`` -- a ``literalinclude`` whose target does not exist.
* ``internal-error`` -- the validator raised on a file; the file's earlier
  findings are kept and the rest of the corpus is still checked.
"""

import ast
import io
import keyword
import re
import sys
import textwrap
import tokenize
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from scripts import api_docs_blocks as blocks  # noqa: E402 -- path set up above
from scripts.api_declaration import (  # noqa: E402 -- path set up above
    Declaration,
    StaticDeclaration,  # noqa: F401 -- re-exported: still importable from this module
)
from scripts.api_parse import parse_quietly  # noqa: E402 -- path set up above

PAGE_PY_LANGS = {
    "python",
    "py",
    "pycon",
    "",
    "indented",
    "{doctest}",
    "{testsetup}",
    "{testcode}",
    "{testcleanup}",
}
# A .py file's own block kinds, plus every page language: a docstring's directive blocks
# (``.. code-block:: python``, ``.. doctest::``...) carry the labels rst_blocks gives a page,
# so a docstring block is checked exactly like a page block.
FILE_PY_LANGS = {"py-file", "py-doctest", "py-literal"} | PAGE_PY_LANGS
DOCTEST_LANGS = {"{doctest}", "py-doctest", "pycon"}
OBJECT_PATH = re.compile(r"^(otto(?:\.\w+)*):(\w+)$")
OBJECT_PATH_IN_TEXT = re.compile(r"[\"'](otto(?:\.\w+)*):(\w+)[\"']")
IMPORT_START = re.compile(r"^(from\s+\S+\s+import\b|import\s+\w)")
# A dotted chain whose every segment is an identifier: ``h.264`` is not an attribute use.
ATTRIBUTE_CHAIN = r"(?:\.[^\W\d]\w*)+"
DYNAMIC_IMPORTERS = {"import_module", "__import__"}
_SETUP_FROM_IMPORT = re.compile(r"from\s+(otto(?:\.\w+)*)\s+import\s+(.+)", re.DOTALL)
_SETUP_IMPORT_OTTO = re.compile(r"import\s+otto(\s|$|\.)")


@dataclass(frozen=True)
class Finding:
    """One taught reference that the declaration does not cover."""

    path: str
    line: int
    kind: str
    detail: str

    def render(self) -> str:
        """Return ``path:line: kind: detail``."""
        return f"{self.path}:{self.line}: {self.kind}: {self.detail}"


@dataclass(frozen=True)
class _OttoClass:
    """An otto class as the private-member check sees it: its label and underscore names."""

    label: str  # "module:Class" of the (first) otto class
    private: frozenset[str]


@dataclass
class _State:
    """What one scope has bound so far; shared by every block of the scope.

    A child scope (a function, lambda, class or comprehension body) has a
    :attr:`parent`: a read falls through to it, a write stays local.
    """

    modules: dict[str, str] = field(default_factory=dict)  # local name -> otto module path
    classes: dict[str, _OttoClass] = field(default_factory=dict)  # local name -> otto class
    instances: dict[str, _OttoClass] = field(default_factory=dict)  # local name -> its class
    plain: set[str] = field(default_factory=set)  # declared otto names that are not classes
    names: set[str] = field(default_factory=set)  # every name the scope has bound
    parent: "_State | None" = None

    def _chain(self) -> "list[_State]":
        """Return this scope and its enclosing ones, innermost first."""
        chain: list[_State] = []
        scope: _State | None = self
        while scope is not None:
            chain.append(scope)
            scope = scope.parent
        return chain

    def class_of(self, name: str) -> "_OttoClass | None":
        """Return the otto class *name* is bound to, None if it is not one."""
        for scope in self._chain():
            if name in scope.names:
                return scope.classes.get(name)
        return None

    def instance_of(self, name: str) -> "_OttoClass | None":
        """Return the otto class *name* is an instance of, None if it is not one."""
        for scope in self._chain():
            if name in scope.names:
                return scope.instances.get(name)
        return None

    def is_plain_otto(self, name: str) -> bool:
        """Return True when *name* is bound to a declared otto name that is not a class."""
        for scope in self._chain():
            if name in scope.names:
                return name in scope.plain
        return False

    def bound(self, name: str) -> bool:
        """Return True when *name* is bound here or in an enclosing scope."""
        return any(name in scope.names for scope in self._chain())

    def module_of(self, name: str) -> str | None:
        """Return the otto module *name* is bound to, None if it is not one."""
        for scope in self._chain():
            if name in scope.names:
                return scope.modules.get(name)
        return None

    def visible_modules(self) -> dict[str, str]:
        """Return every visible ``local name -> otto module`` binding."""
        out: dict[str, str] = {}
        for scope in reversed(self._chain()):
            for name in scope.names:
                out.pop(name, None)
            out.update(scope.modules)
        return out


def _is_otto(module: str) -> bool:
    return module == "otto" or module.startswith("otto.")


def _object_path_findings(decl: Declaration, label: str, no: int, text: str) -> list[Finding]:
    """Return a finding for each quoted ``"otto.a:Name"`` in *text* that is not declared."""
    return [
        Finding(label, no, "undeclared-object-path", f"{module}:{name}")
        for module, name in OBJECT_PATH_IN_TEXT.findall(text)
        if not decl.declares(module, name)
    ]


def _try_parse(source: str) -> ast.Module | None:
    """:func:`~scripts.api_parse.parse_quietly`, or None when *source* is not Python.

    ``ValueError`` too: before 3.12 a NUL byte raises it instead of ``SyntaxError``.
    """
    try:
        return parse_quietly(source)
    except (SyntaxError, ValueError):
        return None


def _label(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _package_of(path: Path, repo_root: Path) -> str | None:
    """Return the package a file under ``src/`` belongs to; None outside ``src/``."""
    try:
        rel = path.resolve().relative_to((repo_root / "src").resolve())
    except ValueError:
        return None
    return ".".join(rel.with_suffix("").parts[:-1]) or None


def _resolve_from(node: ast.ImportFrom, package: str | None) -> str | None:
    """Return the absolute module a ``from`` import names; None if relative with no package."""
    if not node.level:
        return node.module
    if package is None:
        return None
    base = package.split(".")
    base = base[: len(base) - (node.level - 1)]
    return ".".join(base + ([node.module] if node.module else []))


def _without_type_arguments(base: ast.expr) -> ast.expr:
    """Return a base with its ``[...]`` type arguments dropped: they are not bases."""
    while isinstance(base, ast.Subscript):
        base = base.value
    return base


class _Checker(ast.NodeVisitor):
    """Walk one block's tree, recording findings and the bindings it makes."""

    def __init__(
        self,
        decl: Declaration,
        setup_names: set[str],
        package: str | None,
        label: str,
        nums: list[int],
        state: _State,
        out: list[Finding],
    ) -> None:
        self.decl, self.setup, self.package = decl, setup_names, package
        self.label, self.nums, self.state, self.out = label, nums, state, out

    def _add(self, node: ast.AST, kind: str, detail: str) -> None:
        lineno = getattr(node, "lineno", 1)
        self.out.append(Finding(self.label, self.nums[lineno - 1], kind, detail))

    def _bind(
        self,
        name: str,
        module: str | None = None,
        *,
        cls: "_OttoClass | None" = None,
        instance: "_OttoClass | None" = None,
        plain: bool = False,
    ) -> None:
        self.state.names.add(name)
        if plain:
            self.state.plain.add(name)
        else:
            self.state.plain.discard(name)
        for slot, value in (
            (self.state.modules, module),
            (self.state.classes, cls),
            (self.state.instances, instance),
        ):
            if value is None:
                slot.pop(name, None)
            else:
                slot[name] = value

    def _declared_class(self, module: str, name: str) -> "_OttoClass | None":
        private = self.decl.underscore_members(module, name)
        return None if private is None else _OttoClass(f"{module}:{name}", frozenset(private))

    def _class_expr(self, node: ast.expr) -> "_OttoClass | None":
        """Return the otto class *node* names: a bound name, a module chain, ``C[...]``."""
        if isinstance(node, ast.Subscript):
            return self._class_expr(node.value)
        if isinstance(node, ast.Name):
            return self.state.class_of(node.id)
        chain: list[str] = []
        cur: ast.expr = node
        while isinstance(cur, ast.Attribute):
            chain.append(cur.attr)
            cur = cur.value
        if not isinstance(cur, ast.Name):
            return None
        root = self.state.class_of(cur.id)
        if root is not None:  # a nested class reached through an otto class: ``Outer.Inner``
            module, _, name = root.label.partition(":")
            return self._declared_class(module, ".".join([name, *reversed(chain)]))
        path = self.state.module_of(cur.id)
        if path is None:
            return None
        parts = list(reversed(chain))
        while parts and self.decl.is_namespace(f"{path}.{parts[0]}"):
            path = f"{path}.{parts.pop(0)}"
        return self._declared_class(path, ".".join(parts)) if parts else None

    def _instance_expr(self, node: ast.expr) -> "_OttoClass | None":
        """Return the otto class *node* is an instance of: a bound name or a constructor call."""
        if isinstance(node, ast.Name):
            return self.state.instance_of(node.id)
        if isinstance(node, ast.Call):
            return self._class_expr(node.func)
        return None

    def _private_use(self, node: ast.AST, owner: "_OttoClass | None", name: str, how: str) -> None:
        if owner is not None and name in owner.private:
            self._add(node, "taught-private-member", f"{owner.label}.{name}: {how}")

    def _mentions_otto(self, node: ast.expr) -> bool:
        return any(
            isinstance(sub, ast.Name)
            and (
                self.state.module_of(sub.id)
                or self.state.class_of(sub.id)
                or self.state.instance_of(sub.id)
                or self.state.is_plain_otto(sub.id)
            )
            for sub in ast.walk(node)
        )

    @contextmanager
    def _child(self) -> Iterator[None]:
        """Visit a body in a child scope: it reads the enclosing state, its writes stay local."""
        saved = self.state
        self.state = _State(parent=saved)
        try:
            yield
        finally:
            self.state = saved

    def _visit_all(self, nodes: Sequence[ast.AST]) -> None:
        for item in nodes:
            self.visit(item)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if _is_otto(alias.name) and not self.decl.is_namespace(alias.name):
                self._add(node, "undeclared-namespace", alias.name)
            top = alias.name.split(".")[0]
            if alias.asname:
                self._bind(alias.asname, alias.name if _is_otto(alias.name) else None)
            else:
                self._bind(top, top if _is_otto(alias.name) else None)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = _resolve_from(node, self.package)
        for alias in node.names:
            local = alias.asname or alias.name
            if module is None or not _is_otto(module):
                if alias.name != "*":
                    self._bind(local)
                continue
            if alias.name == "*":
                self._add(node, "star-import", module)
                continue
            submodule = f"{module}.{alias.name}"
            if self.decl.is_namespace(submodule):
                self._bind(local, submodule)
            elif self.decl.declares(module, alias.name):
                cls = self._declared_class(module, alias.name)
                self._bind(local, cls=cls, plain=cls is None)
            else:
                self._add(node, "undeclared-import", f"{module}:{alias.name}")
                self._bind(local)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        chain: list[str] = []
        cur: ast.expr = node
        while isinstance(cur, ast.Attribute):
            owner = self._class_expr(cur.value) or self._instance_expr(cur.value)
            self._private_use(cur, owner, cur.attr, "used")
            chain.append(cur.attr)
            cur = cur.value
        path = self.state.module_of(cur.id) if isinstance(cur, ast.Name) else None
        if path is not None:
            for part in reversed(chain):
                if self.decl.is_namespace(f"{path}.{part}"):
                    path = f"{path}.{part}"
                    continue
                if not self.decl.declares(path, part):
                    self._add(node, "undeclared-attribute", f"{path}:{part}")
                break
        self.visit(cur)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self._bind(node.id)
        elif node.id in self.setup and not self.state.bound(node.id):
            self._add(node, "setup-only-name", node.id)

    def visit_Assign(self, node: ast.Assign) -> None:
        # The value is evaluated before the targets bind: ``d = d.x`` reads the old ``d``.
        self.visit(node.value)
        resolved = self._value_binding(node.targets, node.value)
        for target in node.targets:
            self.visit(target)
        self._bind_value(node.targets, resolved)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.visit(node.annotation)
        if node.value is not None:
            self.visit(node.value)
        resolved = (
            self._value_binding([node.target], node.value) if node.value is not None else None
        )
        self.visit(node.target)
        self._bind_value([node.target], resolved)

    def _value_binding(
        self, targets: "list[ast.expr]", value: ast.expr
    ) -> "tuple[_OttoClass | None, _OttoClass | None]":
        """Resolve what the value is BEFORE the target rebinds: ``U = U`` reads the old ``U``."""
        if len(targets) != 1 or not isinstance(targets[0], ast.Name):
            return None, None
        instance = self._instance_expr(value) if isinstance(value, ast.Call) else None
        return self._class_expr(value), instance

    def _bind_value(
        self,
        targets: "list[ast.expr]",
        resolved: "tuple[_OttoClass | None, _OttoClass | None] | None",
    ) -> None:
        """``U = C`` binds an otto class; ``x = C(...)`` binds an otto instance."""
        if resolved is None or not isinstance(targets[0], ast.Name):
            return
        cls, instance = resolved
        if cls is not None or instance is not None:
            self._bind(targets[0].id, cls=cls, instance=instance)

    def _visit_with(self, node: "ast.With | ast.AsyncWith") -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self.visit(item.optional_vars)
                if isinstance(item.optional_vars, ast.Name) and isinstance(
                    item.context_expr, ast.Call
                ):
                    instance = self._instance_expr(item.context_expr)
                    if instance is not None:
                        self._bind(item.optional_vars.id, instance=instance)
        self._visit_all(node.body)

    def visit_With(self, node: ast.With) -> None:
        self._visit_with(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self._visit_with(node)

    def _bind_args(self, args: ast.arguments) -> None:
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
            self._bind(arg.arg)
        for arg in (args.vararg, args.kwarg):
            if arg is not None:
                self._bind(arg.arg)

    def _visit_signature(self, args: ast.arguments) -> None:
        """Visit what a ``def``/``lambda`` evaluates in the ENCLOSING scope."""
        self._visit_all([d for d in [*args.defaults, *args.kw_defaults] if d is not None])
        params = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
        self._visit_all([a.annotation for a in params if a is not None and a.annotation])

    def _visit_def(
        self,
        node: "ast.FunctionDef | ast.AsyncFunctionDef",
        receiver: "_OttoClass | None" = None,
    ) -> None:
        self._visit_all(node.decorator_list)
        self._visit_signature(node.args)
        if node.returns is not None:
            self.visit(node.returns)
        self._bind(node.name)
        static = any(
            isinstance(d, ast.Name) and d.id == "staticmethod" for d in node.decorator_list
        )
        with self._child():
            self._bind_args(node.args)
            first = [*node.args.posonlyargs, *node.args.args][:1]
            if receiver is not None and first and not static:
                self._bind(first[0].arg, instance=receiver)
            self._visit_all(node.body)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_def(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_def(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._visit_signature(node.args)
        with self._child():
            self._bind_args(node.args)
            self.visit(node.body)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_all([*node.decorator_list, *node.bases, *node.keywords])
        otto_bases: list[_OttoClass] = []
        for base in node.bases:
            cls = self._class_expr(base)
            if cls is not None:
                otto_bases.append(cls)
            elif self._mentions_otto(_without_type_arguments(base)):
                self._add(base, "unfollowable-otto-base", ast.unparse(base))
        merged = (
            _OttoClass(otto_bases[0].label, frozenset().union(*(b.private for b in otto_bases)))
            if otto_bases
            else None
        )
        with self._child():
            if merged is not None:
                for member in _class_scope_statements(node.body):
                    self._check_override(member, merged)
            for stmt in node.body:
                if merged is not None and isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self._visit_def(stmt, receiver=merged)
                else:
                    self.visit(stmt)
        self._bind(node.name, cls=merged)

    def _check_override(self, stmt: ast.stmt, owner: _OttoClass) -> None:
        """Report a class-body definition of one of *owner*'s underscore members."""
        names: list[tuple[ast.AST, str]] = []
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append((stmt, stmt.name))
        elif isinstance(stmt, ast.Assign):
            names += [(t, t.id) for t in stmt.targets if isinstance(t, ast.Name)]
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            names.append((stmt.target, stmt.target.id))
        for node, name in names:
            self._private_use(node, owner, name, "overridden")

    def _visit_comprehension(
        self, generators: list[ast.comprehension], results: list[ast.expr]
    ) -> None:
        with self._child():
            for gen in generators:
                self.visit(gen.iter)
                self.visit(gen.target)
                self._visit_all(gen.ifs)
            self._visit_all(results)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node.generators, [node.elt])

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension(node.generators, [node.elt])

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension(node.generators, [node.elt])

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node.generators, [node.key, node.value])

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self._bind(node.name)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            match = OBJECT_PATH.match(node.value)
            if match and not self.decl.declares(*match.groups()):
                self._add(node, "undeclared-object-path", node.value)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name in DYNAMIC_IMPORTERS and node.args:
            first = node.args[0]
            if (
                isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and _is_otto(first.value)
            ):
                self._add(node, "dynamic-import", first.value)
        cls = self._class_expr(func)
        for kw in node.keywords:
            if kw.arg is not None:
                self._private_use(kw, cls, kw.arg, "passed as a constructor keyword")
        if name in {"getattr", "setattr", "delattr", "hasattr"} and len(node.args) > 1:
            attr = node.args[1]
            if isinstance(attr, ast.Constant) and isinstance(attr.value, str):
                owner = self._class_expr(node.args[0]) or self._instance_expr(node.args[0])
                self._private_use(node, owner, attr.value, "used")
        self.generic_visit(node)


# ``except*`` exists from Python 3.11; the checker also runs on 3.10.
_TRY_NODES = tuple(getattr(ast, n) for n in ("Try", "TryStar") if hasattr(ast, n))


def _class_scope_statements(body: "list[ast.stmt]") -> "list[ast.stmt]":
    """Return every statement that executes in a class body's own scope.

    Compound statements (``if``, ``try``, ``with``, ``for``, ``while``,
    ``match``) are walked through, because a name they bind is a class
    attribute; function and nested-class bodies are not entered, because
    they have scopes of their own.
    """
    out: list[ast.stmt] = []
    for stmt in body:
        out.append(stmt)
        inner: list[list[ast.stmt]] = []
        if isinstance(stmt, (ast.If, ast.For, ast.AsyncFor, ast.While)):
            inner = [stmt.body, stmt.orelse]
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            inner = [stmt.body]
        elif isinstance(stmt, _TRY_NODES):
            inner = [stmt.body, *(h.body for h in stmt.handlers), stmt.orelse, stmt.finalbody]
        elif isinstance(stmt, ast.Match):
            inner = [case.body for case in stmt.cases]
        for part in inner:
            out += _class_scope_statements(part)
    return out


# An f-string (3.12+) or t-string (3.14+) arrives as start/middle/end tokens, not one STRING.
_STRING_OPENERS = frozenset(
    getattr(tokenize, name)
    for name in ("FSTRING_START", "TSTRING_START")
    if hasattr(tokenize, name)
)
_STRING_CLOSERS = frozenset(
    getattr(tokenize, name) for name in ("FSTRING_END", "TSTRING_END") if hasattr(tokenize, name)
)


def _string_rows(body: "list[str]") -> "set[int]":
    """Return the 0-based rows of *body* that sit inside a string literal opened on an earlier row.

    Read with :mod:`tokenize`, which never needs the block to parse: ``>>>``
    is two operators to it. A block it cannot tokenize (an unterminated
    string, a dedent to no enclosing level) has no such rows, so it is read
    as a REPL transcript, as every block with a prompt was before.
    """
    rows: set[int] = set()
    opened: list[int] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO("\n".join(body) + "\n").readline):
            if tok.type in _STRING_OPENERS:
                opened.append(tok.start[0])
            elif tok.type in _STRING_CLOSERS and opened:
                rows.update(range(opened.pop(), tok.end[0]))
            elif tok.type == tokenize.STRING and tok.end[0] > tok.start[0]:
                rows.update(range(tok.start[0], tok.end[0]))
    except (tokenize.TokenError, SyntaxError):
        return set()
    return rows


def _doctest_runs(lines: "list[tuple[int, str]]") -> "tuple[list[str], list[int]]":
    """Return the doctest runs in *lines*, prompts removed, and the line number of each line.

    A run opens on a ``>>> `` line and continues through the ``... `` lines
    after it, as a docstring's doctest is read (``scripts/api_docs_blocks.py``):
    prose that merely starts with ``...`` opens nothing.
    """
    src: list[str] = []
    nums: list[int] = []
    in_run = False
    for no, line in lines:
        stripped = line.lstrip()
        if stripped.startswith(">>> ") or stripped == ">>>":
            in_run = True
        elif not (in_run and (stripped.startswith("... ") or stripped == "...")):
            in_run = False
            continue
        src.append(stripped[4:])
        nums.append(no)
    return src, nums


def _block_source(block: blocks.Block) -> tuple[str, list[int]]:
    """Return the block's Python source and the file line number of each source line.

    A doctest block (``{doctest}``, ``pycon``, ``py-doctest``) keeps only its
    ``>>> ``/``... `` lines, prompts removed; its expected output is not
    code. A ``py-file`` block is never doctest-shaped: its ``>>>`` lines sit
    inside docstrings, which arrive as their own ``py-doctest`` blocks.

    Any other block is one of two shapes, told apart by where its first
    ``>>> `` line sits:

    * inside a string literal opened above it: a ``def`` whose docstring
      holds a doctest. Every line is code, as written, so what follows the
      closed docstring (a ``return``, an import) is checked. The doctest runs
      inside the block's strings follow, prompts removed, so the doctest is
      checked too;
    * anywhere else: a REPL transcript. It reads as code up to its first
      ``>>> `` line and as a doctest from there on.
    """
    nums = [no for no, _ in block.lines]
    # split("\n"), not splitlines(): a trailing blank fence line must keep its slot in ``nums``.
    body = textwrap.dedent("\n".join(text for _, text in block.lines)).split("\n")
    if block.lang in DOCTEST_LANGS:
        first_prompt = 0
    elif block.lang == "py-file":
        first_prompt = len(body)
    else:
        first_prompt = next(
            (i for i, ln in enumerate(body) if ln.lstrip().startswith(">>> ")), len(body)
        )
        in_strings = _string_rows(body) if first_prompt < len(body) else set()
        if first_prompt in in_strings:
            numbered = list(zip(nums, body, strict=True))
            doctest, doctest_nums = _doctest_runs([numbered[i] for i in sorted(in_strings)])
            return "\n".join([*body, *doctest]), [*nums, *doctest_nums]
    src: list[str] = []
    src_nums: list[int] = []
    for i, (no, line) in enumerate(zip(nums, body, strict=True)):
        if i < first_prompt:
            src.append(line)
            src_nums.append(no)
            continue
        stripped = line.lstrip()
        for prompt in (">>> ", "... "):
            if stripped.startswith(prompt) or stripped == prompt.strip():
                src.append(stripped[len(prompt) :])
                src_nums.append(no)
                break
    return "\n".join(src), src_nums or [1]


def _module_chain(text: str) -> str:
    """Cut a dotted ``alias.attr.attr`` match at its first keyword, so it parses."""
    parts = text.split(".")
    kept = [parts[0]]
    for part in parts[1:]:
        if keyword.iskeyword(part):
            break
        kept.append(part)
    return ".".join(kept)


def _depth(text: str) -> int:
    """Return how many brackets *text* leaves open (strings and comments are not special)."""
    return sum(text.count(c) for c in "([{") - sum(text.count(c) for c in ")]}")


def _is_otto_import(stmt: str) -> bool:
    return bool(IMPORT_START.match(stmt)) and ("otto" in stmt or stmt.startswith("from ."))


def _check_unparseable(source: str, nums: list[int], checker: _Checker) -> None:
    """Check what can still be checked in a Python block ``ast.parse`` rejects.

    The block is read one logical statement at a time (a line, joined with
    the following lines while it leaves a bracket open). A statement that
    parses alone is visited like any parsed code: every check, every
    binding. An otto import that will not parse even alone is an
    ``unparseable-import``. Any other line that will not parse -- so a block
    that is not Python at all (a TOML or shell snippet) yields nothing
    unless it names otto -- is checked as text by :func:`_check_text_line`.
    """
    # split("\n"), not splitlines(): one slot per entry of ``nums`` (a form feed is not a line).
    lines = source.split("\n")
    i = 0
    while i < len(lines):
        start, stmt = i, lines[i].strip()
        i += 1
        if not stmt:
            continue
        end, joined = i, stmt
        while _depth(joined) > 0 and end < len(lines):
            joined += "\n" + lines[end].strip()
            end += 1
        tree = _try_parse(joined)
        if tree is None and end > i:
            if _is_otto_import(stmt):
                checker.out.append(
                    Finding(checker.label, nums[start], "unparseable-import", joined)
                )
                i = end
                continue
            # The joined lines do not parse together: give each line its own chance.
            end, joined, tree = i, stmt, _try_parse(stmt)
        if tree is not None:
            _visit_at(checker, tree, nums[start:end])
            i = end
        elif _is_otto_import(stmt):
            checker.out.append(Finding(checker.label, nums[start], "unparseable-import", stmt))
        else:
            _check_text_line(stmt, nums[start], checker)


def _check_text_line(text: str, no: int, checker: _Checker) -> None:
    """Check one line of a block that will not parse, as text.

    Its quoted object paths; each ``<bound-otto-alias>.<attr>...`` use, as if
    parsed; and each setup-only name not yet bound, by identifier.
    """
    checker.out.extend(_object_path_findings(checker.decl, checker.label, no, text))
    for alias in checker.state.visible_modules():
        for match in re.finditer(rf"(?<![\w.]){re.escape(alias)}{ATTRIBUTE_CHAIN}", text):
            chain = _module_chain(match.group(0))
            if "." not in chain:
                continue
            tree = _try_parse(chain)
            if tree is None:
                checker.out.append(Finding(checker.label, no, "unparseable", chain))
                continue
            _visit_at(checker, tree, [no])
    for name in sorted(checker.setup):
        if not checker.state.bound(name) and re.search(rf"(?<![\w.]){re.escape(name)}\b", text):
            checker.out.append(Finding(checker.label, no, "setup-only-name", name))


def _visit_at(checker: _Checker, tree: ast.AST, line_nums: list[int]) -> None:
    """Visit a statement *tree* parsed alone; its line ``k`` is file line ``line_nums[k - 1]``."""
    saved = checker.nums
    checker.nums = line_nums
    checker.visit(tree)
    checker.nums = saved


def setup_otto_names(conf_path: Path) -> set[str]:
    """Return the otto names ``doctest_global_setup`` in *conf_path* binds, by bound name."""
    tree = parse_quietly(conf_path.read_text(encoding="utf-8"), str(conf_path))
    text = ""
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "doctest_global_setup" for t in node.targets
        ):
            parts = node.value.values if isinstance(node.value, ast.JoinedStr) else [node.value]
            text = "".join(
                p.value for p in parts if isinstance(p, ast.Constant) and isinstance(p.value, str)
            )
    names: set[str] = set()
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        stmt = lines[i].strip()
        i += 1
        while stmt.count("(") > stmt.count(")") and i < len(lines):
            stmt += " " + lines[i].strip()
            i += 1
        match = _SETUP_FROM_IMPORT.match(stmt)
        if match:
            for item in match.group(2).strip("() ").split(","):
                if item.strip():
                    names.add(item.strip().split(" as ")[-1].strip())
        elif _SETUP_IMPORT_OTTO.match(stmt):
            names.add("otto")
    return names


def _text_findings(path: Path, decl: Declaration, label: str) -> list[Finding]:
    """Check an included non-Python file: its quoted object paths, line by line."""
    out: list[Finding] = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for no, line in enumerate(text.splitlines(), 1):
        out.extend(_object_path_findings(decl, label, no, line))
    return out


def _unparseable_file(path: Path, label: str) -> list[Finding]:
    """Return an ``unparseable`` finding when a ``.py`` file's source will not parse.

    The validator asks the parser itself rather than trusting the walker to
    raise: the walker hands a file that will not parse back as one
    ``py-file`` block, which the fallback then checks line by line -- and a
    line that is not Python is, by design, not a finding there.
    """
    try:
        parse_quietly(path.read_text(encoding="utf-8"), str(path))
    except SyntaxError as exc:
        return [Finding(label, exc.lineno or 1, "unparseable", str(exc.msg))]
    except ValueError as exc:  # a NUL byte, before 3.12
        return [Finding(label, 1, "unparseable", str(exc))]
    return []


def _check_scope(
    path: Path,
    decl: Declaration,
    setup_names: set[str],
    repo_root: Path,
    *,
    page: bool,
    label: str,
    out: list[Finding],
) -> None:
    """Append one scope's findings to *out* (see :func:`check_file`)."""
    if path.suffix == ".py":
        out.extend(_unparseable_file(path, label))
        file_blocks, py_langs = blocks.python_blocks(path), FILE_PY_LANGS
    elif page:
        if path.suffix == ".md":
            file_blocks = blocks.markdown_blocks(path)
        else:
            file_blocks = blocks.rst_blocks(path)
        py_langs = PAGE_PY_LANGS
        for line, target in blocks.literal_includes(path, repo_root / "docs"):
            if not target.is_file():
                out.append(Finding(label, line, "include-missing", _label(target, repo_root)))
    else:
        out.extend(_text_findings(path, decl, label))
        return
    state = _State()
    package = _package_of(path, repo_root)
    for block in file_blocks:
        if block.lang not in py_langs:
            for no, text in block.lines:
                out.extend(_object_path_findings(decl, label, no, text))
            continue
        source, nums = _block_source(block)
        checker = _Checker(decl, setup_names, package, label, nums, state, out)
        tree = _try_parse(source)
        if tree is None:
            _check_unparseable(source, nums, checker)
            continue
        checker.visit(tree)


def check_file(
    path: Path,
    decl: Declaration,
    setup_names: set[str],
    *,
    repo_root: Path,
    corpus: set[Path],
) -> list[Finding]:
    """Check one scope and return its findings.

    A ``.py`` file is a whole-file scope. A ``.md``/``.rst`` file in
    *corpus* is a docs page: one scope, plus an ``include-missing`` finding
    for each ``literalinclude`` whose target does not exist. Any other file
    is a page's non-Python include: its quoted object paths are checked
    line by line. An exception raised while checking becomes an
    ``internal-error`` finding after the findings made so far.
    """
    label = _label(path, repo_root)
    out: list[Finding] = []
    page = path.suffix in {".md", ".rst"} and (path in corpus or path.resolve() in corpus)
    try:
        _check_scope(path, decl, setup_names, repo_root, page=page, label=label, out=out)
    except Exception as exc:  # noqa: BLE001 -- one file's crash must never stop the corpus
        out.append(Finding(label, 1, "internal-error", f"{type(exc).__name__}: {exc}"))
    return out


def check_corpus(repo_root: Path, decl: Declaration, setup_names: set[str]) -> list[Finding]:
    """Check every scope of the teaching corpus under *repo_root*, then every include target.

    Each file is checked once, however many pages include it, and a target
    that is itself in the corpus is checked as what it is there.
    """
    corpus = blocks.teaching_corpus(repo_root)
    corpus_set = {p.resolve() for p in corpus}
    walk = {p.resolve(): p for p in corpus}
    for page in corpus:
        if page.suffix not in {".md", ".rst"}:
            continue
        try:
            includes = blocks.literal_includes(page, repo_root / "docs")
        except Exception:  # noqa: BLE001, S112 -- check_file reports this page's failure itself
            continue
        for _, target in includes:
            if target.is_file():
                walk.setdefault(target.resolve(), target)
    out: list[Finding] = []
    for path in walk.values():
        out.extend(check_file(path, decl, setup_names, repo_root=repo_root, corpus=corpus_set))
    return out


def main(argv: list[str]) -> int:
    """Report agreement failures, validator findings and producer refusals for a manifest.

    Exit 1 if there is any, else 0; ``--report`` exits 0 either way. A manifest
    that cannot be read, or namespaces that cannot be reported, is a
    ``FAIL <reason>`` line and exit 1 in both modes, never a traceback, as
    ``scripts/api_snapshot.py`` reports them.
    """
    import argparse
    from collections import Counter

    from scripts import api_regen
    from scripts.api_agreement import (
        AgreementError,
        agreement_failures,
        declaration_from,
        namespace_reports,
    )
    from scripts.api_manifest import ManifestError, load_manifest

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--report", action="store_true", help="always exit 0 (measurement only)")
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="repository to check: its src/, docs/ (conf.py included) and README "
        "(default: this script's own repo root)",
    )
    args = parser.parse_args(argv)

    try:
        names = sorted(load_manifest(args.manifest))
    except ManifestError as exc:
        print(f"FAIL {exc}")
        return 1
    try:
        reports = namespace_reports(names, args.root)
    except AgreementError as exc:
        print(f"FAIL cannot report the declared namespaces: {exc}")
        return 1
    failures = agreement_failures(names, reports)
    generated = api_regen.generate_worktree(args.root, args.manifest, assume_dir=args.report)
    refusals = list(generated.refusals)
    decl = declaration_from(
        reports,
        assume_dir_when_missing=args.report,
        private={key: set(members) for key, members in generated.private.items()},
    )
    findings = check_corpus(args.root, decl, setup_otto_names(args.root / "docs" / "conf.py"))
    for failure in failures:
        print(f"agreement: {failure}")
    for finding in findings:
        print(finding.render())
    for refusal in refusals:
        print(f"producer refusal: {refusal}")
    counts = Counter(f.kind for f in findings)
    summary = (
        f"api-surface-report: {len(failures)} agreement failure(s), {len(findings)} finding(s)"
    )
    if counts:
        summary += f" ({', '.join(f'{k} {n}' for k, n in sorted(counts.items()))})"
    if refusals:
        summary += f", {len(refusals)} producer refusal(s)"
    print(summary)
    if args.report:
        return 0
    return 1 if failures or findings or refusals else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
