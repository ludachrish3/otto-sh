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
    _provider_snapshot,
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
