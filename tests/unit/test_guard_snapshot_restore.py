"""The root state guards RESTORE pre-test state — they must not reset to defaults.

Pins Wave 14's conversion of ``_restore_otto_logger_state`` and the
bootstrap-state restore, now ``_restore_composition_state`` (root conftest),
from reset-to-default teardowns to snapshot-restore. The composition state has
two owners, ``otto.config.project``'s discovery cache and the catalog
``otto.declared`` holds, and the guard restores the CLI's completion-cache snapshot on
``otto.registry`` with them; all three are pinned. The motivating defect
(review §5.5): a module-scoped fixture's logging/bootstrap state was
destroyed by the reset after its FIRST test, so every later test in that
module silently ran against defaults.

The pin is deliberately order-independent under ``pytest-randomly``: a
module-scoped fixture primes the state and BOTH tests assert it. Under the
old reset teardown, whichever test runs first wipes the primed state and the
other one fails — either order is red pre-fix, green post-fix.

The isolation direction (a test's own poison must NOT leak forward) is not
re-pinned here: ``tests/unit/test_env_hermeticity.py`` re-runs the historical
bootstrap poisoner/victim pair in a subprocess, which holds for
snapshot-restore by induction (each test hands back the worker's pre-test
state).
"""

import contextlib
import dataclasses
import io
import logging
import sys
from collections.abc import Iterator
from types import SimpleNamespace

import pytest

import otto.config.project as config_project
from otto import declared, registry
from otto.logger import management

pytestmark = pytest.mark.hostless

_PRIMED_KEEP_SECONDS = 86_400.0 * 41  # arbitrary, unmistakably non-default


@pytest.fixture(scope="module")
def primed_logger_state() -> Iterator[logging.Handler]:
    """Module-scoped logging setup: a retention value and a marker handler."""
    saved_keep = management._state.keep_seconds
    management._state.keep_seconds = _PRIMED_KEEP_SECONDS
    marker = logging.NullHandler()
    otto_logger = logging.getLogger("otto")
    otto_logger.addHandler(marker)
    yield marker
    otto_logger.removeHandler(marker)
    management._state.keep_seconds = saved_keep


def _assert_logger_state_survives(marker: logging.Handler) -> None:
    assert management._state.keep_seconds == _PRIMED_KEEP_SECONDS, (
        "a module fixture's logging-management state was wiped by a per-test "
        "teardown — the guard reset to defaults instead of restoring its snapshot"
    )
    assert marker in logging.getLogger("otto").handlers, (
        "a module fixture's handler on the 'otto' logger was detached by a "
        "per-test teardown — the guard reset instead of restoring"
    )


_ROOT_POISON = "_guard_root_poison"


@pytest.fixture(scope="module")
def primed_root_state() -> Iterator[logging.Handler]:
    """Module-scoped ROOT logging setup: a marker handler on the root logger.

    The guard grew a ROOT branch with the 2026-08-30 root-capture cutover (otto
    installs its console handler and QueueHandler there now), and that branch
    needs the same pin the ``'otto'`` branch has had.
    """
    marker = logging.NullHandler()
    root = logging.getLogger()
    root.addHandler(marker)
    yield marker
    root.removeHandler(marker)


def _assert_root_state_survives(marker: logging.Handler) -> None:
    """Both directions of the ROOT restore, in one assertion pair.

    RESTORE (not reset): the module fixture's marker must still be attached.
    ISOLATION: the sibling test below deliberately leaves a poison handler on
    root and never removes it — the guard's root restore is the only thing that
    strips it, so whichever of the two tests runs SECOND reds if that restore
    is dropped. (Under xdist the two can land on different workers, in which
    case the isolation half is merely vacuous, never wrong; the mutation is run
    with ``-n0``.)
    """
    root = logging.getLogger()
    assert marker in root.handlers, (
        "a module fixture's handler on the ROOT logger was detached by a "
        "per-test teardown — the guard reset instead of restoring"
    )
    strays = [h for h in root.handlers if getattr(h, _ROOT_POISON, False)]
    assert not strays, (
        "a sibling test's root handler outlived its teardown — the guard's ROOT "
        f"handler restore is missing or incomplete: {strays}"
    )
    poison = logging.NullHandler()
    setattr(poison, _ROOT_POISON, True)
    root.addHandler(poison)  # deliberately NOT removed: the guard must strip it


def test_logger_state_outlives_a_sibling_test(
    primed_logger_state: logging.Handler, primed_root_state: logging.Handler
) -> None:
    _assert_logger_state_survives(primed_logger_state)
    _assert_root_state_survives(primed_root_state)


