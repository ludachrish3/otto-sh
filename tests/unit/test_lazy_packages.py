"""Lazy packages: importing one name imports its module and nothing else.

Every otto package exports every public name lazily (PEP 562), so a caller
pays for the one module that defines the name it asks for rather than for the
whole package. ``PACKAGES`` are the ones whose runtime behaviour is checked
here; the AST checks below cover every package init. Each test that inspects
``sys.modules`` runs in a fresh interpreter: this process has long since
imported everything.

What one import statement must never load (a lazy name's unused siblings, a
command module's heavy dependencies) is a row in
``tests/unit/test_import_contracts.py``, which prints the chain that loaded a
forbidden module. This file keeps the package-shape checks.
"""

import ast
import importlib
import inspect
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

import otto

PACKAGES = [
    "otto._webassets",
    "otto.check",
    "otto.cli",
    "otto.coverage",
    "otto.coverage.fetcher",
    "otto.coverage.merge",
    "otto.coverage.store",
    "otto.creds",
    "otto.docker",
    "otto.env",
    "otto.host",
    "otto.host.survey",
    "otto.inventory",
    "otto.kmodcov",
    "otto.labs",
    "otto.link",
    "otto.models",
    "otto.monitor",
    "otto.project",
    "otto.reservations",
    "otto.suite",
    "otto.testing",
    "otto.tunnel",
]


def _modules_after(code: str) -> set[str]:
    out = subprocess.run(
        [sys.executable, "-c", code + "\nimport sys, json; print(json.dumps(sorted(sys.modules)))"],
        capture_output=True,
        text=True,
        check=True,
    )
    return set(json.loads(out.stdout.strip().splitlines()[-1]))


@pytest.mark.parametrize("pkg", PACKAGES)
def test_importing_the_package_imports_none_of_its_submodules(pkg):
    mods = _modules_after(f"import {pkg}")
    loaded = sorted(m for m in mods if m.startswith(pkg + "."))
    assert not loaded, loaded


@pytest.mark.parametrize("pkg", PACKAGES)
def test_every_export_is_lazy(pkg):
    """The whole public surface is in the table, so none of it is imported eagerly."""
    module = importlib.import_module(pkg)
    assert set(module.__all__) == set(module._LAZY_ATTRS)


@pytest.mark.parametrize("pkg", PACKAGES)
def test_lazy_exports_are_the_real_objects_and_star_import_works(pkg):
    module = importlib.import_module(pkg)
    for name in module.__all__:
        obj = getattr(module, name)
        home = importlib.import_module(module._LAZY_ATTRS[name])
        assert getattr(home, name) is obj, name
        # Identity alone cannot catch a table entry naming a module that merely
        # re-exports the object: that module hands back the same object. A class
        # or function knows where it was defined. An alias (OsType = str, a
        # Callable[...] type) carries another name and a constant carries none,
        # so both are skipped.
        is_definition = inspect.isclass(obj) or inspect.isfunction(obj)
        if is_definition and getattr(obj, "__name__", None) == name:
            assert obj.__module__ == module._LAZY_ATTRS[name], name
        # A submodule sharing an export's name would win the attribute once
        # imported, handing back the module instead of the export.
        assert not isinstance(obj, ModuleType), name
    namespace: dict[str, object] = {}
    exec(f"from {pkg} import *", namespace)  # noqa: S102 — a star import needs a statement
    assert set(module.__all__) <= set(namespace)
    assert set(module.__all__) <= set(dir(module))


@pytest.mark.parametrize("pkg", PACKAGES)
def test_unknown_attribute_raises_attribute_error(pkg):
    module = importlib.import_module(pkg)
    with pytest.raises(AttributeError, match="no_such_name"):
        module.no_such_name  # noqa: B018


SRC_OTTO = Path(otto.__file__).parent


def _lazy_table_nodes(tree: ast.Module) -> list[ast.Dict]:
    """The literal dict of every module-level ``_LAZY_* = {...}`` assignment."""
    tables = []
    for node in tree.body:
        if isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        else:
            continue
        is_lazy_table = isinstance(target, ast.Name) and target.id.startswith("_LAZY_")
        if is_lazy_table and isinstance(value, ast.Dict):
            tables.append(value)
    return tables


