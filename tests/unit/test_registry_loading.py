"""The test-file loading phase, the refusal of registrations made in it, and table discovery."""

import ast
import dataclasses
import pathlib
import sys
import types

import pytest

from otto import registry as reg

ENGINE_TYPES = {"Registry", "BackendRegistry", "Subscription", "RegistryView"}
"""Constructor names the discovery scan looks for."""


@dataclasses.dataclass(frozen=True)
class Widget:
    value: object


@dataclasses.dataclass(frozen=True)
class LazyWidget:
    value: "object | reg.Ref"


def _widgets(kind: str = "widget") -> "reg.Registry[Widget]":
    return reg.Registry(kind, entry=Widget, register_hint="x")


def _from_module(module: str, fn, *args, **kwargs):
    """Call *fn* from a frame whose module is *module* (attribution is by frame)."""
    code = compile("result = fn(*args, **kwargs)", f"<{module}>", "exec")
    scope = {"__name__": module, "fn": fn, "args": args, "kwargs": kwargs}
    exec(code, scope)  # noqa: S102 — a synthetic caller module, the attribution under test
    return scope["result"]


def test_user_registration_during_test_load_is_refused():
    r = _widgets()
    with reg.loading_test_files(), pytest.raises(reg.RegistrationRefused, match="init module"):
        _from_module("test_widgets", r.register, "w", Widget(1))
    assert "w" not in r


def test_otto_module_registration_during_test_load_is_allowed():
    r = _widgets()
    with reg.loading_test_files():
        _from_module("otto.host.embedded_kind", r.register, "w", Widget(1))
    assert "w" in r


def test_the_refusal_names_test_files_and_conftests_and_points_at_an_init_module():
    r = _widgets()
    with reg.loading_test_files(), pytest.raises(reg.RegistrationRefused) as err:
        _from_module("test_widgets", r.register, "w", Widget(1))
    text = str(err.value)
    assert "'test_widgets'" in text
    assert "init module listed in .otto/settings.toml" in text
    assert "test file or conftest" in text
    assert "suites" not in text


