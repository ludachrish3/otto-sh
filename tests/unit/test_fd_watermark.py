"""Pins for the shared FD-watermark bracket (``tests/_fixtures/fd_watermark.py``).

Two halves. The behavior tests drive the bracket's generator body directly —
balanced fds pass, a leak past tolerance trips, garbage that only a
``gc.collect()`` would release is absorbed rather than blamed, and a
pre-bracket cycle cannot inflate the baseline into hiding a real leak. The
drift guard scans the three consumer conftests: each must import
``_fd_watermark`` from the authority and must NOT re-grow a local copy — the
exact drift this consolidation retired (review §5.5: the integration copy
skipped the baseline ``gc.collect()``, silently inflating its tolerance, while
its docstring claimed "Same shape as…").
"""

import ast
import gc
import os

import pytest

from tests._fixtures.fd_watermark import FD_TOLERANCE, fd_watermark_bracket, open_fd_count
from tests._fixtures.paths import PROJECT_ROOT

pytestmark = pytest.mark.hostless


def test_balanced_fds_pass() -> None:
    gen = fd_watermark_bracket()
    next(gen)
    fd = os.open(os.devnull, os.O_RDONLY)
    os.close(fd)
    with pytest.raises(StopIteration):
        next(gen)


def test_leak_past_tolerance_trips() -> None:
    gen = fd_watermark_bracket()
    next(gen)
    leaked = [os.open(os.devnull, os.O_RDONLY) for _ in range(FD_TOLERANCE + 1)]
    try:
        with pytest.raises(AssertionError, match="fd leak"):
            next(gen)
    finally:
        for fd in leaked:
            os.close(fd)


def test_baseline_gc_keeps_prior_garbage_out_of_the_tolerance() -> None:
    """Garbage predating the bracket must not inflate the baseline.

    The drifted integration copy skipped the baseline ``gc.collect()``
    (review §5.5): fds held by a previous test's uncollected cycles counted
    into ``before``, and when the bracket's post-yield collect freed them the
    slack hid a REAL leak of the same size. Reproduce that exactly — a
    pre-bracket cycle of ``FD_TOLERANCE + 1`` fds plus an equally-sized real
    leak inside the bracket — and require the bracket to TRIP: only a
    baseline taken after a collect makes the real leak visible.
    """

    class _FdHolder:
        def __init__(self) -> None:
            self.fd = os.open(os.devnull, os.O_RDONLY)
            self.me = self

        def __del__(self) -> None:
            os.close(self.fd)

    gc.disable()
    leaked: list[int] = []
    try:
        prior_cycle = [_FdHolder() for _ in range(FD_TOLERANCE + 1)]
        del prior_cycle  # unreachable, but only a collect frees the fds
        gen = fd_watermark_bracket()
        next(gen)
        leaked = [os.open(os.devnull, os.O_RDONLY) for _ in range(FD_TOLERANCE + 1)]
        with pytest.raises(AssertionError, match="fd leak"):
            next(gen)
    finally:
        gc.enable()
        for fd in leaked:
            os.close(fd)


def test_gc_only_garbage_is_absorbed_not_blamed() -> None:
    """FDs held only by an unreferenced reference cycle must not read as a leak.

    The bracket's job is to blame the TEST's descriptors, not the collector's
    timing: a post-yield ``gc.collect()`` frees the cycle's fds before the
    VERDICT is reached, so the bracket completes cleanly. Deliberately
    path-agnostic: the first post-yield collect and the retry arm are
    defense-in-depth for the same property (mutating either one away leaves
    the other absorbing the garbage — a behavior-preserving mutant, proven
    during Wave 14's mutation run), so this pins the property, not which
    pass pays for it.
    """

    class _FdHolder:
        """Holds one fd; closes it silently on finalization (no ResourceWarning,
        which the suite promotes to an error via the unraisable hook)."""

        def __init__(self) -> None:
            self.fd = os.open(os.devnull, os.O_RDONLY)
            self.me = self  # unreachable-by-refcount cycle (PEP 442 collectable)

        def __del__(self) -> None:
            os.close(self.fd)

    gc.disable()  # auto-gc between the probes below would vacuously pass this test
    try:
        gen = fd_watermark_bracket()
        next(gen)
        cycle = [_FdHolder() for _ in range(FD_TOLERANCE + 1)]
        baseline_probe = open_fd_count()
        del cycle
        # Positive control: the cycle's fds really are still open
        # (refcount alone can't free a cycle) …
        assert open_fd_count() == baseline_probe
        # … yet the bracket must absorb them via its gc pass instead of tripping.
        with pytest.raises(StopIteration):
            next(gen)
    finally:
        gc.enable()


# The three lanes whose conftests must consume the authority. A new
# leak-sensitive lane should be added here when it adopts the bracket.
_CONSUMERS = (
    "tests/e2e/tunnel_stability/conftest.py",
    "tests/e2e/chaos/conftest.py",
    "tests/integration/chaos/conftest.py",
)


@pytest.mark.parametrize("conftest_rel", _CONSUMERS)
def test_consumers_import_the_authority_and_own_no_copy(conftest_rel: str) -> None:
    tree = ast.parse((PROJECT_ROOT / conftest_rel).read_text())
    local_defs = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_fd_watermark"
    ]
    assert not local_defs, (
        f"{conftest_rel}:{local_defs} re-defines _fd_watermark — the hand-rolled copies "
        "drifted apart once already (review §5.5); import it from "
        "tests/_fixtures/fd_watermark.py instead"
    )
    imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "tests._fixtures.fd_watermark"
        and any(alias.name == "_fd_watermark" for alias in node.names)
    ]
    assert imports, (
        f"{conftest_rel} does not import _fd_watermark from "
        "tests/_fixtures/fd_watermark.py — the lane runs with no FD bracket"
    )
