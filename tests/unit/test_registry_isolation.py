"""Regression for the registry / ``sys.modules`` isolation gap.

The "other half" of issue #108: ``_isolate_registries`` (tests/conftest.py)
snapshots each global otto ``Registry`` and drops entries a test added on
teardown. But an extension module listed in a repo's ``init`` (e.g.
``custom_hosts``, which calls ``register_command_frame`` at import) registers as
an **import side effect** — so dropping the entry while leaving the module in
``sys.modules`` desyncs registry state from module state: a later
``importlib.import_module`` is a no-op and never re-registers. ``_restore_registries``
must therefore evict the origin module of every dropped entry — but only origins
the test itself imported, never a module already loaded before it (a pytest test
module registering a local class, or core ``otto``).

This bit only single-process (``-n0``): under ``-n auto`` the importer and the
victim scatter across xdist workers.
"""

import sys
import types

from otto.registry import Registry
from tests.conftest import (
    _host_spec_snapshot,
    _loaded_registries,
    _provider_snapshot,
    _restore_host_specs,
    _restore_provider_snapshot,
    _restore_registries,
)


def _snapshot(reg: Registry) -> dict[str, tuple[object, str]]:
    return {name: (reg.get(name), reg.origin(name)) for name in reg.names()}


def test_dropped_entry_origin_module_is_evicted() -> None:
    reg = Registry("thing", register_hint="register_thing()")
    snapshot = _snapshot(reg)  # pristine baseline

    # An extension module the test itself imports (absent before the test),
    # registering as an import side effect.
    before = frozenset(sys.modules)
    sys.modules["fake_ext_isolation_regression"] = types.ModuleType("fake_ext_isolation_regression")
    reg.register("added", object(), origin="fake_ext_isolation_regression")

    _restore_registries([(reg, snapshot)], before)

    assert "added" not in reg.names()  # entry the test added is dropped
    # …and its origin module is evicted, so a re-import re-runs the registration
    assert "fake_ext_isolation_regression" not in sys.modules


def test_origin_module_loaded_before_the_test_is_not_evicted() -> None:
    """A module already imported before the test (e.g. the running test file,
    which registers local suite classes via ``register_suite_class``) must be
    left in ``sys.modules`` — evicting it breaks ``inspect.getfile`` for every
    later registration in that file.
    """
    reg = Registry("thing", register_hint="register_thing()")
    snapshot = _snapshot(reg)

    # Module present BEFORE the test (stand-in for a collected pytest test file).
    sys.modules["fake_preloaded_test_module"] = types.ModuleType("fake_preloaded_test_module")
    before = frozenset(sys.modules)
    try:
        reg.register("added", object(), origin="fake_preloaded_test_module")

        _restore_registries([(reg, snapshot)], before)

        assert "added" not in reg.names()  # entry still dropped
        assert "fake_preloaded_test_module" in sys.modules  # but the module survives
    finally:
        sys.modules.pop("fake_preloaded_test_module", None)


def test_core_otto_origin_module_is_not_evicted() -> None:
    reg = Registry("thing", register_hint="register_thing()")
    snapshot = _snapshot(reg)
    assert "otto.registry" in sys.modules  # imported at module top

    before = frozenset(sys.modules) - {"otto.registry"}  # pretend it was imported now
    reg.register("added", object(), origin="otto.registry")

    _restore_registries([(reg, snapshot)], before)

    assert "added" not in reg.names()
    assert "otto.registry" in sys.modules  # core otto is never evicted


def test_snapshot_entries_are_preserved() -> None:
    reg = Registry("thing", register_hint="register_thing()")
    reg.register("builtin", "keep", origin="builtin_origin")
    snapshot = _snapshot(reg)

    before = frozenset(sys.modules)
    sys.modules["fake_ext_isolation_regression2"] = types.ModuleType(
        "fake_ext_isolation_regression2"
    )
    reg.register("added", "drop", origin="fake_ext_isolation_regression2")

    _restore_registries([(reg, snapshot)], before)

    assert reg.names() == ["builtin"]  # added dropped, snapshot restored
    assert reg.get("builtin") == "keep"
    assert "fake_ext_isolation_regression2" not in sys.modules


