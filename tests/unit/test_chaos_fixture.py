"""The tier-1 sweep harness itself: it must flag unguarded chains and pass guarded ones.

Toy chains only — the product chains get their own sweeps next to their
modules (test_connections_close.py, test_unix_host.py, test_context.py).
"""

import asyncio
import contextlib

import pytest

from otto.host.errors import HostCommandError
from otto.utils import WaitTimeoutError
from tests._fixtures.chaos import (
    DEFAULT_FAULTS,
    ChaosPoints,
    ConnectionDropped,
    Fault,
    Surface,
    sweep_cancellation,
)

_LABELS = ["a", "b", "c"]
_TRANSPORT_SURFACES = frozenset({Surface.NETWORK, Surface.COMMAND})


async def _guarded(points: ChaosPoints) -> None:
    """Every step guarded the way ``teardown_step`` guards: ``except Exception``.

    Not ``suppress(ConnectionDropped)``. The product catches ``Exception``
    (``otto/host/connections.py``), so a toy that names ONE fault stops
    modelling it the moment another fault is added — and would then report the
    harness broken when what actually changed was the toy.
    """
    for label in _LABELS:
        with contextlib.suppress(Exception):
            await points.point(label)


async def _unguarded(points: ChaosPoints) -> None:
    """The pre-fix shape: one raising step skips everything behind it."""
    for label in _LABELS:
        await points.point(label)


def _guarded_oracle(
    points: ChaosPoints, outcome: "BaseException | None", exc_type: type, k: int
) -> None:
    """Partition on what the GUARD catches, never on which fault was injected.

    ``teardown_step`` catches ``Exception``; ``CancelledError`` is a
    ``BaseException`` and propagates. Branching on ``exc_type is
    ConnectionDropped`` encoded a two-entry table instead, so every fault added
    after it landed in the cancellation arm by accident.
    """
    if issubclass(exc_type, Exception):
        # Guarded: the fault is swallowed, every later step still runs.
        assert outcome is None, f"{exc_type.__name__} at point {k} escaped the chain"
        assert points.executed == [s for i, s in enumerate(_LABELS) if i != k - 1], (
            f"steps after point {k} were skipped"
        )
    else:  # BaseException (cancellation): aborting loudly is the contract
        assert isinstance(outcome, exc_type), (
            f"cancellation at point {k} was swallowed — it must abort loudly"
        )
        assert points.executed == _LABELS[: k - 1]


async def _swallows_everything(points: ChaosPoints) -> None:
    """The shape that must NEVER pass: cancellation suppressed like a drop."""
    for label in _LABELS:
        with contextlib.suppress(BaseException):
            await points.point(label)


@pytest.mark.asyncio
async def test_sweep_fails_a_chain_that_eats_cancellation():
    """The loud arm must still bite: a chain that swallows cancellation is a finding.

    Without this, a generalization of the oracle that quietly stopped asserting
    anything about cancellation would look green.
    """
    with pytest.raises(AssertionError, match="cancellation"):
        await sweep_cancellation(_swallows_everything, _guarded_oracle)


@pytest.mark.asyncio
async def test_sweep_passes_a_guarded_chain():
    await sweep_cancellation(_guarded, _guarded_oracle)


@pytest.mark.asyncio
async def test_sweep_fails_an_unguarded_chain():
    with pytest.raises(AssertionError, match="escaped the chain"):
        await sweep_cancellation(_unguarded, _guarded_oracle)


@pytest.mark.asyncio
async def test_sweep_rejects_a_scenario_with_no_points():
    async def empty(points: ChaosPoints) -> None:
        pass

    with pytest.raises(AssertionError, match="no chaos points"):
        await sweep_cancellation(empty, _guarded_oracle)


@pytest.mark.asyncio
async def test_baseline_run_counts_and_records_everything():
    points = ChaosPoints()
    await _unguarded(points)
    assert points.count == 3
    assert points.executed == _LABELS
    assert points.tripped_at is None


# ---------------------------------------------------------------------------
# The applicability predicate (spec section 6): a fault declares where it is
# meaningful, a checkpoint declares what it stands for, the sweep crosses them.
# ---------------------------------------------------------------------------


async def _mixed_surfaces(points: ChaosPoints) -> None:
    """One point of each surface, all guarded, so only the COUNTS vary."""
    with contextlib.suppress(Exception):
        await points.point("any-step")
    with contextlib.suppress(Exception):
        await points.point("local-step", surface=Surface.LOCAL)
    with contextlib.suppress(Exception):
        await points.point("net-step", surface=Surface.NETWORK)


def _counting_oracle(
    points: ChaosPoints, outcome: "BaseException | None", exc_type: type, k: int
) -> None:
    """Asserts nothing: these tests are about which injections HAPPEN."""