def _lazy_package_inits() -> list[Path]:
    """Every package ``__init__`` under src/otto holding a module-level ``_LAZY_*`` dict."""
    inits = sorted(SRC_OTTO.rglob("__init__.py"))
    return [p for p in inits if _lazy_table_nodes(ast.parse(p.read_text()))]


def _package_of(path: Path) -> str:
    return ".".join(path.parent.relative_to(SRC_OTTO.parent).parts)


def _type_checking_imports(path: Path) -> dict[str, list[str | None]]:
    """name -> [module, attribute] for each import under ``if TYPE_CHECKING:``.

    ``from pkg import sub`` naming a submodule resolves to ``[pkg.sub, None]``:
    the name IS that module.
    """
    package = _package_of(path)
    found: dict[str, list[str | None]] = {}
    for node in ast.parse(path.read_text()).body:
        if not (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "TYPE_CHECKING"
        ):
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.ImportFrom):
                continue
            base = package.split(".")
            base = base[: len(base) - stmt.level + 1] if stmt.level else []
            module = ".".join([*base, *([stmt.module] if stmt.module else [])])
            for alias in stmt.names:
                name = alias.asname or alias.name
                sub = f"{module}.{alias.name}"
                sub_path = SRC_OTTO.parent.joinpath(*sub.split("."))
                if sub_path.is_dir() or sub_path.with_suffix(".py").is_file():
                    found[name] = [sub, None]
                else:
                    found[name] = [module, alias.name]
    return found


def _lazy_tables(path: Path) -> dict[str, list[str]]:
    """name -> [module, attribute] from every ``_LAZY_*`` table, either shape."""
    found: dict[str, list[str]] = {}
    for table in _lazy_table_nodes(ast.parse(path.read_text())):
        for name, entry in ast.literal_eval(table).items():
            found[name] = [entry, name] if isinstance(entry, str) else list(entry)
    return found


def test_the_lazy_package_scan_finds_the_known_packages():
    """A scan that finds nothing would make the drift guard pass vacuously."""
    assert {
        "otto",
        "otto._webassets",
        "otto.check",
        "otto.cli",
        "otto.config",
        "otto.coverage",
        "otto.coverage.fetcher",
        "otto.coverage.merge",
        "otto.coverage.store",
        "otto.creds",
        "otto.docker",
        "otto.env",
        "otto.host",
        "otto.host.survey",
        "otto.host.transfer",
        "otto.inventory",
        "otto.kmodcov",
        "otto.labs",
        "otto.link",
        "otto.logger",
        "otto.models",
        "otto.monitor",
        "otto.project",
        "otto.reservations",
        "otto.suite",
        "otto.testing",
        "otto.tunnel",
    } <= {_package_of(p) for p in _lazy_package_inits()}


def _binds_nothing(tree: ast.Module) -> bool:
    """Whether a module is empty or holds only its docstring."""
    body = tree.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    return not body


def test_every_package_init_is_lazy_or_binds_nothing():
    """One shape for every package: its init exports through a lazy table, or binds nothing.

    The checks below cover every init that carries a ``_LAZY_*`` table, and
    the lazy-init ast-grep rule flags a module-scope import in any init. An
    init that exported through a hand-written ``__getattr__``, or defined its
    code in place, would pass both: this names it.
    """
    inits = sorted(SRC_OTTO.rglob("__init__.py"))
    # Counted a second way, so a scan that walks nothing cannot pass vacuously.
    walked = [root for root, _dirs, files in os.walk(SRC_OTTO) if "__init__.py" in files]
    assert len(inits) == len(walked) >= 25, (len(inits), len(walked))
    divergent = [
        _package_of(path)
        for path in inits
        if not _lazy_table_nodes(tree := ast.parse(path.read_text())) and not _binds_nothing(tree)
    ]
    assert not divergent, divergent