# ── the PROVIDER seams: plain lists, which no Registry scan can find ─────────
#
# ``register_product_provider`` / ``register_dev_tool_provider`` append to two
# module-global lists that nothing ever unregisters, and every ``bootstrap()``
# importing a provider-registering init module adds to them for the rest of the
# process. ``_isolate_registries`` cannot help: it discovers state by scanning
# for ``otto.registry.Registry`` instances, and these are ``list``.
# ``_restore_provider_registries`` (root conftest) covers them; these tests
# INJECT the hostile condition into its restore rather than hoping to observe a
# leak from a neighbouring test — a cross-test observation lands in a different
# xdist worker as often as not, and would pass by luck exactly when the guard
# is broken.


def _providers():
    from otto.host import dev_tool as dev_tool_mod
    from otto.host import product as product_mod

    return product_mod._PRODUCT_PROVIDERS, dev_tool_mod._DEV_TOOL_PROVIDERS


def test_provider_registered_during_a_test_is_dropped_by_the_restore() -> None:
    """The injection: register into both seams, restore, expect the snapshot back."""
    products, dev_tools = _providers()
    before = (list(products), list(dev_tools))

    snapshot = _provider_snapshot()
    products.append((lambda host: [], "fake-repo"))
    dev_tools.append((lambda host: [], "fake-repo"))
    # The hostile condition is real, on both seams, before anything restores.
    assert products != before[0]
    assert dev_tools != before[1]

    _restore_provider_snapshot(snapshot)

    assert list(products) == before[0]
    assert list(dev_tools) == before[1]


def test_providers_present_before_the_test_survive_the_restore() -> None:
    """Restore, not reset: a provider that predates the test must still be there.

    A module- or session-scoped fixture that registers a provider sets up
    BEFORE any function-scoped snapshot, so its entry is inside the snapshot and
    has to come back out of it. A guard that cleared to empty instead would kill
    that fixture with its first test — the same distinction ``_restore_bootstrap_state``
    draws against the old ``bootstrap._reset()`` teardown.
    """
    products, _dev_tools = _providers()
    pre_existing = (lambda host: [], "pre-existing-repo")
    products.append(pre_existing)
    try:
        snapshot = _provider_snapshot()
        products.append((lambda host: [], "added-by-the-test"))

        _restore_provider_snapshot(snapshot)

        assert products[-1] is pre_existing
    finally:
        if pre_existing in products:
            products.remove(pre_existing)


def test_a_seam_the_test_itself_imported_is_restored_to_empty() -> None:
    """The hole that let the first-party instructions escape ``_isolate_registries``.

    A snapshot taken while the module is UNLOADED has nothing to record, and a
    guard that then skipped the restore would let everything the test registered
    survive — which is exactly how that guard misses a registry first imported
    mid-test. Both provider lists are ``[]`` at import, so "not loaded when we
    looked" has one correct restore: empty.
    """
    products, _dev_tools = _providers()
    real = sys.modules["otto.host.product"]
    try:
        del sys.modules["otto.host.product"]
        snapshot = _provider_snapshot()
        assert snapshot[0] == ("otto.host.product", "_PRODUCT_PROVIDERS", None)
    finally:
        sys.modules["otto.host.product"] = real

    products.append((lambda host: [], "registered-after-the-import"))

    _restore_provider_snapshot(snapshot)

    assert products == []


def test_the_restore_mutates_in_place_so_from_import_readers_see_it() -> None:
    """``otto.bootstrap`` binds these lists by ``from … import`` at call time.

    Rebinding the module attribute would restore what a fresh attribute lookup
    reads and leave that reader holding the leaked list, so the guard's fix
    would be invisible to the D2 check it most needs to protect.
    """
    products, _dev_tools = _providers()
    bound = products  # the shape `from otto.host.product import _PRODUCT_PROVIDERS` makes
    before = list(products)

    snapshot = _provider_snapshot()
    products.append((lambda host: [], "fake-repo"))

    _restore_provider_snapshot(snapshot)

    assert bound is products  # same object: the restore never rebound the attribute
    assert bound == before


# ── the hole: a registry whose module was imported DURING the test ────────