@pytest.mark.asyncio
async def test_a_narrowed_fault_skips_the_points_it_cannot_reach():
    """The predicate, measured rather than asserted to exist.

    Mutation check: make ``Fault.applies_at`` return True unconditionally and
    connection-dropped injects 3 instead of 2; drop the ``Surface.ANY`` escape
    and it injects 1 instead of 2. Both directions go red here.
    """
    table = [
        Fault("cancellation", asyncio.CancelledError),
        Fault("connection-dropped", ConnectionDropped, frozenset({Surface.NETWORK})),
    ]
    report = await sweep_cancellation(_mixed_surfaces, _counting_oracle, faults=table)

    assert report.points == 3
    assert report.surfaces == [Surface.ANY, Surface.LOCAL, Surface.NETWORK]
    # Cancellation lands at every await — there is no such thing as an await
    # that cannot be cancelled, so this entry is universal by construction.
    assert report.injected["cancellation"] == 3
    assert report.skipped["cancellation"] == 0
    # The drop reaches the ANY point and the NETWORK point, never the LOCAL one.
    assert report.injected["connection-dropped"] == 2
    assert report.skipped["connection-dropped"] == 1


@pytest.mark.asyncio
async def test_a_universal_fault_cannot_be_narrowed_away():
    """A table that narrows cancellation is wrong by construction, and says so."""
    table = [Fault("cancellation", asyncio.CancelledError, frozenset({Surface.COMMAND}))]
    with pytest.raises(AssertionError, match="cancellation must apply at every point"):
        await sweep_cancellation(_mixed_surfaces, _counting_oracle, faults=table)


@pytest.mark.asyncio
async def test_a_scenario_that_changes_its_point_surfaces_is_rejected():
    """The predicate reads point k's surface from the BASELINE run, so a scenario
    whose shape differs between runs would be gated on a stale answer.
    Deterministic shape is a precondition; this makes it a checked one."""
    runs = {"n": 0}

    async def unstable(points: ChaosPoints) -> None:
        runs["n"] += 1
        first = Surface.NETWORK if runs["n"] == 1 else Surface.LOCAL
        with contextlib.suppress(Exception):
            await points.point("shifty", surface=first)

    with pytest.raises(AssertionError, match="surface changed between runs"):
        await sweep_cancellation(unstable, _counting_oracle)


# ---------------------------------------------------------------------------
# The default table (spec section 6): five exception-shaped faults.
# ---------------------------------------------------------------------------


async def _guarded_command_step(points: ChaosPoints) -> None:
    """One COMMAND-surface point, guarded — every fault in the table reaches it."""
    with contextlib.suppress(Exception):
        await points.point("cmd", surface=Surface.COMMAND)


@pytest.mark.asyncio
async def test_every_default_fault_is_actually_injected_not_merely_listed():
    """A fault in the table that reaches zero points asserts nothing at all.

    Listing is not injecting. Pinning the count means an entry whose surface
    set accidentally excludes every point cannot sit in the table looking like
    coverage.
    """
    report = await sweep_cancellation(_guarded_command_step, _counting_oracle)
    assert sorted(report.injected) == [
        "cancellation",
        "command-failure",
        "connection-dropped",
        "connection-reset",
        "timeout",
    ]
    assert all(count == 1 for count in report.injected.values()), report.injected


@pytest.mark.asyncio
async def test_the_drop_and_the_reset_are_not_the_same_fault():
    """Spec section 6 wants connection-reset DISTINCT from ConnectionDropped.

    ConnectionResetError is an OSError; ConnectionDropped deliberately is not.
    A chain guarding ``except OSError`` therefore swallows one and is torn by
    the other, which is the whole reason both belong in the table. Delete
    either entry and the pair stops discriminating.
    """
    seen: "list[str]" = []

    async def guards_only_oserror(points: ChaosPoints) -> None:
        with contextlib.suppress(OSError):
            await points.point("net", surface=Surface.NETWORK)

    def oracle(
        points: ChaosPoints, outcome: "BaseException | None", exc_type: type, k: int
    ) -> None:
        seen.append(f"{exc_type.__name__}:{'escaped' if outcome is not None else 'swallowed'}")

    table = [
        Fault("connection-dropped", ConnectionDropped, frozenset({Surface.NETWORK})),
        Fault("connection-reset", ConnectionResetError, frozenset({Surface.NETWORK})),
        Fault("cancellation", asyncio.CancelledError),
    ]
    await sweep_cancellation(guards_only_oserror, oracle, faults=table)
    assert "ConnectionDropped:escaped" in seen
    assert "ConnectionResetError:swallowed" in seen


