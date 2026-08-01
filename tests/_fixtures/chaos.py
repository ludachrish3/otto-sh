"""Tier-1 chaos harness: deterministic cancellation/fault sweeps over teardown chains.

``sweep_cancellation()`` runs a scenario once to COUNT its chaos points
(instrumented await checkpoints inside fakes), then re-runs it once per
point per exception class, arming exactly one point each run. After every
armed run the caller's oracle asserts the chain's invariants — every
must-run step behind the injection point still executed, or the chain
failed loudly rather than silently skipping (chaos spec tier 1:
docs/superpowers/specs/2026-07-30-chaos-hardening-design.md).

Determinism: no wall-clock, no randomness — the injection point is an
integer counter. The ``CancelledError`` variant models a cancellation
landing at that await; ``ConnectionDropped`` models the transport dying
there. ``point()`` awaits ``asyncio.sleep(0)`` first so every instrumented
point is a REAL suspension point, keeping cancellation semantics honest.
"""

import asyncio
from collections.abc import Awaitable, Callable


class ConnectionDropped(Exception):  # noqa: N818 — no Error suffix per chaos spec design
    """Injected stand-in for a transport dying mid-call (tier-1 fault variant)."""


class ChaosPoints:
    """Counts instrumented checkpoints; arms at most one to raise.

    Fakes call ``await points.point("label")`` (or ``points.sync_point``
    for synchronous steps) everywhere a real implementation would touch the
    network. A run with nothing armed counts the points; sweep runs arm
    point *k* to raise the injected exception there instead of recording it.
    """

    def __init__(self) -> None:
        self.count = 0
        self.executed: "list[str]" = []
        self.tripped_at: "str | None" = None
        self._arm_at: "int | None" = None
        self._exc: "type[BaseException] | None" = None

    def arm(self, at: int, exc: "type[BaseException]") -> None:
        """Make checkpoint number *at* (1-based) raise *exc* instead of executing."""
        self._arm_at = at
        self._exc = exc

    def sync_point(self, label: str) -> None:
        """A synchronous checkpoint: counts, then trips or records."""
        self.count += 1
        if self._arm_at == self.count:
            assert self._exc is not None
            self.tripped_at = label
            raise self._exc(f"chaos injection at point {self.count} ({label})")
        self.executed.append(label)

    async def point(self, label: str) -> None:
        """An async checkpoint: a real suspension, then counts, then trips or records."""
        await asyncio.sleep(0)
        self.sync_point(label)


async def sweep_cancellation(
    scenario_factory: "Callable[[ChaosPoints], Awaitable[object]]",
    oracle: "Callable[[ChaosPoints, BaseException | None, type[BaseException], int], None]",
    *,
    exceptions: "tuple[type[BaseException], ...]" = (asyncio.CancelledError, ConnectionDropped),
) -> None:
    """Sweep a scenario: one baseline run to count points, then one run per (exception, point).

    *scenario_factory* must build FRESH fakes for the ``ChaosPoints`` it
    receives each call — state must never leak between runs. The baseline
    run (nothing armed) must complete without raising: a scenario that
    fails un-injected is a broken scenario, not a chaos finding.
    """
    baseline = ChaosPoints()
    await scenario_factory(baseline)
    n = baseline.count
    assert n > 0, "scenario exercised no chaos points — nothing to sweep"
    for exc_type in exceptions:
        for k in range(1, n + 1):
            points = ChaosPoints()
            points.arm(k, exc_type)
            outcome: "BaseException | None" = None
            try:
                await scenario_factory(points)
            except BaseException as e:  # noqa: BLE001 — the sweep records ANY outcome for the oracle
                outcome = e
            oracle(points, outcome, exc_type, k)
