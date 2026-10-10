"""Regression for the registry / ``sys.modules`` isolation gap.

The "other half" of issue #108: ``_isolate_registries`` (tests/conftest.py)
snapshots every engine table (``otto.registry.instances()``) and restores it on
teardown. But an extension module listed in a repo's ``init`` (e.g.
``custom_hosts``, which calls ``register_command_frame`` at import) registers as
an **import side effect** — so dropping the entry while leaving the module in
``sys.modules`` desyncs registry state from module state: a later
``importlib.import_module`` is a no-op and never re-registers. ``_restore_tables``
must therefore evict the origin module of every dropped entry — but only origins
the test itself imported, never a module already loaded before it (a pytest test
module registering a local class, or core ``otto``).

This bit only single-process (``-n0``): under ``-n auto`` the importer and the
victim scatter across xdist workers.
"""

import sys
import types
from dataclasses import dataclass

from otto.registry import Registry, RegistryView, instances
from tests.conftest import _restore_tables, _snapshot_tables


@dataclass(frozen=True)
class Thing:
    value: object


def _things(kind: str = "thing") -> "Registry[Thing]":
    return Registry(kind, entry=Thing, register_hint=f"register_{kind}()")


def _from_module(module: str, fn, *args, **kwargs):
    """Call *fn* from a frame whose module is *module* (attribution is by frame)."""
    code = compile("result = fn(*args, **kwargs)", f"<{module}>", "exec")
    scope = {"__name__": module, "fn": fn, "args": args, "kwargs": kwargs}
    exec(code, scope)  # noqa: S102 — a synthetic registrant module
    return scope["result"]


def _snapshot(reg: Registry) -> list:
    return reg._snapshot()


def _every_table_but(reg: Registry) -> list:
    """Snapshot every live table except *reg*: *reg* is then one the snapshot missed."""
    return [
        (t, t._snapshot()) for t in instances() if t is not reg and not isinstance(t, RegistryView)
    ]


def test_dropped_entry_origin_module_is_evicted() -> None:
    reg = _things()
    snapshot = _snapshot(reg)  # pristine baseline

    # An extension module the test itself imports (absent before the test),
    # registering as an import side effect.
    before = frozenset(sys.modules)
    sys.modules["fake_ext_isolation_regression"] = types.ModuleType("fake_ext_isolation_regression")
    _from_module("fake_ext_isolation_regression", reg.register, "added", Thing(object()))

    _restore_tables([(reg, snapshot)], before)

    assert "added" not in reg.names()  # entry the test added is dropped
    # …and its origin module is evicted, so a re-import re-runs the registration
    assert "fake_ext_isolation_regression" not in sys.modules


def test_origin_module_loaded_before_the_test_is_not_evicted() -> None:
    """A module already imported before the test (e.g. the running test file,
    which registers a locally defined class) must be left in ``sys.modules`` —
    evicting it breaks ``inspect.getfile`` for every later registration in
    that file.
    """
    reg = _things()
    snapshot = _snapshot(reg)

    # Module present BEFORE the test (stand-in for a collected pytest test file).
    sys.modules["fake_preloaded_test_module"] = types.ModuleType("fake_preloaded_test_module")
    before = frozenset(sys.modules)
    try:
        _from_module("fake_preloaded_test_module", reg.register, "added", Thing(object()))

        _restore_tables([(reg, snapshot)], before)

        assert "added" not in reg.names()  # entry still dropped
        assert "fake_preloaded_test_module" in sys.modules  # but the module survives
    finally:
        sys.modules.pop("fake_preloaded_test_module", None)


def test_core_otto_origin_module_is_not_evicted() -> None:
    reg = _things()
    snapshot = _snapshot(reg)
    assert "otto.errors" in sys.modules  # imported by otto.registry at module top

    before = frozenset(sys.modules) - {"otto.errors"}  # pretend it was imported now
    _from_module("otto.errors", reg.register, "added", Thing(object()))

    _restore_tables([(reg, snapshot)], before)

    assert "added" not in reg.names()
    assert "otto.errors" in sys.modules  # core otto is never evicted


