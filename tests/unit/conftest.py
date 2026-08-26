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

``_no_ambient_webassets`` below is the mirror-image case: the *fixture itself*
(``neutralized_webassets``) is a root-conftest process-global guard per #132,
but its ACTIVATION — making it autouse — is deliberately scoped to this tree
only. The unit lane must default to blind (issue #175: a real ``make web``
build on a dev box must never make a unit test pass by accident), but
``tests/e2e`` must default to seeing the real bundle (the Playwright dashboard
lane serves and screenshots it) and ``tests/integration`` has no opinion
either way. Autouse-ing the same fixture at the root would flip the e2e
default too, so the fixture lives at the root (shareable, #132-compliant) and
only its autouse wiring is tree-local, matching this file's established
root-fixture-vs-local-activation split.
"""

import sys

import pytest


@pytest.fixture(autouse=True)
def _no_ambient_webassets(neutralized_webassets: object) -> None:
    """Every tests/unit test starts blind to real build artifacts (#175).

    Activates the root ``neutralized_webassets`` fixture for every test
    collected under ``tests/unit`` so a missing monkeypatch fails identically
    on a bare-checkout CI runner and on a dev box that happens to have run
    ``make web`` — see ``neutralized_webassets``'s docstring in the root
    conftest for the full mechanism and the #132 rationale for keeping the
    fixture itself there.

    Ordering with package-level hermetic-dist fixtures (e.g.
    ``tests/unit/monitor/conftest.py``'s autouse ``_hermetic_static_dir``,
    which stands in a throwaway React dist so most of that package's tests
    can boot a ``MonitorServer`` without needing a real build): pytest sets up
    autouse fixtures shallowest-conftest-first, so THIS fixture (declared in
    the shallower ``tests/unit/conftest.py``) runs and neutralizes first, and
    the deeper package's autouse fixture then re-patches the same attribute
    over it — verified empirically for this pair (``pytest tests/unit/monitor``
    green: the package's hermetic dist wins, unaffected by the neutralizer
    running first). A test that needs the real present-bundle path instead
    (outside that package) requests ``hermetic_covapp_bundle`` /
    ``hermetic_monitor_dist`` directly — an in-body request always wins over
    any autouse fixture, ordering aside.
    """


@pytest.fixture(autouse=True)
def _no_off_loopback_dials(refuse_off_loopback_dials: list[str]) -> None:
    """Every tests/unit test refuses in-process asyncio dials off loopback (root fixture).

    A hostless test that opens a socket to a routable address is measuring
    the network, not the code:
    ``test_power.py::test_unix_shutdown_issues_shutdown_sudo`` spent 30 s on
    three real SSH dials to 10.0.0.1 because the code under test resolved
    its userland before the mocked ``run`` — a fixed 30 s on every run, x2 in
    unit-repeat, x5 per Python in nightly's unit-matrix, and (by inference
    from a fixed cost, not measured) a tail that lands on whichever xdist
    worker drew it. Activation is tree-local for the same reason
    ``_no_ambient_webassets``'s is: ``tests/integration`` dials the lab by
    design, and ``tests/e2e`` holds subprocess tests this in-process patch
    cannot reach anyway. See the root fixture for the mechanism and scope.
    """


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