def test_logger_state_outlives_another_sibling_test(
    primed_logger_state: logging.Handler, primed_root_state: logging.Handler
) -> None:
    _assert_logger_state_survives(primed_logger_state)
    _assert_root_state_survives(primed_root_state)


# ── the spec §4 noise floor must not outlive the test that installed it ──────
#
# ``install_console`` now pins a level on three third loggers
# (``DEFAULT_LIBRARY_LEVELS``) on EVERY install, and several test venues run
# ``init_cli_logging`` without a ``management.reset()`` afterwards. The guard's
# third-logger restore is what stops those levels running on for the rest of
# the xdist worker.
#
# These drive the guard's generator BY HAND rather than using the
# sibling-test pattern above. The autouse fixture wraps every test, so its
# teardown cannot be observed from inside one; and a sibling pair would pass
# vacuously whenever pytest-randomly happened to schedule the observer first,
# which is exactly the "green whether the fix exists or not" shape this repository
# rejects. Driving it directly is deterministic and order-independent.


@contextlib.contextmanager
def _logger_guard_round_trip() -> Iterator[None]:
    """Run one setup/teardown cycle of the root conftest's logging guard.

    ``tests.conftest`` is the very module pytest loaded as a plugin (same
    object — the package has an ``__init__.py``, so the conftest is imported
    under its dotted name), so this exercises the live guard, not a copy.
    """
    from tests import conftest as root_conftest

    generator = root_conftest._restore_otto_logger_state.__wrapped__()
    next(generator)
    try:
        yield
    finally:
        with pytest.raises(StopIteration):
            next(generator)


def test_the_guard_hands_back_loggers_the_noise_floor_pinned(tmp_path) -> None:
    """A test that installs otto's console must not leave asyncssh & co. quieted."""
    name = next(iter(management.DEFAULT_LIBRARY_LEVELS))
    floored_level = logging.getLevelName(management.DEFAULT_LIBRARY_LEVELS[name])
    logging.getLogger(name).setLevel(logging.NOTSET)

    with _logger_guard_round_trip():
        management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
        # POSITIVE CONTROL: the floor really was applied inside the window, so
        # the assertion below is about the RESTORE and not about a no-op.
        assert logging.getLogger(name).level == floored_level

    assert logging.getLogger(name).level == logging.NOTSET, (
        f"the noise floor left {name} pinned after the guard's teardown"
    )


def test_the_guard_restores_a_floored_level_rather_than_clearing_it(tmp_path) -> None:
    """RESTORE, not reset — the same rule the rest of this guard follows.

    A logger an OUTER fixture had already floored must come back at the level
    that fixture chose, not at NOTSET; blanket-clearing would re-introduce the
    review §5.5 defect (an outer fixture's state destroyed by an inner
    teardown) on the axis this commit added.
    """
    name = "guard_probe_vendor"
    management.apply_library_levels({name: "DEBUG"})
    try:
        with _logger_guard_round_trip():
            management.apply_library_levels({name: "ERROR"})
            assert logging.getLogger(name).level == logging.ERROR

        assert logging.getLogger(name).level == logging.DEBUG, (
            "the guard cleared a level its snapshot had recorded instead of restoring it"
        )
    finally:
        logging.getLogger(name).setLevel(logging.NOTSET)


def test_the_guards_state_snapshot_does_not_alias_its_mutable_fields(tmp_path) -> None:
    """``dataclasses.replace`` re-passes values BY REFERENCE.

    ``_LogConfig`` grew mutable collection fields with the noise floor, so a
    shallow snapshot silently adopts everything the test appends to them — and
    the restored state then describes a floor the guard did not restore.
    """
    before_floored = list(management._state.floored_loggers)
    before_overrides = dict(management._state.level_overrides)

    with _logger_guard_round_trip():
        management.apply_library_levels({"guard_alias_probe": "ERROR"})
        assert "guard_alias_probe" in management._state.floored_loggers

    assert management._state.floored_loggers == before_floored, (
        "the snapshot aliased floored_loggers — it adopted the test's appends"
    )
    assert management._state.level_overrides == before_overrides, (
        "the snapshot aliased level_overrides — it adopted the test's overrides"
    )


def test_the_guard_takes_the_cli_stream_proxies_back_out(tmp_path) -> None:
    """``sys.stdout``/``sys.stderr`` are process state the rebind of ``_state`` cannot reach.

    A test that runs the CLI path installs the stream proxies; the guard must
    put the streams it found back, and leave no record for the next install.
    """
    before = [sys.stdout, sys.stderr]

    with _logger_guard_round_trip():
        management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
        management.create_output_dir("run")
        # POSITIVE CONTROL: the proxies really were installed inside the window.
        assert isinstance(sys.stdout, management._QueuedStream)
        assert isinstance(sys.stderr, management._QueuedStream)

    assert [sys.stdout, sys.stderr] == before
    assert management._state.proxy_streams is False
    assert management._state.process_streams is None
    assert management._state.stream_proxies == {}
    assert management._state.dead_routes == {}


