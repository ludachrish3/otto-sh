"""Regression guard: the registry-isolation guard reaches the e2e tree too.

``_isolate_registries`` (root ``tests/conftest.py``) snapshots every global otto
``Registry`` before each test and restores it after. It originally lived in
``tests/unit/conftest.py``, so it covered only the unit tree — while the e2e
tree bootstraps the real ``otto`` CLI, whose commands, host classes, backends
and carriers all land in those same process-global singletons. ``tests_hostless``
runs ``tests/unit`` and ``tests/e2e`` in ONE session, so a single xdist worker
runs tests from both trees in one process: an entry an e2e test leaves behind
lands in the very registry the next unit test asserts against.

No e2e test leaks one *today* — the guard was promoted preventively, because a
guard scoped narrower than the state it guards is exactly the defect behind
issues #132 (hermetic web dist) and #133 (the CliRunner shield). This test pins
the guard's REACH into this tree; ``tests/unit/test_registry_isolation.py`` pins
its BEHAVIOUR.

It asserts on ``request.fixturenames`` rather than trying to observe a leak
across two tests: a fixture's restore only runs at teardown, so a leak would
have to be caught by a *second* test, and under xdist the pair can land in
different workers — which would make the guard pass by luck exactly when it is
broken.
"""

import sys
import types

import pytest

from otto.registry import Registry
from otto.suite.register import SUITES

pytestmark = pytest.mark.hostless

# Guards that protect process-global state and must therefore apply to EVERY
# tree. Each is defined in the root tests/conftest.py; if one is moved back into
# a package conftest, it silently stops covering this tree and this test fails.
GLOBAL_GUARDS = (
    "_isolate_registries",
    "_clirunner_live_log_capture_guard",
    "_reset_otto_context",
    "_restore_otto_logger_state",
    "_restore_bootstrap_state",
    "_restore_provider_registries",
    "_coverage_preinit_failure_is_loud",
)


def test_global_guards_apply_to_the_e2e_tree(request: pytest.FixtureRequest) -> None:
    """Every process-global guard must be active for tests in tests/e2e."""
    missing = [g for g in GLOBAL_GUARDS if g not in request.fixturenames]
    assert not missing, (
        f"process-global guard(s) {missing} are not active in tests/e2e. They must be "
        f"defined in the ROOT tests/conftest.py, not a package conftest — a guard "
        f"scoped narrower than the state it guards is the defect behind #132/#133."
    )


def test_registry_discovery_sees_ottos_registries() -> None:
    """Sanity: the guard's dynamic discovery can see otto's registries at all.

    ``_isolate_registries`` finds registries by scanning loaded ``otto.*``
    modules for ``Registry`` instances. If that discovery silently found
    nothing, the guard would "pass" while protecting nothing.
    """
    from tests.conftest import _loaded_registries

    found = _loaded_registries()
    assert found, "registry discovery found no otto registries — the guard is a no-op"
    assert isinstance(SUITES, Registry)
    assert any(reg is SUITES for reg in found), "SUITES is not among the guarded registries"


def test_discovery_sees_a_new_registry_at_unchanged_module_count(monkeypatch) -> None:
    """Completeness: a registry imported mid-test is discovered even when
    ``len(sys.modules)`` did not change.

    Import one module and evict another in the same test — the exact shape a
    count-keyed memo was blind to: same count, stale cache, and a brand-new
    ``Registry`` was silently NOT isolated (its guard "passed" while
    protecting nothing). Discovery now re-scans every call — measured at
    0.2 ms with all of otto imported, 25x under the plan's keep-the-cache
    threshold — so this pin holds by construction, and fails loudly if
    anyone re-adds a cache without an identity-safe key.
    """
    from tests.conftest import _loaded_registries

    _loaded_registries()  # prime any memo a future edit might reintroduce

    probe_mod = types.ModuleType("otto._registry_probe_w11")
    probe_registry: Registry[object] = Registry(
        "w11 probe", register_hint="tests.e2e.cli.test_registry_isolation_e2e (test-local)"
    )
    probe_mod.PROBE = probe_registry

    # Evict a loaded otto module (monkeypatch restores it) so the module
    # COUNT is unchanged by the paired insert below. Any otto.* module works;
    # nothing re-imports it inside this test.
    evictable = next(
        name
        for name, mod in sys.modules.items()
        if name.startswith("otto.") and mod is not None and name != "otto.registry"
    )
    monkeypatch.delitem(sys.modules, evictable)
    monkeypatch.setitem(sys.modules, "otto._registry_probe_w11", probe_mod)

    found = _loaded_registries()
    assert any(reg is probe_registry for reg in found), (
        "a Registry imported mid-test (module count unchanged) was not "
        "discovered — the isolation guard would silently skip it"
    )