def test_snapshot_entries_are_preserved() -> None:
    reg = _things()
    _from_module("builtin_origin", reg.register, "builtin", Thing("keep"))
    snapshot = _snapshot(reg)

    before = frozenset(sys.modules)
    sys.modules["fake_ext_isolation_regression2"] = types.ModuleType(
        "fake_ext_isolation_regression2"
    )
    _from_module("fake_ext_isolation_regression2", reg.register, "added", Thing("drop"))

    _restore_tables([(reg, snapshot)], before)

    assert reg.names() == ["builtin"]  # added dropped, snapshot restored
    assert reg.get("builtin") == Thing("keep")
    assert reg.origin("builtin") == "builtin_origin"
    assert "fake_ext_isolation_regression2" not in sys.modules


def test_an_entry_the_test_replaced_is_restored_and_its_origin_evicted() -> None:
    """An overwrite counts as an addition: the old entry comes back, the new origin goes."""
    reg = _things()
    _from_module("builtin_origin", reg.register, "builtin", Thing("keep"))
    snapshot = _snapshot(reg)

    before = frozenset(sys.modules)
    sys.modules["fake_ext_isolation_regression3"] = types.ModuleType(
        "fake_ext_isolation_regression3"
    )
    _from_module(
        "fake_ext_isolation_regression3", reg.register, "builtin", Thing("new"), overwrite=True
    )

    _restore_tables([(reg, snapshot)], before)

    assert reg.get("builtin") == Thing("keep")
    assert reg.origin("builtin") == "builtin_origin"
    assert "fake_ext_isolation_regression3" not in sys.modules


# ── the PROVIDER seams: engine subscriptions, restored like every table ─────
#
# ``register_product_provider`` / ``register_dev_tool_provider`` subscribe to
# two engine subscriptions, and every ``bootstrap()`` importing a
# provider-registering init module adds to them. ``_isolate_registries``
# restores them with every other table; this test INJECTS the hostile
# condition into its restore rather than hoping to observe a leak from a
# neighbouring test.


def test_a_provider_subscribed_during_a_test_is_dropped_by_the_restore() -> None:
    """Both seams: a provider the test adds goes, one that predates the snapshot stays."""
    from otto.host.dev_tool import DEV_TOOL_PROVIDERS
    from otto.host.product import PRODUCT_PROVIDERS

    for subscription in (PRODUCT_PROVIDERS, DEV_TOOL_PROVIDERS):
        pre_existing = subscription.subscribe(lambda host: [])
        try:
            before = [s.value for s in subscription.items()]
            snapshots = _snapshot_tables()
            subscription.subscribe(lambda host: [])
            assert len(subscription) == len(before) + 1  # the hostile condition is real

            _restore_tables(snapshots, frozenset(sys.modules))

            assert [s.value for s in subscription.items()] == before
        finally:
            pre_existing.cancel()


# ── the hole: a registry whose module was imported DURING the test ────────


def _late_module(name: str, reg: Registry) -> types.ModuleType:
    """An ``otto.*`` module carrying *reg*, as if imported mid-test."""
    mod = types.ModuleType(name)
    mod.LATE = reg
    sys.modules[name] = mod
    return mod


def test_a_registry_that_first_appeared_mid_test_is_not_left_polluted() -> None:
    """An entry a test adds must not survive just because the registry is new.

    ``_isolate_registries`` snapshots the tables that exist at setup, so a
    registry built by an ``otto.*`` module the test itself imports has no
    snapshot — and a restore iterating snapshots alone never looks at it.
    Everything the test registered there would leak into the next test with
    nothing to notice.
    """
    reg = Registry("late", entry=Thing, register_hint="register_late()")
    snapshots = _every_table_but(reg)
    _from_module("otto._fake_late_pkg", reg.register, "from_import", Thing(object()))
    _late_module("otto._fake_late_pkg", reg)
    before = frozenset(sys.modules) - {"otto._fake_late_pkg"}
    try:
        reg.register("from_test", Thing(object()))

        _restore_tables(snapshots, before)

        assert list(reg.names()) == ["from_import"]
    finally:
        sys.modules.pop("otto._fake_late_pkg", None)