def _imported_from(tree: ast.Module, *modules: str) -> set[str]:
    """The names *tree*'s module-scope ``from .<module> import …`` statements bind."""
    return {
        alias.asname or alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module in modules
        for alias in node.names
    }


def _allowed_eager_names(package: str, tree: ast.Module) -> set[str]:
    """The public names *package*'s lazy init may bind eagerly, and nothing else.

    They are the arms of the lazy-init rule's allow-list
    (.ast-grep/rules/lazy-package-init-stays-lazy.yml, which gives each one's
    reason); no init holds a public definition of its own.
    """
    if package == "otto.logger":
        return {"levels"}
    if package == "otto.config":
        return _imported_from(tree, "env")
    if package == "otto.host.transfer":
        return _imported_from(tree, "base", "registry")
    return set()


@pytest.mark.parametrize("path", _lazy_package_inits(), ids=_package_of)
def test_a_lazy_init_binds_no_public_name_eagerly_beyond_its_allowance(path):
    """A lazy init's public names come from its table, bar the allowed few.

    A public ``def``, class or constant written into an init is code every
    import of the package and of each submodule runs; it belongs in a named
    submodule the table points at.
    """
    tree = ast.parse(path.read_text())
    package = _package_of(path)
    public_eager = {name for name in _eager_bindings(tree.body) if not name.startswith("_")}
    extra = sorted(public_eager - _allowed_eager_names(package, tree))
    assert not extra, f"{package} binds these public names eagerly: {extra}"


@pytest.mark.parametrize("path", _lazy_package_inits(), ids=_package_of)
def test_type_checking_imports_and_the_lazy_table_agree(path):
    """The block type checkers and Sphinx read and the table the runtime reads name the same things.

    Every table entry has its ``TYPE_CHECKING`` import, from the same module
    (and attribute, where the import names one). A ``TYPE_CHECKING`` import
    with no table entry must be annotation-only: not exported in ``__all__``.
    """
    typed = _type_checking_imports(path)
    table = _lazy_tables(path)
    exported = set(importlib.import_module(_package_of(path)).__all__)
    assert set(table) <= set(typed), sorted(set(table) - set(typed))
    assert not (set(typed) - set(table)) & exported, sorted((set(typed) - set(table)) & exported)
    for name, (module, attr) in table.items():
        typed_module, typed_attr = typed[name]
        assert typed_module == module, name
        assert typed_attr in (None, attr), name


def _is_type_checking_block(node: ast.stmt) -> bool:
    return isinstance(node, ast.If) and ast.unparse(node.test) in {
        "TYPE_CHECKING",
        "typing.TYPE_CHECKING",
    }


def _assigned_names(node: ast.stmt) -> list[str]:
    """The plain names an assignment statement binds; none for any other statement."""
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, ast.AnnAssign):
        targets = [node.target]
    else:
        return []
    return [t.id for t in targets if isinstance(t, ast.Name)]


def _eager_bindings(body: list[ast.stmt]) -> set[str]:
    """Every name *body* binds when the module runs, outside ``if TYPE_CHECKING:``.

    Names imported from ``typing`` are left out: an init imports them for its
    ``TYPE_CHECKING`` block and its annotations, never to export them.
    """
    names: set[str] = set()
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            names.update(_assigned_names(node))
        elif isinstance(node, ast.ImportFrom) and node.module not in ("typing", "__future__"):
            names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.If) and not _is_type_checking_block(node):
            names |= _eager_bindings(node.body) | _eager_bindings(node.orelse)
        elif isinstance(node, ast.Try):
            names |= _eager_bindings(node.body) | _eager_bindings(node.orelse)
            for handler in node.handlers:
                names |= _eager_bindings(handler.body)
    return names


@pytest.mark.parametrize("path", _lazy_package_inits(), ids=_package_of)
def test_every_lazy_init_defines_getattr_and_dir(path):
    """The resolver, and the ``dir()`` that shows what it resolves.

    Without ``__dir__``, ``dir(pkg)`` and tab-completion list only what the
    module dict holds, and a lazy init's dict holds none of its lazy names.
    (The resolver's body is ast-grep's: .ast-grep/rules/lazy-getattr-no-write-back.yml.)
    """
    tree = ast.parse(path.read_text())
    defined = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert {"__getattr__", "__dir__"} <= defined, sorted({"__getattr__", "__dir__"} - defined)