def test_the_next_install_after_the_guard_reads_the_process_streams_afresh(
    tmp_path, monkeypatch
) -> None:
    first, second = io.StringIO(), io.StringIO()
    monkeypatch.setattr(
        management, "_process_streams", lambda: management._ProcessStreams(first, first)
    )
    with _logger_guard_round_trip():
        management.init_cli_logging(xdir=tmp_path / "a", log_level="INFO", keep_days=7)
        management.create_output_dir("run")
        assert management._state.process_streams == management._ProcessStreams(first, first)

    monkeypatch.setattr(
        management, "_process_streams", lambda: management._ProcessStreams(second, second)
    )
    with _logger_guard_round_trip():
        management.init_cli_logging(xdir=tmp_path / "b", log_level="INFO", keep_days=7)
        management.create_output_dir("run")
        assert management._state.process_streams == management._ProcessStreams(second, second)


@pytest.fixture(scope="module")
def primed_bootstrap_cache() -> Iterator[object]:
    """Module-scoped discovery cache: a discovery memoized before any per-test snapshot."""
    saved = config_project._discovery.result
    sentinel = SimpleNamespace(projects=[], errors=[])
    config_project._discovery.result = sentinel
    yield sentinel
    config_project._discovery.result = saved


def _assert_bootstrap_state_survives_then_poison_it(sentinel: object) -> None:
    assert config_project._discovery.result is sentinel, (
        "a module fixture's discovery cache was cleared by a per-test teardown — "
        "the guard reset to defaults instead of restoring its snapshot"
    )
    # Poison for whichever sibling runs next: the guard must swap it back out.
    config_project._discovery.result = SimpleNamespace(projects=[], errors=[])


def test_bootstrap_state_outlives_a_sibling_test(primed_bootstrap_cache: object) -> None:
    _assert_bootstrap_state_survives_then_poison_it(primed_bootstrap_cache)


def test_bootstrap_state_outlives_another_sibling_test(primed_bootstrap_cache: object) -> None:
    _assert_bootstrap_state_survives_then_poison_it(primed_bootstrap_cache)


@pytest.fixture(scope="module")
def primed_completion_names() -> Iterator[dict]:
    """Module-scoped completion-cache snapshot, installed before any per-test snapshot."""
    saved = registry.get_completion_names()
    sentinel = {"__snapshot_restore_pin__": object()}
    registry.set_completion_names(sentinel)
    yield sentinel
    registry.set_completion_names(saved)


def _assert_completion_names_survive_then_poison_them(sentinel: dict) -> None:
    assert registry.get_completion_names() is sentinel, (
        "a module fixture's completion snapshot was cleared by a per-test teardown — "
        "the guard reset it instead of restoring its snapshot"
    )
    # Poison for whichever sibling runs next: the guard must swap it back out.
    registry.set_completion_names({"__poison__": True})


def test_the_completion_snapshot_outlives_a_sibling_test(primed_completion_names: dict) -> None:
    _assert_completion_names_survive_then_poison_them(primed_completion_names)


def test_the_completion_snapshot_outlives_another_sibling_test(
    primed_completion_names: dict,
) -> None:
    _assert_completion_names_survive_then_poison_them(primed_completion_names)


@pytest.fixture(scope="module")
def primed_catalog() -> Iterator[object]:
    """Module-scoped catalog: a complete result published before any per-test snapshot."""
    saved = dataclasses.replace(declared._composition)
    sentinel = SimpleNamespace(projects=[], ordered_projects=[])
    declared.publish_composed(sentinel, complete=True)
    yield sentinel
    declared._composition = saved


def _assert_catalog_survives_then_poison_it(sentinel: object) -> None:
    assert declared._composition.result is sentinel, (
        "a module fixture's published result was cleared by a per-test teardown — "
        "the guard reset the catalog instead of restoring its snapshot"
    )
    assert declared._composition.in_progress is None, (
        "a sibling's in-progress publication leaked forward"
    )
    # Poison for whichever sibling runs next: the guard must swap it back out.
    declared._composition.in_progress = SimpleNamespace(projects=[], ordered_projects=[])


def test_the_catalog_outlives_a_sibling_test(primed_catalog: object) -> None:
    _assert_catalog_survives_then_poison_it(primed_catalog)


def test_the_catalog_outlives_another_sibling_test(primed_catalog: object) -> None:
    _assert_catalog_survives_then_poison_it(primed_catalog)
