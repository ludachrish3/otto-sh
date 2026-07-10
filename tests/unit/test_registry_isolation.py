"""Regression for the registry / ``sys.modules`` isolation gap.

The "other half" of issue #108: ``_isolate_registries`` (tests/unit/conftest.py)
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
from tests.unit.conftest import _restore_registries


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