@pytest.mark.parametrize("path", _lazy_package_inits(), ids=_package_of)
def test_every_lazy_init_declares_all_as_a_literal_list_of_its_public_names(path):
    """``__all__`` is a literal list naming exactly the public lazy and eager names.

    Literal, so this AST guard, Sphinx and a type checker read the same
    names; a list, per the house style. Exactly: a public lazy name missing
    from it is invisible to a star import, and an extra entry names nothing
    the package binds or resolves.
    """
    tree = ast.parse(path.read_text())
    declared = [node for node in tree.body if "__all__" in _assigned_names(node)]
    assert len(declared) == 1, f"{len(declared)} module-scope __all__ assignments"
    # One literal is the whole list: `__all__ += [...]` or `__all__.extend(...)`
    # would add names this check never reads.
    augmented = [
        ast.unparse(node)
        for node in ast.walk(tree)
        if (isinstance(node, ast.AugAssign) and ast.unparse(node.target) == "__all__")
        or (isinstance(node, ast.Attribute) and ast.unparse(node.value) == "__all__")
    ]
    assert not augmented, f"__all__ is changed after its literal: {augmented}"
    value = declared[0].value
    assert isinstance(value, ast.List), f"__all__ is a {type(value).__name__}, not a list literal"
    assert all(isinstance(elt, ast.Constant) and isinstance(elt.value, str) for elt in value.elts)
    names = [elt.value for elt in value.elts if isinstance(elt, ast.Constant)]
    assert len(names) == len(set(names)), sorted(n for n in set(names) if names.count(n) > 1)
    public_lazy = {name for name in _lazy_tables(path) if not name.startswith("_")}
    public_eager = {name for name in _eager_bindings(tree.body) if not name.startswith("_")}
    expected = public_lazy | public_eager
    assert set(names) == expected, {
        "missing": sorted(expected - set(names)),
        "extra": sorted(set(names) - expected),
    }


# Lazy package inits change the order in which modules first run. Each line is
# one fresh interpreter; a cycle shows up as an ImportError on a partially
# initialised module.
IMPORT_ORDERS = [
    "import otto",
    "import otto.host",
    "import otto.models",
    "import otto.monitor",
    "from otto.host import UnixHost",
    "from otto.host import LocalHost",
    "from otto.models import HostSpec",
    "from otto.monitor import MetricCollector",
    "import otto.models.host; import otto.host",
    "import otto.models.host; from otto.host import UnixHost",
    "import otto.models.settings; from otto.models import UnixHostSpec",
    "import otto.models.monitor; from otto.monitor import build_monitor_collector",
    "import otto.monitor.collector; from otto.models import MonitorExport",
    "import otto.host.factory; from otto.models import HostSpec",
    "import otto.host.os_profile; from otto.host import EmbeddedHost",
    "import otto.host.unix_host; import otto.models.host",
    "import otto.coverage",
    "from otto.coverage import CoverageReporter",
    "import otto.coverage.instrumentation; from otto.coverage import collect_coverage",
    "import otto.config.coverage_settings; from otto.coverage import CoverageConfigError",
    "import otto.docker",
    "from otto.docker import compose_up",
    "import otto.docker.compose; from otto.docker import build_images",
    "from otto.docker import deploy; from otto.docker import build_images",
    "import otto.inventory",
    "from otto.inventory import build_inventory",
    "import otto.inventory.netbox; from otto.inventory import resolve_host_entry",
    "import otto.models.inventory; from otto.inventory import JsonInventory",
    "import otto.reservations",
    "from otto.reservations import build_reservation_gate",
    "import otto.reservations.check; from otto.reservations import ReservationBackendBase",
    "import otto.reservations.json_backend; from otto.reservations import build_backend",
    "import otto.link",
    "from otto.link import IMPAIRERS",
    "import otto.models.host; from otto.link import impair_link",
    "import otto.link.manage; from otto.link import NetEmImpairer",
    "import otto.link.check; from otto.link import repair_all",
    "import otto.link.netem; from otto.link import check_link",
    "from otto.link import check_link; import otto.models.host",
    "from otto.cli.invoke import ensure_lab_context",
    "import otto.cli.registry; from otto.cli import app",
    "from otto.cli import app; import otto.cli.registry",
    "import otto.project.actions; from otto.project import install",
    "import otto.labs.composite; from otto.labs import host_summaries",
    "import otto.labs.sources; from otto.labs import JsonFileLabRepository",
    "import otto.env.preflight; from otto.env import create_env",
    "import otto.kmodcov.library; from otto.kmodcov import INTERFACE",
    "import otto.tunnel.manage; from otto.tunnel import discover_tunnels",
    "import otto.check.verdict; from otto.check import probe_fingerprint",
    "import otto.creds.registry; from otto.creds import JsonCredsStore",
    "import otto.suite.run; from otto.suite import OttoFixturesPlugin",
    # The built-in command table imports the registry only when it runs: a
    # process whose first import is the table still gets every built-in.
    (
        "import otto.cli.builtin_commands; from otto.cli.registry import CLI_COMMANDS; "
        "assert 'init' in CLI_COMMANDS"
    ),
]


