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

``_isolate_registries`` (the guard against global-registry state leaking between
tests) and ``_clirunner_live_log_capture_guard`` (the issue-#110 CliRunner
shield) used to live here too. Both moved to the root ``tests/conftest.py`` for
the same reason: the state they guard is process-global, so a guard confined to
one tree leaves every other tree exposed — the defect behind issues #132 and
#133. Anything guarding process-global state belongs at the root; only genuinely
tree-local setup belongs here.
"""

import sys

import pytest


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
