"""The root state guards RESTORE pre-test state — they must not reset to defaults.

Pins Wave 14's conversion of ``_restore_otto_logger_state`` and
``_restore_bootstrap_state`` (root conftest) from reset-to-default teardowns
to snapshot-restore. The motivating defect (review §5.5): a module-scoped
fixture's logging/bootstrap state was destroyed by the reset after its FIRST
test, so every later test in that module silently ran against defaults.

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

import logging
from collections.abc import Iterator

import pytest

from otto import bootstrap
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


def test_logger_state_outlives_a_sibling_test(primed_logger_state: logging.Handler) -> None:
    _assert_logger_state_survives(primed_logger_state)


def test_logger_state_outlives_another_sibling_test(
    primed_logger_state: logging.Handler,
) -> None:
    _assert_logger_state_survives(primed_logger_state)


@pytest.fixture(scope="module")
def primed_completion_names() -> Iterator[dict]:
    """Module-scoped bootstrap cache: the completion-names fast-path snapshot."""
    saved = bootstrap.get_completion_names()
    sentinel = {"__snapshot_restore_pin__": object()}
    bootstrap.set_completion_names(sentinel)
    yield sentinel
    bootstrap.set_completion_names(saved)


def _assert_bootstrap_state_survives(sentinel: dict) -> None:
    assert bootstrap.get_completion_names() is sentinel, (
        "a module fixture's bootstrap cache was cleared by a per-test teardown — "
        "the guard reset to defaults instead of restoring its snapshot"
    )


def test_bootstrap_state_outlives_a_sibling_test(primed_completion_names: dict) -> None:
    _assert_bootstrap_state_survives(primed_completion_names)


def test_bootstrap_state_outlives_another_sibling_test(primed_completion_names: dict) -> None:
    _assert_bootstrap_state_survives(primed_completion_names)
