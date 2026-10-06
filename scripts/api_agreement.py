"""Ask a fresh interpreter what each declared namespace really exports.

Spec §6, "Agreement tests": every declared namespace imports, has a LITERAL
``__all__``, binds every listed name at runtime (a name imported only under
``if TYPE_CHECKING:`` fails), lists no name twice, and every name exported
from two places is one object. Aliases are judged by defining site, never by
spelling: ``otto.link:DryRunPlan`` and ``otto.tunnel:DryRunPlan`` are
different classes and that is fine.

The defining site is found STATICALLY, in this process, the way the evidence
ledger finds it: each exported name is followed through ``from X import Y
[as Z]`` (relative or absolute), ``import a.b as c``, and a facade's
``_LAZY_*`` tables (``name -> "module"`` or ``name -> ("module", "attr")``,
consulted only for names the module does not bind eagerly) to the module that
binds it by ``def``, ``class``, assignment, or an import of something outside
the tree. ``if TYPE_CHECKING:`` imports are not bindings. Exports with one
site are one group, and every runtime id within a group must be equal -- so
an ordinary instance that one import replaces between two facades' captures
is caught, though it has no ``module:qualname`` of its own, and two closures
one factory made are NOT one site although they share a qualname.

When static resolution fails (a name bound only by ``from m import *``, a
loop, ``globals()``; a name bound more than once where one binding sits in an
``if``/``try`` branch), the runtime metadata site is the fallback: an
object's own ``module:qualname`` (classes, functions, ``functools.wraps``
wrappers, ``NewType``), or ``module:<name>`` for a module object, which is
always judged by its runtime name. A type alias such as ``Callable[[Host],
None]`` forwards its generic's name and so has no runtime site. A name with
neither a static nor a runtime site is not grouped and not judged: that is
the limit of this check.

The literal is checked against the runtime value: ``__all__ = ["a"]``
followed by ``__all__ += [...]`` is not the literal a static reader sees, so
it fails too.

The child imports from ``<repo>/src`` first, so the answer describes that
tree. Anything one namespace raises -- at import, from a lazy ``__getattr__``
resolving an export, or while its ``__all__`` is read, ``SystemExit``
included -- is recorded against that namespace (or that name) only; the
others are still reported.
"""

import ast
import importlib.util
import json
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from scripts.api_declaration import StaticDeclaration  # noqa: E402 -- path set up above
from scripts.api_parse import parse_quietly  # noqa: E402 -- path set up above
from scripts.api_resolver_env import resolver_env  # noqa: E402 -- path set up above

_CHILD = r"""
import importlib, json, sys, types


def why(exc):
    return f"{type(exc).__name__}: {exc}"


def defined(obj):
    # Only a class, a function, or an object carrying its OWN __qualname__ (a
    # functools.wraps wrapper, a NewType) is defined at module:qualname. A
    # type alias (Callable[...], Literal[...], list[int]) forwards its
    # generic's name; isinstance(list[int], type) is even True on 3.10.
    return (
        issubclass(type(obj), type)
        or isinstance(obj, (types.FunctionType, types.BuiltinFunctionType, types.MethodType))
        or "__qualname__" in getattr(obj, "__dict__", {})
    )


def inspect(mod, entry):
    entry["origin"] = getattr(getattr(mod, "__spec__", None), "origin", "") or ""
    entry["is_package"] = hasattr(mod, "__path__")
    names = getattr(mod, "__all__", None)
    if names is None:
        entry["public_dir"] = sorted(n for n in vars(mod) if not n.startswith("_"))
        return
    entry["all"] = [str(n) for n in names]
    for name in entry["all"]:
        try:
            obj = getattr(mod, name)
        except AttributeError:
            entry["missing"].append(name)
            continue
        except BaseException as exc:  # a lazy export whose target is broken
            entry["missing"].append(name)
            entry["missing_reasons"][name] = why(exc)
            continue
        try:
            if isinstance(obj, types.ModuleType):
                site = "module:" + obj.__name__
            elif (
                defined(obj)
                and isinstance(getattr(obj, "__module__", None), str)
                and isinstance(getattr(obj, "__qualname__", None), str)
            ):
                site = f"{obj.__module__}:{obj.__qualname__}"
            else:
                site = ""
        except BaseException:
            site = ""  # no defining site to judge an alias by
        entry["sites"][name] = [site, id(obj)]


out = {}
for ns in sys.argv[1:]:
    entry = {"error": "", "inspect_error": "", "origin": "", "is_package": False, "all": None,
             "missing": [], "missing_reasons": {}, "public_dir": [], "sites": {}}
    out[ns] = entry
    try:
        mod = importlib.import_module(ns)
    except BaseException as exc:
        entry["error"] = why(exc)
        continue
    try:
        inspect(mod, entry)
    except BaseException as exc:
        entry["inspect_error"] = why(exc)
sys.__stdout__.write(json.dumps(out) + "\n")
sys.__stdout__.flush()
"""


