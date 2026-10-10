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

import pytest

from otto.params import OPTIONS
from otto.registry import ClassEntry, Registry

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
    """Sanity: the guard's discovery can see otto's registries at all.

    ``_isolate_registries`` snapshots the tables :func:`otto.registry.instances`
    lists. If that discovery silently found nothing, the guard would "pass"
    while protecting nothing.
    """
    from tests.conftest import _snapshot_tables

    found = [table for table, _state in _snapshot_tables()]
    assert found, "registry discovery found no otto registries — the guard is a no-op"
    assert isinstance(OPTIONS, Registry)
    assert any(reg is OPTIONS for reg in found), "OPTIONS is not among the guarded registries"


def test_discovery_sees_a_registry_built_mid_test() -> None:
    """Completeness: a registry built mid-test is discovered at once.

    Discovery is the engine's own list of the tables it built, not a scan of
    ``sys.modules``, so a table is guarded from the moment it is constructed,
    whatever module (if any) binds it and whatever the module count does.
    """
    from tests.conftest import _snapshot_tables

    _snapshot_tables()  # prime any memo a future edit might introduce

    probe_registry: Registry[ClassEntry[object]] = Registry(
        "w11 probe",
        entry=ClassEntry,
        register_hint="tests.e2e.cli.test_registry_isolation_e2e (test-local)",
    )

    found = [table for table, _state in _snapshot_tables()]
    assert any(reg is probe_registry for reg in found), (
        "a Registry built mid-test was not discovered — the isolation guard would silently skip it"
    )