def test_a_ref_resolved_while_test_files_load_may_register_on_import(tmp_path, monkeypatch):
    """A registered ``Ref``'s module is the init module's code, wherever it first resolves.

    Only an init module (or otto) can have registered the ``Ref``: the same
    registration from a test file is refused. So when the first ``get`` lands
    inside a pytest session, what the target's module registers at import is
    not refused, and the phase marker is in force again once the import is
    done.
    """
    home: reg.Registry[LazyWidget] = reg.Registry(
        "widget", entry=LazyWidget, register_hint="x", check_resolved=lambda n, e: None
    )
    other = _widgets("gadget")
    monkeypatch.setitem(sys.modules, "_refhome", types.SimpleNamespace(OTHER=other, Widget=Widget))
    (tmp_path / "late_widget_mod.py").write_text(
        "import _refhome\n_refhome.OTHER.register('companion', _refhome.Widget(2))\nWIDGET = 1\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "late_widget_mod", raising=False)
    _from_module("repo_init", home.register, "w", LazyWidget(reg.Ref("late_widget_mod:WIDGET")))
    with reg.loading_test_files():
        assert home.get("w").value == 1
        assert reg.is_loading_test_files()
        with pytest.raises(reg.RegistrationRefused):
            _from_module("test_widgets", other.register, "late", Widget(3))
    assert other.origin("companion") == "late_widget_mod"


# ── discovery: the source scan and the engine's own list ───────────────────


@dataclasses.dataclass(frozen=True)
class Construction:
    """One engine-table construction the source scan found."""

    module: str
    bound: str | None
    """The module attribute it is assigned to, or ``None``."""
    kind: str | None
    """The kind literal (the first positional argument), when it is a string."""
    module_level: bool


def _scan_constructions(root: pathlib.Path, package: str) -> "list[Construction]":
    """AST-scan *root* (the directory of *package*) for engine-table constructions."""
    rows = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        parts = (package, *rel.with_suffix("").parts)
        module = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
        if module == "otto.registry":
            continue  # the engine's owned inner tables
        tree = ast.parse(path.read_text())
        top_level = {}
        for stmt in tree.body:
            value = getattr(stmt, "value", None)
            if isinstance(value, ast.Call):
                target = (
                    stmt.target
                    if isinstance(stmt, ast.AnnAssign)
                    else stmt.targets[0]
                    if isinstance(stmt, ast.Assign)
                    else None
                )
                top_level[id(value)] = target.id if isinstance(target, ast.Name) else None
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = getattr(node.func, "id", getattr(node.func, "attr", None))
            if func not in ENGINE_TYPES:
                continue
            first = node.args[0] if node.args else None
            kind = first.value if isinstance(first, ast.Constant) else None
            rows.append(
                Construction(
                    module=module,
                    bound=top_level.get(id(node)),
                    kind=kind if isinstance(kind, str) else None,
                    module_level=id(node) in top_level,
                )
            )
    return rows


def _import_every_module_that_builds_a_registry() -> "list[Construction]":
    """AST-scan src/otto for engine-table constructions and import each such module.

    Scanning the source, not what happens to be imported, is what makes the
    guards cover a table in a module nothing on the CLI path imports yet.
    """
    import importlib

    from tests._fixtures.paths import PROJECT_ROOT

    rows = _scan_constructions(PROJECT_ROOT / "src" / "otto", "otto")
    for module in sorted({row.module for row in rows}):
        importlib.import_module(module)
    return rows


def _is_otto_module(name: str) -> bool:
    return name == "otto" or name.startswith("otto.")


def _is_test_module(name: str) -> bool:
    last = name.rpartition(".")[2]
    return name.startswith("tests.") or last.startswith("test_") or last == "conftest"


def _otto_registries() -> list:
    """Every live engine table constructed by an ``otto`` module (never one a test built).

    Exclusion must be LOUD: a table whose recorded constructor is neither an
    otto module nor a test module (a subscripted ``Registry[X](...)`` records
    ``typing``, whose ``__call__`` sits between) would otherwise drop out of the
    guard silently, so it fails here instead.
    """
    live = reg.instances()
    unattributed = [
        (r.kind, r.defined_in)
        for r in live
        if not _is_otto_module(r.defined_in) and not _is_test_module(r.defined_in)
    ]
    assert not unattributed, f"registries with an unrecognised constructing module: {unattributed}"
    return [r for r in live if _is_otto_module(r.defined_in)]


def test_every_otto_registry_refuses_during_test_load():
    """Enumeration guard: every writable table otto builds, including one added later, refuses.

    The refusal is the engine's step before the record check, so a probe of
    the wrong type is refused like any other write. A ``RegistryView`` has no
    write method and is skipped; every other table is probed, and the probed
    set must be every non-view table, so a table skipped here is red. A table
    lost from discovery is caught by the conformance coverage test
    (``test_every_registry_has_a_case``).
    """
    from otto.params import OPTIONS

    assert _import_every_module_that_builds_a_registry(), "the source scan found no registries"
    registries = _otto_registries()
    assert OPTIONS in registries, "the scope filter lost the options registry"
    probed = []
    for r in registries:
        if isinstance(r, reg.RegistryView):
            continue
        revision = r.revision
        if isinstance(r, reg.Subscription):
            write, args = r.subscribe, (object(),)
        else:
            write, args = r.register, ("__refusal_probe__", object())
        with reg.loading_test_files(), pytest.raises(reg.RegistrationRefused):
            _from_module("test_probe", write, *args)
        assert r.revision == revision, f"{r.kind}: the refused write changed the table"
        if not isinstance(r, reg.Subscription):
            assert "__refusal_probe__" not in r, r.kind
        probed.append(r)
    writable = [r for r in registries if not isinstance(r, reg.RegistryView)]
    assert sorted((r.defined_in, r.kind) for r in probed) == sorted(
        (r.defined_in, r.kind) for r in writable
    )
    assert {type(r) for r in probed} == {reg.Registry, reg.BackendRegistry, reg.Subscription}


def test_every_engine_table_is_built_at_module_level():
    """A construction inside a function or class body would escape discovery by binding."""
    rows = _import_every_module_that_builds_a_registry()
    inner = [(row.module, row.kind) for row in rows if not row.module_level]
    assert inner == []


def test_the_module_level_scan_flags_a_construction_inside_a_function(tmp_path):
    """The scan's own red: a scratch module building a table in a ``def`` is flagged."""
    pkg = tmp_path / "scratchpkg"
    pkg.mkdir()
    (pkg / "mod.py").write_text(
        "from otto.registry import Registry\n"
        "TOP = Registry('top', register_hint='x')\n"
        "def build():\n"
        "    return Registry('inner', register_hint='x')\n"
    )
    rows = _scan_constructions(pkg, "scratchpkg")
    assert {(row.kind, row.module_level, row.bound) for row in rows} == {
        ("top", True, "TOP"),
        ("inner", False, None),
    }


def test_the_source_scan_equals_instances():
    """Every scanned construction is a live table, and every live otto table was scanned."""
    rows = _import_every_module_that_builds_a_registry()
    scanned = {(row.module, row.kind) for row in rows}
    live = {(t.defined_in, t.kind) for t in _otto_registries()}
    assert scanned == live


def test_no_two_otto_tables_share_a_module_and_kind():
    """``(defined_in, kind)`` is every guard's key, so two tables sharing one collapse silently."""
    _import_every_module_that_builds_a_registry()
    keys = [(t.defined_in, t.kind) for t in _otto_registries()]
    assert sorted(k for k in set(keys) if keys.count(k) > 1) == []


def test_a_test_built_registry_is_outside_the_guard_scope():
    """The guard's filter is by constructing module, so a test's own registry is excluded."""
    r = _widgets("scratch")
    assert r.defined_in == __name__
    assert r not in _otto_registries()


def test_a_registry_from_an_unrecognised_module_fails_the_guard_loudly():
    """A registry attributed to neither otto nor a test must not drop out silently."""
    r = _widgets("scratch")
    r.defined_in = "typing"  # what a subscripted `Registry[X](...)` would record
    try:
        with pytest.raises(AssertionError, match="unrecognised constructing module"):
            _otto_registries()
    finally:
        r.defined_in = __name__  # never leave a poisoned instance for a later guard run


@pytest.mark.parametrize(
    ("path", "func"),
    [
        ("otto.host.product", "register_product_provider"),
        ("otto.host.dev_tool", "register_dev_tool_provider"),
    ],
)
def test_providers_refuse_during_test_load(path, func):
    import importlib

    register = getattr(importlib.import_module(path), func)

    def provider(host):
        return []

    # An otto-looking __module__: the refusal is keyed on the module that CALLED
    # the wrapper (this test file), never on where the provider claims to live.
    provider.__module__ = "otto.fake_providers"
    module = importlib.import_module(path)
    providers = module.PRODUCT_PROVIDERS if "product" in path else module.DEV_TOOL_PROVIDERS
    before = providers.items()
    with reg.loading_test_files(), pytest.raises(reg.RegistrationRefused):
        register(provider)
    assert providers.items() == before, "a refused provider still reached the subscription"


@pytest.mark.parametrize(
    ("path", "func"),
    [
        ("otto.host.product", "register_product_kind"),
        ("otto.host.dev_tool", "register_dev_tool_kind"),
    ],
)
def test_kind_wrappers_attribute_the_caller(path, func):
    """These two wrappers passed no origin, so a test file's call looked like otto's own."""
    import importlib

    module = importlib.import_module(path)
    code = compile(
        f"from {path} import {func}\n{func}('__probe_kind__', lambda entry, host: None)\n",
        "test_kind_probe",
        "exec",
    )
    with reg.loading_test_files(), pytest.raises(reg.RegistrationRefused):
        exec(code, {"__name__": "test_kind_probe"})  # noqa: S102
    registry = module.PRODUCT_KINDS if "product" in path else module.DEV_TOOL_KINDS
    assert "__probe_kind__" not in registry