_CHILD_TIMEOUT = 300  # seconds; one import of every declared namespace


class AgreementError(Exception):
    """The reporting child process itself failed."""


@dataclass
class NamespaceReport:
    """What one namespace exports at runtime."""

    name: str
    error: str = ""  # the import raised: ``<ExceptionType>: <message>``
    inspect_error: str = ""  # imported, but reading its exports raised
    origin: str = ""
    is_package: bool = False
    all: "list[str] | None" = None
    missing: list[str] = field(default_factory=list)
    missing_reasons: dict[str, str] = field(default_factory=dict)  # non-AttributeError only
    public_dir: list[str] = field(default_factory=list)
    sites: dict[str, list] = field(default_factory=dict)  # name -> [runtime site, id]
    static_sites: dict[str, str] = field(default_factory=dict)  # name -> static site


def _bound_names(target: ast.expr) -> list[str]:
    """Return the names an assignment *target* binds (``a``, ``a, *b``; not ``a.b``/``a[0]``)."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Starred):
        return _bound_names(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for elt in target.elts for name in _bound_names(elt)]
    return []


# A binding of one top-level name: ("here",) defined in this module;
# ("from", module, name) re-exported; ("module", dotted) a module object.
_Binding = tuple[str, ...]
# What one module binds: eager top-level bindings (``None`` = undecided), and
# its ``_LAZY_*`` table entries.
_Tables = tuple[dict[str, "_Binding | None"], dict[str, _Binding]]
_LAZY_TARGET_SHAPES = (1, 2)  # "module" or ("module", "attr")


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


class _ModuleScan:
    """Collect one module's top-level bindings, as :class:`StaticSites` follows them."""

    def __init__(self, package: str) -> None:
        self.package = package
        self.found: dict[str, list[tuple[_Binding | None, bool]]] = {}
        self.lazy: dict[str, _Binding] = {}

    def absolute(self, dotted: str) -> "str | None":
        try:
            return importlib.util.resolve_name(dotted, self.package)
        except (ImportError, ValueError):
            return None

    def bind(self, name: str, binding: "_Binding | None", conditional: bool) -> None:
        self.found.setdefault(name, []).append((binding, conditional))

    def tables(self) -> _Tables:
        eager: dict[str, _Binding | None] = {}
        for name, bindings in self.found.items():
            if len(bindings) > 1 and any(conditional for _, conditional in bindings):
                eager[name] = None  # which binding wins depends on the run
            else:
                eager[name] = bindings[-1][0]
        return eager, self.lazy

    def visit(self, body: list[ast.stmt], conditional: bool) -> None:
        for node in body:
            if isinstance(node, ast.If):
                if _is_type_checking(node.test):
                    self.visit(node.orelse, conditional)  # the only branch that runs
                else:
                    self.visit(node.body, True)
                    self.visit(node.orelse, True)
            elif isinstance(node, (ast.Try, getattr(ast, "TryStar", ast.Try))):
                for branch in [node.body, *(h.body for h in node.handlers)]:
                    self.visit(branch, True)
                self.visit(node.orelse, True)
                self.visit(node.finalbody, True)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                self.visit(node.body, conditional)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.bind(node.name, ("here",), conditional)
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                self.assign(node, conditional)
            elif isinstance(node, ast.ImportFrom):
                source = self.absolute("." * node.level + (node.module or ""))
                for alias in node.names:
                    if alias.name != "*":
                        binding = ("from", source, alias.name) if source else None
                        self.bind(alias.asname or alias.name, binding, conditional)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:  # ``import a.b as c`` binds a.b
                        self.bind(alias.asname, ("module", alias.name), conditional)
                    else:  # ``import a.b`` binds a
                        top = alias.name.split(".")[0]
                        self.bind(top, ("module", top), conditional)

    def assign(self, node: "ast.Assign | ast.AnnAssign | ast.AugAssign", conditional: bool) -> None:
        if node.value is None:
            return  # a bare annotation binds nothing
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            for name in _bound_names(target):
                self.bind(name, ("here",), conditional)
            if (
                isinstance(target, ast.Name)
                and target.id.startswith("_LAZY")
                and isinstance(node.value, ast.Dict)
            ):
                self.lazy_table(node.value)

    def lazy_table(self, table: ast.Dict) -> None:
        for key, value in zip(table.keys, table.values, strict=True):
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                continue
            parts = value.elts if isinstance(value, ast.Tuple) else [value]
            strings = [
                p.value for p in parts if isinstance(p, ast.Constant) and isinstance(p.value, str)
            ]
            if len(strings) != len(parts) or len(strings) not in _LAZY_TARGET_SHAPES:
                continue  # not a shape this resolver knows: the name stays unresolved
            target = self.absolute(strings[0])
            if target:
                attr = strings[1] if len(strings) > 1 else key.value
                self.lazy[key.value] = ("from", target, attr)


