"""The tier-1 sweep harness itself: it must flag unguarded chains and pass guarded ones.

Toy chains only — the product chains get their own sweeps next to their
modules (test_connections_close.py, test_unix_host.py, test_context.py).
"""

import asyncio
import contextlib

import pytest

from tests._fixtures.chaos import ChaosPoints, ConnectionDropped, sweep_cancellation

_LABELS = ["a", "b", "c"]


async def _guarded(points: ChaosPoints) -> None:
    """Every step individually guarded against a drop — the shape Task 2 builds."""
    for label in _LABELS:
        with contextlib.suppress(ConnectionDropped):
            await points.point(label)


async def _unguarded(points: ChaosPoints) -> None:
    """The pre-fix shape: one raising step skips everything behind it."""
    for label in _LABELS:
        await points.point(label)


def _guarded_oracle(
    points: ChaosPoints, outcome: "BaseException | None", exc_type: type, k: int
) -> None:
    if exc_type is ConnectionDropped:
        assert outcome is None, f"drop at point {k} escaped the chain"
        assert points.executed == [s for i, s in enumerate(_LABELS) if i != k - 1], (
            f"steps after point {k} were skipped"
        )
    else:  # CancelledError: aborting loudly is the contract — nothing runs after, nothing hides
        assert isinstance(outcome, asyncio.CancelledError)
        assert points.executed == _LABELS[: k - 1]


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