# ---------------------------------------------------------------------------
# The report must count OBSERVED injections, and the shape check must mean
# what it claims. Both gaps were found in pre-squash review.
# ---------------------------------------------------------------------------


def _shape_shifter(counts: "list[int]"):
    """A scenario whose point count follows *counts*, run by run."""
    runs = {"n": 0}

    async def scenario(points: ChaosPoints) -> None:
        n = counts[min(runs["n"], len(counts) - 1)]
        runs["n"] += 1
        for i in range(n):
            with contextlib.suppress(Exception):
                await points.point(f"p{i}", surface=Surface.NETWORK)

    return scenario


@pytest.mark.asyncio
async def test_a_scenario_that_shrinks_after_the_baseline_is_rejected():
    """`injected` must count trips that HAPPENED, not runs that were armed.

    The counter increments before the armed run, so a scenario that records
    three points on the baseline and two afterwards leaves point 3 unreachable
    — never tripped — while the report still claims three injections. A
    prefix comparison cannot see it: a shorter run always prefix-matches.
    """
    with pytest.raises(AssertionError, match="never reached its armed point"):
        await sweep_cancellation(_shape_shifter([3, 2]), _counting_oracle)


@pytest.mark.asyncio
async def test_a_scenario_that_grows_after_the_baseline_is_rejected():
    """The mirror case: extra trailing points sit beyond the compared prefix."""
    with pytest.raises(AssertionError, match="not deterministic"):
        await sweep_cancellation(_shape_shifter([2, 3]), _counting_oracle)


@pytest.mark.asyncio
async def test_a_scenario_that_reorders_its_steps_is_rejected():
    """Same surfaces in a different order is still a shape change.

    Comparing surfaces alone cannot see it — every point here is NETWORK.
    """
    runs = {"n": 0}
    order = [["a", "b"], ["b", "a"]]

    async def reordering(points: ChaosPoints) -> None:
        labels = order[min(runs["n"], 1)]
        runs["n"] += 1
        for label in labels:
            with contextlib.suppress(Exception):
                await points.point(label, surface=Surface.NETWORK)

    with pytest.raises(AssertionError, match="not deterministic"):
        await sweep_cancellation(reordering, _counting_oracle)


@pytest.mark.asyncio
async def test_a_table_with_duplicate_fault_names_is_rejected():
    """Counters are keyed by name, so duplicates would silently collapse."""
    table = [
        Fault("dup", ConnectionDropped, frozenset({Surface.NETWORK})),
        Fault("dup", ConnectionResetError, frozenset({Surface.NETWORK})),
        Fault("cancellation", asyncio.CancelledError),
    ]
    with pytest.raises(AssertionError, match="duplicate fault name"):
        await sweep_cancellation(_mixed_surfaces, _counting_oracle, faults=table)


def test_the_default_table_pins_its_exception_classes_and_surfaces():
    """Pin the SHIPPED table, not just the classes' properties.

    Every other test either builds its own table or compares an outcome
    against `exc_type` drawn from the same table, which is self-referential:
    swapping connection-reset's class for ConnectionDropped leaves the whole
    suite green. This is the only test that would go red.
    """
    assert {f.name: (f.exc, f.surfaces) for f in DEFAULT_FAULTS} == {
        "cancellation": (asyncio.CancelledError, frozenset()),
        "connection-dropped": (ConnectionDropped, _TRANSPORT_SURFACES),
        "connection-reset": (ConnectionResetError, _TRANSPORT_SURFACES),
        "timeout": (WaitTimeoutError, _TRANSPORT_SURFACES),
        "command-failure": (HostCommandError, frozenset({Surface.COMMAND})),
    }


async def _guarded_local_then_command(points: ChaosPoints) -> None:
    """A LOCAL step and a COMMAND step, so OVER-application is detectable."""
    with contextlib.suppress(Exception):
        await points.point("local", surface=Surface.LOCAL)
    with contextlib.suppress(Exception):
        await points.point("cmd", surface=Surface.COMMAND)


@pytest.mark.asyncio
async def test_transport_faults_never_reach_a_local_step():
    """Without a LOCAL point under the DEFAULT table, `_TRANSPORT` gaining
    Surface.LOCAL would be undetectable suite-wide — every call site is
    NETWORK and the other fixture scenarios are ANY or COMMAND."""
    report = await sweep_cancellation(_guarded_local_then_command, _counting_oracle)
    assert report.points == 2
    assert report.injected["cancellation"] == 2  # universal, reaches LOCAL too
    for name in ("connection-dropped", "connection-reset", "timeout", "command-failure"):
        assert report.injected[name] == 1, name
        assert report.skipped[name] == 1, name