class StaticSites:
    """Follow ``(module, name)`` through one ``src/`` tree to its static defining site.

    A site is ``"module:name"`` for an object, ``"module:<dotted>"`` for a module
    object, or ``None`` when the source does not decide it (see the module docstring).
    """

    def __init__(self, src: Path) -> None:
        self.src = Path(src)
        self._tables: dict[str, _Tables | None] = {}

    def path(self, module: str) -> "Path | None":
        """Return *module*'s source file in the tree (a package's ``__init__.py`` first)."""
        parts = module.split(".")
        if not all(p.isidentifier() for p in parts):
            return None
        package = self.src.joinpath(*parts, "__init__.py")
        if package.is_file():
            return package
        plain = self.src.joinpath(*parts[:-1], parts[-1] + ".py")
        return plain if plain.is_file() else None

    def site(self, module: str, name: str) -> "str | None":
        """Return the static defining site of *module*'s top-level *name*."""
        seen: set[tuple[str, str]] = set()
        while (module, name) not in seen:
            seen.add((module, name))
            tables = self._bindings(module)
            if tables is None:
                return None
            eager, lazy = tables
            if name in eager:
                binding = eager[name]
            elif name in lazy:
                binding = lazy[name]
            else:  # ``from pkg import sub`` imports the submodule
                return f"module:{module}.{name}" if self.path(f"{module}.{name}") else None
            if binding is None:
                return None
            if binding[0] == "here":
                return f"{module}:{name}"
            if binding[0] == "module":
                return f"module:{binding[1]}"
            target, attr = binding[1], binding[2]
            if self.path(target.split(".")[0]) is None:
                return f"{module}:{name}"  # an object from outside the tree is bound HERE
            module, name = target, attr
        return None  # an import cycle

    def _bindings(self, module: str) -> "_Tables | None":
        if module not in self._tables:
            self._tables[module] = self._scan(module)
        return self._tables[module]

    def _scan(self, module: str) -> "_Tables | None":
        path = self.path(module)
        if path is None:
            return None
        try:
            tree = parse_quietly(path.read_text(encoding="utf-8"), str(path))
        except (OSError, SyntaxError, ValueError):
            return None  # the child's import reports it
        scan = _ModuleScan(module if path.name == "__init__.py" else module.rpartition(".")[0])
        scan.visit(tree.body, False)
        return scan.tables()