def test_a_late_registrys_own_import_time_entries_survive() -> None:
    """otto's own import-time registrations are the new baseline — never dropped.

    Once ``otto.project.actions`` (say) is imported, its six first-party
    entries ARE the process's state: the module stays in ``sys.modules``, so a
    re-import is a no-op and anything dropped here could never be re-registered.
    Clearing them would leave otto missing its own defaults for every later
    test — a cure far worse than the leak.
    """
    reg = Registry("late", entry=Thing, register_hint="register_late()")
    snapshots = _every_table_but(reg)
    for name in ("install", "uninstall"):
        _from_module("otto._fake_late_defaults", reg.register, name, Thing(object()))
    _late_module("otto._fake_late_defaults", reg)
    before = frozenset(sys.modules) - {"otto._fake_late_defaults"}
    try:
        _restore_tables(snapshots, before)

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
    reg = Registry("late", entry=Thing, register_hint="register_late()")
    snapshots = _every_table_but(reg)
    _late_module("otto._fake_late_ext_home", reg)
    ext = types.ModuleType("custom_frames_ext")
    sys.modules["custom_frames_ext"] = ext
    before = frozenset(sys.modules) - {"otto._fake_late_ext_home", "custom_frames_ext"}
    try:
        _from_module("custom_frames_ext", reg.register, "zephyr_ish", Thing(object()))

        _restore_tables(snapshots, before)

        assert list(reg.names()) == []
        assert "custom_frames_ext" not in sys.modules
    finally:
        sys.modules.pop("otto._fake_late_ext_home", None)
        sys.modules.pop("custom_frames_ext", None)


# ── the host-class record: class and spec restored as one entry ─────────────
#
# A ``HOST_CLASSES`` record carries its class and its spec together, so the
# engine-table restore that drops a test's registration drops both, and a record
# that predates the snapshot comes back whole. These tests INJECT the hostile
# condition into the guard's restore rather than hoping to observe a leak from a
# neighbouring test. Their otto imports are function-local like ``_providers``:
# a module-scope import would run at COLLECTION, inside the root guard's own
# baseline.


def _guard_snapshot():
    """Exactly what ``_isolate_registries`` records at setup."""
    return (_snapshot_tables(), frozenset(sys.modules))


def _guard_restore(state) -> None:
    """Exactly what ``_isolate_registries`` runs at teardown."""
    snapshots, modules_before = state
    _restore_tables(snapshots, modules_before)


def test_a_host_class_registered_during_a_test_is_dropped_by_the_restore() -> None:
    """The injection: register a host class, restore, expect the record gone.

    The last call is the one a leftover would break: the spec lookup behind a
    registration that names no spec walks every registered record.
    """
    from otto.host import os_profile
    from otto.host.unix_host import UnixHost

    class _PinHost(UnixHost):
        pass

    class _NextPinHost(UnixHost):
        pass

    state = _guard_snapshot()
    os_profile.register_host_class("pinhostos", _PinHost)
    assert "pinhostos" in os_profile.HOST_CLASSES.names()

    _guard_restore(state)

    assert "pinhostos" not in os_profile.HOST_CLASSES.names()
    try:
        os_profile.register_host_class("nextpinhostos", _NextPinHost)
    finally:
        _guard_restore(state)


def test_host_classes_present_before_the_test_survive_the_restore() -> None:
    """Restore, not reset: a record that predates the snapshot is still there, identical.

    A module- or session-scoped fixture registering a host class sets up BEFORE
    any function-scoped snapshot, so its record is inside the snapshot and has
    to come back out of it. A guard that cleared the table instead would kill
    that fixture with its first test, and take otto's own built-ins with it.
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

        assert os_profile.HOST_CLASSES.peek("preexistingos").spec is _PreExistingSpec
        assert "addedbythetestos" not in os_profile.HOST_CLASSES.names()
        # Read through build_host_spec: the built-in's entry may hold the Ref it
        # was registered with, which the restore puts back exactly as it found it.
        assert os_profile.build_host_spec("unix") is UnixHostSpec
    finally:
        _guard_restore(pristine)