@pytest.mark.parametrize("code", IMPORT_ORDERS)
def test_imports_succeed_in_any_order(code):
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr


def test_plugin_subclasses_a_lazily_exported_builtin_host():
    from otto.host import UnixHost
    from otto.host.os_profile import HOST_CLASSES
    from otto.host.unix_host import UnixHost as Real

    assert UnixHost is Real
    assert HOST_CLASSES.get("unix") is Real

    class Mine(UnixHost):
        pass

    assert issubclass(Mine, Real)


# ── The lazy-export leak guard (tests/_fixtures/_lazy_exports.py) ─────────────
#
# The root conftest's teardown fails a test that leaves a lazily exported name
# cached in its package's __dict__. These pin the guard's reach and its red.


def test_the_leak_guard_checks_every_package_with_a_lazy_attrs_table():
    """A guard whose package scan came back short would pass vacuously."""
    from tests._fixtures._lazy_exports import lazy_package_names

    declares = re.compile(r"^_LAZY_ATTRS\b", re.MULTILINE)
    expected = {
        _package_of(path) for path in _lazy_package_inits() if declares.search(path.read_text())
    }
    assert set(lazy_package_names()) == expected
    assert {"otto.session", *PACKAGES} <= expected


def test_a_monkeypatch_on_the_lazy_package_is_caught_and_evicted():
    """The leak made exactly as monkeypatch makes it: by the UNDO of a package patch."""
    import otto.session
    from tests._fixtures._lazy_exports import (
        LeakedLazyExportError,
        leaked_lazy_exports,
        raise_on_leaked_lazy_exports,
    )

    assert leaked_lazy_exports() == []
    patcher = pytest.MonkeyPatch()
    patcher.setattr("otto.session.build_lab", lambda repos, labs: None)
    patcher.undo()
    try:
        assert leaked_lazy_exports() == ["otto.session.build_lab"]
        with pytest.raises(
            LeakedLazyExportError,
            match=re.escape("otto.session.build_lab -> patch otto.session.lab.build_lab"),
        ):
            raise_on_leaked_lazy_exports("the-leaking-test")
        assert "build_lab" not in vars(otto.session)  # evicted, so it cannot cascade
    finally:
        vars(otto.session).pop("build_lab", None)


def test_a_monkeypatch_on_the_defining_module_leaves_nothing_behind():
    """The fix the guard's message asks for is itself clean."""
    import otto.session
    from tests._fixtures._lazy_exports import leaked_lazy_exports

    patcher = pytest.MonkeyPatch()
    patcher.setattr("otto.session.lab.build_lab", lambda repos, labs: None)
    try:
        assert otto.session.build_lab(None, None) is None  # the package resolves the patch
    finally:
        patcher.undo()
    assert leaked_lazy_exports() == []