def namespace_reports(namespaces: list[str], repo: Path) -> dict[str, NamespaceReport]:
    """Report every namespace in *namespaces* from one fresh interpreter over *repo*'s ``src/``.

    :class:`AgreementError` when the child itself fails: a non-zero exit, a
    timeout, or no JSON report on its last line of output.
    """
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed argv, no shell
            [sys.executable, "-c", _CHILD, *namespaces],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
            timeout=_CHILD_TIMEOUT,
            env=resolver_env(repo),
        )
    except subprocess.TimeoutExpired as exc:
        raise AgreementError(f"the reporting child timed out after {exc.timeout:g}s") from exc
    if proc.returncode != 0:
        raise AgreementError(proc.stderr.strip() or f"exit {proc.returncode}")
    try:
        data = json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise AgreementError(f"the reporting child printed no report: {exc}") from exc
    reports = {ns: NamespaceReport(name=ns, **entry) for ns, entry in data.items()}
    static = StaticSites(Path(repo) / "src")
    for ns, report in reports.items():
        for name in report.all or []:
            site = static.site(ns, name)
            if site:
                report.static_sites[name] = site
    return reports


def literal_all_names(path: Path) -> "list[str] | None":
    """Return *path*'s module-level ``__all__`` when it is a literal list/tuple of strings."""
    tree = parse_quietly(path.read_text(encoding="utf-8"), str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
            value = node.value
            if isinstance(value, (ast.List, ast.Tuple)) and all(
                isinstance(e, ast.Constant) and isinstance(e.value, str) for e in value.elts
            ):
                return [e.value for e in value.elts]
            return None
    return None


def _group_site(static_site: str, runtime_site: str) -> str:
    """Return the site an export is grouped by: static first, runtime as the fallback.

    A module object is judged by its runtime name. A static answer of "a module"
    for an object that is not one means the source misled the resolver, so the
    runtime site decides.
    """
    if runtime_site.startswith("module:"):
        return runtime_site
    if static_site and not static_site.startswith("module:"):
        return static_site
    return runtime_site


def agreement_failures(namespaces: list[str], reports: dict[str, NamespaceReport]) -> list[str]:
    """Return one message per disagreement between the declaration and the runtime."""
    failures: list[str] = []
    objects: dict[str, set[int]] = defaultdict(set)
    where: dict[str, list[str]] = defaultdict(list)
    for ns in namespaces:
        report = reports[ns]
        if report.error:
            failures.append(f"{ns}: cannot import: {report.error}")
            continue
        if report.inspect_error:
            failures.append(f"{ns}: imports, but reading its exports fails: {report.inspect_error}")
            continue
        if report.all is None:
            failures.append(f"{ns}: has no __all__")
            continue
        literal = literal_all_names(Path(report.origin)) if report.origin.endswith(".py") else None
        if literal is None:
            failures.append(f"{ns}: __all__ is not a literal list of strings")
        elif literal != report.all:
            failures.append(f"{ns}: runtime __all__ differs from its literal")
        repeated = sorted({n for n in report.all if report.all.count(n) > 1})
        if repeated:
            failures.append(f"{ns}: __all__ repeats {repeated}")
        for name in report.missing:
            reason = report.missing_reasons.get(name)
            suffix = f" ({reason})" if reason else ""
            failures.append(f"{ns}:{name} is in __all__ but not bound at runtime{suffix}")
        for name, (runtime_site, oid) in report.sites.items():
            site = _group_site(report.static_sites.get(name, ""), runtime_site)
            if site:
                objects[site].add(oid)
                where[site].append(f"{ns}:{name}")
    failures.extend(
        f"{site} is exported as different objects: {sorted(where[site])}"
        for site in sorted(objects)
        if len(objects[site]) > 1
    )
    return failures


def declaration_from(
    reports: dict[str, NamespaceReport],
    *,
    assume_dir_when_missing: bool,
    private: "dict[str, set[str]] | None" = None,
) -> StaticDeclaration:
    """Build a validator declaration from runtime reports, plus the dump's private-member map.

    *private* maps ``"module:Class"`` to its underscore members, as
    ``scripts/api_dump_child.py`` reports them; it drives ``taught-private-member``.

    *assume_dir_when_missing* treats a namespace with no ``__all__`` as
    declaring every non-underscore global. Only the dormant report target
    uses that, so it can measure the cutover's docs work before those
    modules have their first ``__all__``.
    """
    members: dict[str, set[str]] = {}
    for ns, report in reports.items():
        if report.all is not None:
            members[ns] = set(report.all)
        else:
            members[ns] = set(report.public_dir) if assume_dir_when_missing else set()
    return StaticDeclaration(members=members, private=dict(private or {}))
