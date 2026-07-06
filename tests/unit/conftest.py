"""
Unit-tree conftest.

The parametrized host fixtures (``host1`` / ``host2`` / ``host3`` /
``hop_host`` / ``transfer_host``) and the ``host_data`` / ``make_host``
helpers used to live here. They moved to ``tests/conftest.py`` so the
``tests/integration/host/`` tree can use them too without import gymnastics.
The unit tests inherit them transparently through the conftest hierarchy —
no changes needed at the call sites.

The OttoContext ContextVar reset (``_reset_otto_context``) likewise lives in
the root ``tests/conftest.py`` now, so it applies to the integration tree as
well. That matters under ``make coverage``, which runs unit and integration
tests in one process: a module-scoped context an integration fixture installs
(e.g. the integration host lab) must not leak across an xdist worker into a
unit test that asserts a pristine ``try_get_context() is None``.

It also hosts ``_isolate_registries`` (below), the unit-tree-wide guard against
global-registry state leaking between tests.
"""

import sys

import pytest

from otto.registry import Registry

_cached_registries: list[Registry] = []
_cached_module_count: int = -1


def _loaded_registries() -> list[Registry]:
    """Return every ``Registry`` reachable from a loaded ``otto.*`` module.

    Discovery is dynamic (scans ``sys.modules``) rather than a hand-maintained
    list, so a registry added in the future is isolated automatically without a
    matching test-side edit. Instances are de-duplicated by ``id`` because a
    single registry is often re-exported from several modules. The result is
    memoized and only re-scanned when ``sys.modules`` grows (a new module —
    possibly carrying a new registry — was imported); registries are import-time
    singletons that are never torn down, so this keeps the per-test snapshot cost
    negligible across the ~15k-test unit run.
    """
    global _cached_registries, _cached_module_count  # noqa: PLW0603 — memoized discovery
    module_count = len(sys.modules)
    if module_count != _cached_module_count:
        found: dict[int, Registry] = {}
        for module in list(sys.modules.values()):
            mod_name = getattr(module, "__name__", "")
            if mod_name != "otto" and not mod_name.startswith("otto."):
                continue
            try:
                members = vars(module)
            except TypeError:  # pragma: no cover - namespace without __dict__
                continue
            for value in members.values():
                if isinstance(value, Registry):
                    found[id(value)] = value
        _cached_module_count = module_count
        _cached_registries = list(found.values())
    return _cached_registries


@pytest.fixture(autouse=True)
def _isolate_registries():
    """Snapshot every global otto ``Registry`` before each test; restore after.

    The ``otto.registry.Registry`` singletons (``INSTRUCTIONS``,
    ``LOADER_CLASSES``, ``FRAME_CLASSES``, ``CLI_COMMANDS``, …) live for the
    whole process. Tests that register entries into them — via ``@instruction``,
    ``register_binary_loader``, ``register_cli_command``, etc. — never clean up,
    so under the nightly ``--count=N --repeat-scope=session`` repeat the second
    pass re-registers the same name and the registry's loud collision guard
    raises ``ValueError: already registered`` (issue #108). A single CI pass
    registers each name exactly once and never trips this, which is why the leak
    only surfaces in the nightly repeat job — never in ``make nox`` /
    ``make coverage`` (both single-pass by default).

    Snapshotting each registry's entries before the test and, on teardown,
    dropping anything the test added and restoring the originals keeps every
    registry byte-for-byte stable across tests and across repeat iterations of
    the same test in one process. Built-in registrations (present at import)
    survive because they are part of the snapshot.

    The ``tests/unit/suite`` package additionally keeps its own
    ``_isolate_suites`` fixture: it *clears* ``SUITES`` to an empty baseline that
    those tests assert against, which this snapshot/restore alone does not do.
    """
    snapshots = [
        (reg, {name: (reg.get(name), reg.origin(name)) for name in reg.names()})
        for reg in _loaded_registries()
    ]

    yield

    for reg, parked in snapshots:
        for name in list(reg.names()):
            if name not in parked:
                reg.unregister(name)
        for name, (entry, origin) in parked.items():
            reg.register(name, entry, overwrite=True, origin=origin)


@pytest.fixture
def purge_tmp_imports(tmp_path_factory):
    """Drop modules a test imported from its tmp dir, so they don't leak onward.

    Tests that write a package/repo into ``tmp_path`` and import it leave those
    modules in ``sys.modules``. On the nightly ``--repeat-scope=session`` repeat
    the second pass re-imports the same module *name* and gets pass 1's cached
    copy: a stale valid module shadows a freshly-written broken one
    (``bootstrap()`` reports 0 discovery errors instead of 1), and
    ``assert "fake_pkg.cmds" not in sys.modules`` right after a fresh registration
    fails because pass 1 already cached it (issue #108). A single CI pass imports
    each name once and never trips this.

    This is **opt-in**, requested by the tests/dirs that import tmp artifacts —
    ``tests/unit/bootstrap`` (dir-wide) and the lazy-loader test in
    ``tests/unit/cli`` — rather than a unit-tree-wide autouse. A blanket purge
    also drops tmp modules that *other* tests legitimately keep imported across
    calls within one test (e.g. ``test_listing``'s instruction-panel matching),
    so scoping it to the leak sites keeps the fix surgical. The PR repeat-guard
    (see the ``tests_unit_repeat`` nox session) catches any new leak site so it
    can opt in too.

    Only modules whose file lives under pytest's base temp dir are removed;
    production/stdlib/site-packages imports are left untouched.
    """
    base = str(tmp_path_factory.getbasetemp())
    before = frozenset(sys.modules)

    yield

    for name in set(sys.modules) - before:
        module = sys.modules.get(name)
        if module is None:
            continue
        origin = getattr(module, "__file__", None) or ""
        if origin.startswith(base):
            del sys.modules[name]