def _late_module(name: str, reg: Registry) -> types.ModuleType:
    """An ``otto.*`` module carrying *reg*, as if imported mid-test."""
    mod = types.ModuleType(name)
    mod.LATE = reg
    sys.modules[name] = mod
    return mod


def test_a_registry_that_first_appeared_mid_test_is_not_left_polluted() -> None:
    """An entry a test adds must not survive just because the registry is new.

    ``_isolate_registries`` snapshots what is REACHABLE at setup, so a registry
    living in an ``otto.*`` module the test itself imports has no snapshot —
    and the restore, iterating snapshots alone, never looked at it. Everything
    the test registered there leaked into the next test with nothing to notice.
    """
    reg = Registry("late", register_hint="register_late()")
    reg.register("from_import", object(), origin="otto._fake_late_pkg")
    _late_module("otto._fake_late_pkg", reg)
    before = frozenset(sys.modules) - {"otto._fake_late_pkg"}
    try:
        reg.register("from_test", object(), origin="tests.unit.test_registry_isolation")

        _restore_registries([], before)

        assert list(reg.names()) == ["from_import"]
    finally:
        sys.modules.pop("otto._fake_late_pkg", None)


def test_a_late_registrys_own_import_time_entries_survive() -> None:
    """otto's own import-time registrations are the new baseline — never dropped.

    Once ``otto.project.instructions`` (say) is imported, its six first-party
    entries ARE the process's state: the module stays in ``sys.modules``, so a
    re-import is a no-op and anything dropped here could never be re-registered.
    Clearing them would leave otto missing its own defaults for every later
    test — a cure far worse than the leak.
    """
    reg = Registry("late", register_hint="register_late()")
    for name in ("install", "uninstall"):
        reg.register(name, object(), origin="otto._fake_late_defaults")
    _late_module("otto._fake_late_defaults", reg)
    before = frozenset(sys.modules) - {"otto._fake_late_defaults"}
    try:
        _restore_registries([], before)

        assert sorted(reg.names()) == ["install", "uninstall"]
    finally:
        sys.modules.pop("otto._fake_late_defaults", None)


def test_a_late_registrys_extension_origin_is_evicted_so_reimport_re_registers() -> None:
    """A non-otto origin the test imported is dropped AND evicted, as elsewhere.

    Same rule the snapshotted path already follows: the entry goes, and its
    module leaves ``sys.modules`` so the next ``import_module`` re-runs the
    registration instead of silently no-opping into a registry that no longer
    holds it.
    """
    reg = Registry("late", register_hint="register_late()")
    _late_module("otto._fake_late_ext_home", reg)
    ext = types.ModuleType("custom_frames_ext")
    sys.modules["custom_frames_ext"] = ext
    before = frozenset(sys.modules) - {"otto._fake_late_ext_home", "custom_frames_ext"}
    try:
        reg.register("zephyr_ish", object(), origin="custom_frames_ext")

        _restore_registries([], before)

        assert list(reg.names()) == []
        assert "custom_frames_ext" not in sys.modules
    finally:
        sys.modules.pop("otto._fake_late_ext_home", None)
        sys.modules.pop("custom_frames_ext", None)


# ── the host-class SPEC table: a plain dict, half of a pair with a Registry ──
#
# ``register_host_class`` writes ``HOST_CLASSES`` (a ``Registry``) and
# ``otto.host.os_profile._HOST_SPECS`` (a ``dict``) together, and
# ``_nearest_registered_spec`` reads them together. Restoring only the half the
# ``Registry`` scan can see leaves a spec whose class is gone, and the next
# ``register_host_class`` anywhere in the process — the next test's bootstrap of
# a repo whose init registers a host class, say — dies on
# ``ValueError: Unknown host class '<the leaked name>'``. As with the provider
# seams above, these tests INJECT the hostile condition into the guard's restore
# rather than hoping to observe a leak from a neighbouring test. Their otto
# imports are function-local like ``_providers``: a module-scope import would
# run at COLLECTION, inside the root guard's own baseline.


def _guard_snapshot():
    """Exactly what ``_isolate_registries`` records at setup."""
    return (
        [(reg, _snapshot(reg)) for reg in _loaded_registries()],
        frozenset(sys.modules),
        _host_spec_snapshot(),
    )


