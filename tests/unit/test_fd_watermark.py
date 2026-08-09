"""Pins for the shared FD-watermark bracket (``tests/_fixtures/fd_watermark.py``).

Two halves. The behavior tests drive the bracket's generator body directly —
balanced fds pass, a leak past tolerance trips, garbage that only a
``gc.collect()`` would release is absorbed rather than blamed, and a
pre-bracket cycle cannot inflate the baseline into hiding a real leak. The
drift guard scans every consumer conftest in ``_CONSUMERS``: each must import
``_fd_watermark`` from the authority and must NOT re-grow a local copy — the
exact drift this consolidation retired (review §5.5: the integration copy
skipped the baseline ``gc.collect()``, silently inflating its tolerance, while
its docstring claimed "Same shape as…").
"""

import ast
import gc
import os

import pytest

from tests._fixtures import fd_watermark as fdw
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


def test_zero_tolerance_sees_a_single_descriptor() -> None:
    """The verdict boundary is where the arithmetic says it is.

    ``after <= before + tolerance`` means a tolerance of ONE still passes a
    one-descriptor leak, which is why ``tests/unit/host`` runs at zero. Pinned
    both ways with the same single descriptor: green at 1, red at 0.

    The descriptor here is a bare ``os.open`` held by a live local, i.e. the
    RETAINED class this bracket actually covers. It is deliberately not a
    stand-in for the unclosed-transport flake — that one is collectable, and
    the pre-verdict ``gc.collect()`` releases it before the count is taken
    (measured; see the module docstring of the authority).
    """
    for tolerance, should_trip in ((1, False), (0, True)):
        gen = fd_watermark_bracket(tolerance)
        next(gen)
        fd = os.open(os.devnull, os.O_RDONLY)
        try:
            if should_trip:
                with pytest.raises(AssertionError, match="fd leak"):
                    next(gen)
            else:
                with pytest.raises(StopIteration):
                    next(gen)
        finally:
            os.close(fd)


def test_on_suspicion_policy_does_not_collect_when_the_count_is_flat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole reason the unit lane can bracket every test for free.

    ``"always"`` pays two collects per test whether or not the test touches a
    descriptor — 3.3x on ``tests/unit/host``. ``"on-suspicion"`` pays none on
    the flat path, which is 1424 of that directory's 1426 tests. If a future
    edit reintroduces an unconditional collect the cost comes back silently,
    so count the calls rather than trust the branch.
    """
    calls = []
    monkeypatch.setattr(fdw.gc, "collect", lambda *a, **k: calls.append(1))

    gen = fd_watermark_bracket(0, gc_policy="on-suspicion")
    next(gen)
    with pytest.raises(StopIteration):
        next(gen)
    assert calls == [], f"flat boundary collected {len(calls)} time(s)"

    # Positive control for the same instrument: the eager policy DOES collect,
    # so an empty list above means "did not collect", not "did not measure".
    gen = fd_watermark_bracket(0)
    next(gen)
    with pytest.raises(StopIteration):
        next(gen)
    assert len(calls) == 2, f"eager policy collected {len(calls)} time(s), expected 2"


def test_on_suspicion_policy_collects_before_blaming_a_test() -> None:
    """Skipping the collects must not turn collector timing into a red build.

    The fast path's cheapness is only sound if suspicion still escalates: a
    raw count over tolerance has to trigger a collect and a re-read before the
    verdict, or every cycle-held descriptor becomes a false leak. Same
    construction as the eager test above — a cycle only a collect can free —
    but through the policy that skips the unconditional passes.
    """

    class _FdHolder:
        def __init__(self) -> None:
            self.fd = os.open(os.devnull, os.O_RDONLY)
            self.me = self

        def __del__(self) -> None:
            os.close(self.fd)

    gc.disable()
    try:
        gen = fd_watermark_bracket(0, gc_policy="on-suspicion")
        next(gen)
        cycle = [_FdHolder() for _ in range(3)]
        del cycle
        with pytest.raises(StopIteration):
            next(gen)
    finally:
        gc.enable()


# The lanes whose conftests must consume the authority. A new leak-sensitive
# lane should be added here when it adopts the bracket.
_CONSUMERS = (
    "tests/e2e/tunnel_stability/conftest.py",
    "tests/e2e/chaos/conftest.py",
    "tests/integration/chaos/conftest.py",
    # Not a chaos lane like the three above: tests/unit/host is where the real
    # subprocess spawning lives, so it is where an unclosed transport is most
    # likely to be born (dab13a7b was). Adopted 2026-08-09.
    "tests/unit/host/conftest.py",
)


@pytest.mark.parametrize("conftest_rel", _CONSUMERS)
def test_consumers_import_the_authority_and_own_no_copy(conftest_rel: str) -> None:
    """A consumer may adopt the bracket either way, but never re-implement it.

    Two legitimate shapes. The chaos/stability lanes import the authority's
    ``_fd_watermark`` fixture wholesale, bracketing every test in the lane.
    ``tests/unit/host`` instead defines a local ``_fd_watermark`` that
    DELEGATES to ``fd_watermark_bracket`` for a measured subset of modules,
    because there the bracket is worth its two gc passes only on the tests that
    can actually spawn (see that conftest). Importing the fixture there would
    re-arm it for all 1426 tests, which is the cost being avoided.

    What both shapes must share is that the collect-baseline-collect-verdict
    body lives in the authority. A local definition is therefore allowed only
    if it calls ``fd_watermark_bracket``; a body that re-grows the logic is the
    drift this guard exists to stop (review §5.5).
    """
    tree = ast.parse((PROJECT_ROOT / conftest_rel).read_text())
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "tests._fixtures.fd_watermark"
        for alias in node.names
    }
    local_defs = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_fd_watermark"
    ]
    for node in local_defs:
        delegates = any(
            isinstance(stmt, ast.YieldFrom)
            and isinstance(stmt.value, ast.Call)
            and isinstance(stmt.value.func, ast.Name)
            and stmt.value.func.id == "fd_watermark_bracket"
            for stmt in ast.walk(node)
        )
        assert delegates, (
            f"{conftest_rel}:{node.lineno} re-defines _fd_watermark without a "
            "`yield from fd_watermark_bracket(...)` — the hand-rolled copies drifted apart "
            "once already (review §5.5); drive the authority's generator instead of "
            "re-growing its body"
        )
        # Merely CALLING the authority is not delegation, which is why the
        # check above insists on ``yield from``: `next(fd_watermark_bracket())`
        # followed by a bare `yield` runs the baseline and then throws the
        # generator away, bracketing nothing while reading as a consumer.
        autouse = any(
            keyword.arg == "autouse"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for decorator in node.decorator_list
            if isinstance(decorator, ast.Call)
            for keyword in decorator.keywords
        )
        assert autouse, (
            f"{conftest_rel}:{node.lineno} defines _fd_watermark without autouse=True, so "
            "the lane runs with no bracket unless a test asks for one by name — which none "
            "do. The imported-fixture shape carries autouse in the authority; a local one "
            "has to declare it"
        )
    assert "_fd_watermark" in imported or local_defs, (
        f"{conftest_rel} neither imports _fd_watermark from "
        "tests/_fixtures/fd_watermark.py nor defines a delegating one — the lane runs "
        "with no FD bracket"
    )
    if local_defs:
        assert "fd_watermark_bracket" in imported, (
            f"{conftest_rel} defines a local _fd_watermark but does not import "
            "fd_watermark_bracket from the authority"
        )
