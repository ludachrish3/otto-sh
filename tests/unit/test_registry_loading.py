"""The test-file loading phase, and the refusal of registrations made in it."""

import sys
import types

import pytest

from otto import registry as reg


def test_user_registration_during_test_load_is_refused():
    r: reg.Registry[int] = reg.Registry("widget", register_hint="x")
    with reg.loading_test_files(), pytest.raises(reg.RegistrationRefused, match="init module"):
        r.register("w", 1, origin="test_widgets")
    assert "w" not in r


def test_otto_module_registration_during_test_load_is_allowed():
    r: reg.Registry[int] = reg.Registry("widget", register_hint="x")
    with reg.loading_test_files():
        r.register("w", 1, origin="otto.host.llext_kind")
    assert "w" in r


def test_the_refusal_names_test_files_and_conftests_and_points_at_an_init_module():
    r: reg.Registry[int] = reg.Registry("widget", register_hint="x")
    with reg.loading_test_files(), pytest.raises(reg.RegistrationRefused) as err:
        r.register("w", 1, origin="test_widgets")
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
    home: reg.Registry[int] = reg.Registry("widget", register_hint="x")
    other: reg.Registry[int] = reg.Registry("gadget", register_hint="x")
    monkeypatch.setitem(sys.modules, "_refhome", types.SimpleNamespace(OTHER=other))
    (tmp_path / "late_widget_mod.py").write_text(
        "import _refhome\n_refhome.OTHER.register('companion', 2)\nWIDGET = 1\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "late_widget_mod", raising=False)
    home.register("w", reg.Ref("late_widget_mod:WIDGET"), origin="repo_init")
    with reg.loading_test_files():
        assert home.get("w") == 1
        assert reg.is_loading_test_files()
        with pytest.raises(reg.RegistrationRefused):
            other.register("late", 3, origin="test_widgets")
    assert other.origin("companion") == "late_widget_mod"


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


def test_every_otto_registry_refuses_during_test_load():
    """Enumeration guard: every registry otto builds, including one added later, refuses.

    Scoped to registries an ``otto`` module constructed: a test-built registry is
    not the product's seam.
    """
    from otto.params import OPTIONS

    assert _import_every_module_that_builds_a_registry(), "the source scan found no registries"
    registries = _otto_registries()
    assert OPTIONS in registries, "the scope filter lost the options registry"
    for r in registries:
        with reg.loading_test_files(), pytest.raises(reg.RegistrationRefused):
            r.register("__refusal_probe__", object(), origin="test_probe")
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

    provider.__module__ = "test_providers"
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
        "test_kind_probe",
        "exec",
    )
    with reg.loading_test_files(), pytest.raises(reg.RegistrationRefused):
        exec(code, {"__name__": "test_kind_probe"})  # noqa: S102
    registry = module.PRODUCT_KINDS if "product" in path else module.DEV_TOOL_KINDS
    assert "__probe_kind__" not in registry