def _guard_restore(state) -> None:
    """Exactly what ``_isolate_registries`` runs at teardown, in its order."""
    snapshots, modules_before, host_specs = state
    _restore_registries(snapshots, modules_before)
    _restore_host_specs(host_specs)


def test_host_spec_registered_during_a_test_is_dropped_by_the_restore() -> None:
    """The injection: register a host class, restore, expect BOTH tables clean.

    The third assertion is the call that actually failed: a leftover spec is not
    merely untidy, it POISONS ``_nearest_registered_spec`` for every later
    registration in the process.
    """
    from otto.host import os_profile
    from otto.host.unix_host import UnixHost

    class _PinHost(UnixHost):
        pass

    class _NextPinHost(UnixHost):
        pass

    state = _guard_snapshot()
    os_profile.register_host_class("pinhostos", _PinHost)
    # The hostile condition is real, in both halves of the pair.
    assert "pinhostos" in os_profile.HOST_CLASSES.names()
    assert "pinhostos" in os_profile._HOST_SPECS

    _guard_restore(state)

    assert "pinhostos" not in os_profile.HOST_CLASSES.names()
    assert "pinhostos" not in os_profile._HOST_SPECS
    try:
        os_profile.register_host_class("nextpinhostos", _NextPinHost)
    finally:
        _guard_restore(state)


def test_host_specs_present_before_the_test_survive_the_restore() -> None:
    """Restore, not reset: a spec that predates the snapshot is still there, identical.

    A module- or session-scoped fixture registering a host class sets up BEFORE
    any function-scoped snapshot, so its spec is inside the snapshot and has to
    come back out of it — the same distinction the provider survival pin above
    draws. A guard that cleared the dict instead would kill that fixture with
    its first test, and take otto's own built-in specs with it.
    """
    from otto.host import os_profile
    from otto.host.unix_host import UnixHost
    from otto.models.host import UnixHostSpec

    class _PreExistingHost(UnixHost):
        pass

    class _PreExistingSpec(UnixHostSpec):
        pass

    class _AddedHost(UnixHost):
        pass

    pristine = _guard_snapshot()
    try:
        os_profile.register_host_class("preexistingos", _PreExistingHost, spec=_PreExistingSpec)

        state = _guard_snapshot()
        os_profile.register_host_class("addedbythetestos", _AddedHost)

        _guard_restore(state)

        assert os_profile._HOST_SPECS["preexistingos"] is _PreExistingSpec
        assert os_profile._HOST_SPECS["unix"] is UnixHostSpec
    finally:
        _guard_restore(pristine)


def test_a_spec_table_first_seen_mid_test_is_reduced_to_the_surviving_classes() -> None:
    """No pre-test copy to return to: agree with the ``HOST_CLASSES`` the restore left.

    A snapshot taken while ``otto.host.os_profile`` is UNLOADED records
    ``None`` — "there was nothing to snapshot", not "empty" — and a guard that
    then skipped the restore would let everything the test registered survive.
    The registry restore has meanwhile reduced ``HOST_CLASSES`` to its
    import-time entries, so the one correct answer is the set that agrees with
    it: ``set(_HOST_SPECS) == set(HOST_CLASSES.names())``, which is precisely
    what ``_nearest_registered_spec`` iterates.
    """
    from otto.host import os_profile
    from otto.host.unix_host import UnixHost

    class _LatePinHost(UnixHost):
        pass

    snapshots = [(reg, _snapshot(reg)) for reg in _loaded_registries()]
    modules_before = frozenset(sys.modules)
    real = sys.modules["otto.host.os_profile"]
    try:
        del sys.modules["otto.host.os_profile"]
        host_specs = _host_spec_snapshot()
        assert host_specs is None
    finally:
        sys.modules["otto.host.os_profile"] = real

    os_profile.register_host_class("latepinhostos", _LatePinHost)

    _restore_registries(snapshots, modules_before)
    _restore_host_specs(host_specs)

    assert "latepinhostos" not in os_profile._HOST_SPECS
    assert set(os_profile._HOST_SPECS) == set(os_profile.HOST_CLASSES.names())
