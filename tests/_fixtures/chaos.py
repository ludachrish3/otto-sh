"""Tier-1 chaos harness: deterministic cancellation/fault sweeps over teardown chains.

``sweep_cancellation()`` runs a scenario once to COUNT its chaos points
(instrumented await checkpoints inside fakes), then re-runs it once per
APPLICABLE (fault, point) pair, arming exactly one point each run — a fault
the point's surface excludes is skipped, not run. After every
armed run the caller's oracle asserts the chain's invariants — every
must-run step behind the injection point still executed, or the chain
failed loudly rather than silently skipping (chaos spec tier 1:
docs/superpowers/specs/2026-07-30-chaos-hardening-design.md).

Determinism: no wall-clock, no randomness — the injection point is an
integer counter. ``point()`` awaits ``asyncio.sleep(0)`` first so every
instrumented point is a REAL suspension point, keeping cancellation
semantics honest.

The table is :data:`DEFAULT_FAULTS`: cancellation (universal — any await can
be cancelled), the transport dying in two NON-interchangeable shapes
(``ConnectionDropped``, a bare ``Exception``, and ``ConnectionResetError``,
an ``OSError``, so a chain guarding ``except OSError`` swallows one and is
torn by the other), otto's own ``WaitTimeoutError``, and ``HostCommandError``
for a command that ran and reported failure. Each entry declares the
:class:`Surface` values where it is meaningful and each checkpoint declares
its surface, so a socket close is never asked to survive a command failure.

Phase 1 is exception-shaped faults ONLY. Data-shaped faults — truncated
reads, interleaved output, partial writes — mutate returned bytes rather than
raising and need oracle-aware handling; they are deferred by
docs/superpowers/specs/2026-08-22-test-strategy-and-unix-lab-rename-design.md
section 6, and this note is where that deferral is visible from the code.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum

from otto.host.errors import HostCommandError
from otto.utils import WaitTimeoutError


class ConnectionDropped(Exception):  # noqa: N818 — no Error suffix per chaos spec design
    """Injected stand-in for a transport dying mid-call (tier-1 fault variant)."""


class Surface(Enum):
    """What a checkpoint stands for, so a fault can say whether it belongs there.

    The fake declares this; the fault declares which surfaces it is meaningful
    at; the sweep crosses the two. ``ANY`` is the default precisely so that
    silence never narrows a sweep — a fake that says nothing gets the whole
    table, and shrinking coverage stays a deliberate, visible act.
    """

    ANY = "any"
    LOCAL = "local"
    NETWORK = "network"
    COMMAND = "command"


@dataclass(frozen=True)
class Fault:
    """One entry in the table: a name, an exception shape, and where it applies.

    *surfaces* empty means UNIVERSAL — meaningful at every checkpoint. That is
    the honest encoding for cancellation, which can land at any await at all.
    """

    name: str
    exc: "type[BaseException]"
    surfaces: "frozenset[Surface]" = frozenset()

    def applies_at(self, surface: Surface) -> bool:
        """The applicability predicate, over a point's declared surface.

        A point that declares ``ANY`` takes every fault: it has not claimed to
        be a narrow kind of step, so nothing may be skipped on its behalf.
        """
        if not self.surfaces:
            return True
        if surface is Surface.ANY:
            return True
        return surface in self.surfaces


@dataclass(frozen=True)
class SweepReport:
    """What a sweep actually did — returned so a call site can PIN it.

    A narrowed sweep asserts less than a full one, and the difference is
    invisible from a green result. Handing back per-fault counts is what lets
    a caller state "this chain skips command-failure, on purpose, at every
    point" instead of leaving a reader to assume it did not.

    ``injected`` counts trips that were OBSERVED, not runs that were armed:
    :func:`sweep_cancellation` asserts the armed point was actually reached
    before counting it.
    """

    points: int
    surfaces: "list[Surface]"
    injected: "dict[str, int]"
    skipped: "dict[str, int]"


_TRANSPORT = frozenset({Surface.NETWORK, Surface.COMMAND})

DEFAULT_FAULTS: "list[Fault]" = [
    # Universal: any await can be cancelled, so this entry carries no surfaces.
    Fault("cancellation", asyncio.CancelledError),
    # The transport dying, in the two shapes that are NOT interchangeable:
    # ConnectionDropped is a bare Exception, ConnectionResetError is an OSError,
    # so a chain guarding `except OSError` swallows one and is torn by the other.
    Fault("connection-dropped", ConnectionDropped, _TRANSPORT),
    Fault("connection-reset", ConnectionResetError, _TRANSPORT),
    # otto's own timeout: raised by otto.utils.wait_for / wait_for_async, and a
    # stdlib TimeoutError subclass, so it exercises `except TimeoutError` too.
    Fault("timeout", WaitTimeoutError, _TRANSPORT),
    # A command that RAN and reported failure — only meaningful where a command
    # runs, never at a socket close.
    Fault("command-failure", HostCommandError, frozenset({Surface.COMMAND})),
]


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
        self.labels: "list[str]" = []
        self.surfaces: "list[Surface]" = []
        self.tripped_at: "str | None" = None
        self._arm_at: "int | None" = None
        self._exc: "type[BaseException] | None" = None

    def arm(self, at: int, exc: "type[BaseException]") -> None:
        """Make checkpoint number *at* (1-based) raise *exc* instead of executing."""
        self._arm_at = at
        self._exc = exc

    def sync_point(self, label: str, *, surface: Surface = Surface.ANY) -> None:
        """A synchronous checkpoint: counts, records its surface, then trips or records.

        Label and surface are recorded BEFORE the trip: a point that raises
        still declared what it was, and the shape-stability check in
        :func:`sweep_cancellation` needs that record from armed runs too.
        """
        self.count += 1
        self.labels.append(label)
        self.surfaces.append(surface)
        if self._arm_at == self.count:
            assert self._exc is not None
            self.tripped_at = label
            raise self._exc(f"chaos injection at point {self.count} ({label})")
        self.executed.append(label)

    async def point(self, label: str, *, surface: Surface = Surface.ANY) -> None:
        """An async checkpoint: a real suspension, then counts, then trips or records."""
        await asyncio.sleep(0)
        self.sync_point(label, surface=surface)


async def sweep_cancellation(
    scenario_factory: "Callable[[ChaosPoints], Awaitable[object]]",
    oracle: "Callable[[ChaosPoints, BaseException | None, type[BaseException], int], None]",
    *,
    faults: "list[Fault] | None" = None,
) -> SweepReport:
    """Sweep a scenario: a baseline run to count points, then one run per applicable (fault, point).

    *scenario_factory* must build FRESH fakes for the ``ChaosPoints`` it
    receives each call — state must never leak between runs. The baseline
    run (nothing armed) must complete without raising: a scenario that
    fails un-injected is a broken scenario, not a chaos finding.

    Returns what was injected and what was skipped, so a caller that narrows a
    sweep can pin the narrowing instead of leaving it to be discovered.
    """
    table = DEFAULT_FAULTS if faults is None else faults
    names = [f.name for f in table]
    assert len(names) == len(set(names)), (
        f"duplicate fault name in table {names} — the injected/skipped counters "
        f"are keyed by name, so duplicates would silently collapse into one entry"
    )
    baseline = ChaosPoints()
    await scenario_factory(baseline)
    n = baseline.count
    assert n > 0, "scenario exercised no chaos points — nothing to sweep"

    injected = {f.name: 0 for f in table}
    skipped = {f.name: 0 for f in table}
    for fault in table:
        for k in range(1, n + 1):
            if not fault.applies_at(baseline.surfaces[k - 1]):
                skipped[fault.name] += 1
                continue
            injected[fault.name] += 1
            points = ChaosPoints()
            points.arm(k, fault.exc)
            outcome: "BaseException | None" = None
            try:
                await scenario_factory(points)
            except BaseException as e:  # noqa: BLE001 — the sweep records ANY outcome for the oracle
                outcome = e
            shape = list(zip(points.labels, points.surfaces, strict=True))
            base_shape = list(zip(baseline.labels, baseline.surfaces, strict=True))
            # Compare LABEL and surface, and reject growth outright. Surfaces
            # alone cannot see a reorder (every step may share one surface),
            # and a prefix comparison cannot see a run that grew a tail.
            assert len(shape) <= len(base_shape), (
                f"scenario is not deterministic: the armed run reached "
                f"{len(shape)} points, more than the baseline's {len(base_shape)} "
                f"(baseline {base_shape}, armed run {shape})"
            )
            assert shape == base_shape[: len(shape)], (
                f"scenario is not deterministic: a point's label or surface changed "
                f"between runs (baseline {base_shape}, armed run {shape})"
            )
            # The counter must record injections that HAPPENED. It is bumped
            # before the run, and a run that stopped short would otherwise be
            # counted as evidence -- which is exactly what the call sites pin.
            # On a deterministic scenario this cannot false-positive: points
            # 1..k-1 are injection-free, so point k is always reached.
            assert points.tripped_at is not None, (
                f"scenario never reached its armed point {k} of {n} — "
                f"`injected` would count an injection that did not happen "
                f"(reached {len(shape)} points: {shape})"
            )
            oracle(points, outcome, fault.exc, k)

    for fault in table:
        if issubclass(fault.exc, asyncio.CancelledError):
            assert injected[fault.name] == n, (
                f"cancellation must apply at every point: {fault.name} reached "
                f"{injected[fault.name]} of {n}. Any await can be cancelled, so a "
                f"table that narrows it is wrong by construction."
            )

    return SweepReport(
        points=n, surfaces=list(baseline.surfaces), injected=injected, skipped=skipped
    )
