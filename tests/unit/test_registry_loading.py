"""Registry loaders, the suite-loading phase, and the refusal of non-suite registrations."""

import sys
import types

import pytest

from otto import registry as reg


def _install_loader(monkeypatch, fn):
    mod = types.ModuleType("_loader_mod")
    mod.load = fn
    monkeypatch.setitem(sys.modules, "_loader_mod", mod)
    return "_loader_mod:load"


_READS = {
    "items": lambda r: r.items(),
    "names": lambda r: r.names(),
    "get": lambda r: r.get("seed"),
    "origin": lambda r: r.origin("seed"),
    "unregister": lambda r: r.unregister("seed"),
    "__contains__": lambda r: "seed" in r,
    "__len__": len,
}


@pytest.mark.parametrize("read", list(_READS), ids=list(_READS))
def test_every_read_calls_the_loader_once_and_it_is_never_reentered(monkeypatch, read):
    """Each of the seven reads runs the loader, through the real ``loader=`` kwarg.

    The loader itself reads the registry, as ``register_suite_class`` does;
    that inner read must not recurse into the loader.
    """
    calls = []
    loader = _install_loader(monkeypatch, lambda: (calls.append("load"), r.names()))
    r: reg.Registry[int] = reg.Registry("thing", register_hint="x", loader=loader)
    with reg.suspend_loaders():
        r.register("seed", 0)
    _READS[read](r)
    assert calls == ["load"]


def test_suspend_loaders_reads_without_loading(monkeypatch):
    calls = []
    loader = _install_loader(monkeypatch, lambda: calls.append(1))
    r: reg.Registry[int] = reg.Registry("thing", register_hint="x", loader=loader)
    with reg.suspend_loaders():
        r.names()
        r.items()
    assert calls == []
    r.names()
    assert calls == [1], "loading did not resume after suspend_loaders() exited"


def test_loading_resumes_after_suspend_loaders_exits_by_exception(monkeypatch):
    calls = []
    loader = _install_loader(monkeypatch, lambda: calls.append(1))
    r: reg.Registry[int] = reg.Registry("thing", register_hint="x", loader=loader)

    def _read_then_raise() -> None:
        with reg.suspend_loaders():
            r.names()
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        _read_then_raise()
    assert calls == []
    r.names()
    assert calls == [1], "an exception left the loaders suspended"


def test_user_registration_during_test_load_is_refused():
    r: reg.Registry[int] = reg.Registry("widget", register_hint="x")
    with reg.loading_test_files(), pytest.raises(reg.RegistrationRefused, match="init module"):
        r.register("w", 1, origin="_otto_suite_test_widgets")
    assert "w" not in r


def test_otto_module_registration_during_test_load_is_allowed():
    r: reg.Registry[int] = reg.Registry("widget", register_hint="x")
    with reg.loading_test_files():
        r.register("w", 1, origin="otto.host.llext_kind")
    assert "w" in r


def test_an_opted_in_registry_accepts_test_file_registrations():
    r: reg.Registry[int] = reg.Registry("suite", register_hint="x", accepts_test_files=True)
    with reg.loading_test_files():
        r.register("TestX", 1, origin="_otto_suite_test_x")
    assert "TestX" in r


def _import_every_module_that_builds_a_registry() -> list[str]:
    """AST-scan src/otto for `Registry(`/`KindRegistry(` calls and import each such module.

    Scanning the source, not what happens to be imported, is what makes the
    guard cover a registry in a module nothing on the CLI path imports yet.
    """
    import ast
    import importlib

    from tests._fixtures.paths import PROJECT_ROOT

    src = PROJECT_ROOT / "src"
    imported = []
    for path in sorted((src / "otto").rglob("*.py")):
        tree = ast.parse(path.read_text())
        builds = any(
            isinstance(n, ast.Call)
            and getattr(n.func, "id", getattr(n.func, "attr", None)) in {"Registry", "KindRegistry"}
            for n in ast.walk(tree)
        )
        if builds:
            parts = path.relative_to(src).with_suffix("").parts
            name = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
            importlib.import_module(name)
            imported.append(name)
    return imported


def _is_otto_module(name: str) -> bool:
    return name == "otto" or name.startswith("otto.")


def _is_test_module(name: str) -> bool:
    last = name.rpartition(".")[2]
    return name.startswith("tests.") or last.startswith("test_") or last == "conftest"


def _otto_registries() -> "list[reg.Registry]":
    """Every live registry constructed by an ``otto`` module (never one a test built).

    Exclusion must be LOUD: a registry whose recorded constructor is neither an
    otto module nor a test module (a subscripted ``Registry[X](...)`` records
    ``typing``, whose ``__call__`` sits between) would otherwise drop out of the
    guard silently, so it fails here instead.
    """
    live = reg.Registry.instances()
    unattributed = [
        (r._kind, r.defined_in)
        for r in live
        if not _is_otto_module(r.defined_in) and not _is_test_module(r.defined_in)
    ]
    assert not unattributed, f"registries with an unrecognised constructing module: {unattributed}"
    return [r for r in live if _is_otto_module(r.defined_in)]


def test_every_otto_registry_but_suites_refuses_during_test_load():
    """Enumeration guard: a registry added later refuses by default, or this test names it.

    Scoped to registries an ``otto`` module constructed: a test-built registry is
    not the product's seam. Probed under ``suspend_loaders()`` so the check never
    runs the suites loader, which would import a SUT's test files.
    """
    from otto.suite.register import SUITES

    assert _import_every_module_that_builds_a_registry(), "the source scan found no registries"
    registries = _otto_registries()
    assert SUITES in registries, "the scope filter lost the suites registry"
    for r in registries:
        with reg.suspend_loaders():
            if r is SUITES:
                with reg.loading_test_files():
                    r.register("__refusal_probe__", object(), origin="_otto_suite_probe")
                assert "__refusal_probe__" in r, "SUITES must accept a test file's suite"
                r.unregister("__refusal_probe__")
                continue
            with reg.loading_test_files(), pytest.raises(reg.RegistrationRefused):
                r.register("__refusal_probe__", object(), origin="_otto_suite_probe")
            assert "__refusal_probe__" not in r, r


def test_a_test_built_registry_is_outside_the_guard_scope():
    """The guard's filter is by constructing module, so a test's own registry is excluded."""
    r: reg.Registry[int] = reg.Registry("scratch", register_hint="x")
    assert r.defined_in == __name__
    assert r not in _otto_registries()


def test_a_registry_from_an_unrecognised_module_fails_the_guard_loudly():
    """A registry attributed to neither otto nor a test must not drop out silently."""
    r: reg.Registry[int] = reg.Registry("scratch", register_hint="x")
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

    provider.__module__ = "_otto_suite_test_providers"
    module = importlib.import_module(path)
    providers = module._PRODUCT_PROVIDERS if "product" in path else module._DEV_TOOL_PROVIDERS
    before = list(providers)
    with reg.loading_test_files(), pytest.raises(reg.RegistrationRefused):
        register(provider)
    assert providers == before, "a refused provider still reached the provider list"


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
        "_otto_suite_kind_probe",
        "exec",
    )
    with reg.loading_test_files(), pytest.raises(reg.RegistrationRefused):
        exec(code, {"__name__": "_otto_suite_kind_probe"})  # noqa: S102
    registry = module.PRODUCT_KINDS if "product" in path else module.DEV_TOOL_KINDS
    assert "__probe_kind__" not in registry
