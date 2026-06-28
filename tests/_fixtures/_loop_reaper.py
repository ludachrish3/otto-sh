"""Orphaned-event-loop reaper — pure logic, wired into the test session by
``tests/conftest.py``.

Background: ``filterwarnings = ["error"]`` (pyproject.toml) plus pytest's
``unraisableexception`` plugin turn a gc-finalized ``ResourceWarning: unclosed
event loop`` into a hard failure, attributed to *whichever* test happened to
trigger the ``gc.collect()`` (Hypothesis's ``register_random`` is a frequent
trigger) — not the test that leaked the loop. The leak source has been shown to
be exclusively pytest-asyncio's per-test function loop under an xdist teardown
race; ``otto/`` product code only ever creates loops via ``asyncio.run()``,
which always closes them, so a product loop never sits open at a test boundary.

This module closes leaked *harness* loops at the boundary (killing the flake
for all current and future harness leak sites) while refusing to mask a
*product* leak: a loop whose creation stack runs through ``otto/`` is reported,
never closed, so a genuine product regression fails loudly with attribution.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio


class LeakedProductLoopError(AssertionError):
    """Raised when an event loop created by ``otto/`` product code is found
    open at a test boundary — a real product resource leak that must not be
    masked by the harness reaper.
    """


def classify_loop_origin(stack_filenames: Iterable[str]) -> str:
    """Classify a loop's origin from its creation-stack filenames.

    ``"product"`` if any frame is in ``otto/`` source (so a leak of it must be
    surfaced, not swept); ``"harness"`` otherwise (pytest-asyncio / pytest /
    stdlib — safe to close).
    """
    for filename in stack_filenames:
        if "/otto/" in filename:
            return "product"
    return "harness"


def reap_orphan_loops(
    loops: Iterable["asyncio.AbstractEventLoop"],
    origin_of: Callable[["asyncio.AbstractEventLoop"], str],
) -> tuple[list["asyncio.AbstractEventLoop"], list["asyncio.AbstractEventLoop"]]:
    """Close orphaned harness loops; report (without closing) product ones.

    Returns ``(closed_harness, leaked_product)``. An already-closed or
    currently-running loop is left untouched.
    """
    closed_harness: list[asyncio.AbstractEventLoop] = []
    leaked_product: list[asyncio.AbstractEventLoop] = []
    for loop in loops:
        if loop.is_closed() or loop.is_running():
            continue
        if origin_of(loop) == "product":
            leaked_product.append(loop)
        else:
            loop.close()
            closed_harness.append(loop)
    return closed_harness, leaked_product


def reap_or_raise(
    loops: Iterable["asyncio.AbstractEventLoop"],
    origin_of: Callable[["asyncio.AbstractEventLoop"], str],
    *,
    describe: Callable[["asyncio.AbstractEventLoop"], str] = repr,
) -> int:
    """Close orphaned harness loops and return how many were reaped.

    Raises :class:`LeakedProductLoopError` (without closing them) if any
    ``otto/``-originated loop is found open — surfacing a product leak instead
    of masking it. ``describe`` renders each leaked loop for the message.
    """
    closed_harness, leaked_product = reap_orphan_loops(loops, origin_of)
    if leaked_product:
        details = "; ".join(describe(loop) for loop in leaked_product)
        raise LeakedProductLoopError(
            f"{len(leaked_product)} event loop(s) created by otto/ product code "
            f"were left open at a test boundary (never closed): {details}. "
            "This is a product resource leak, not a test-harness artifact — "
            "fix the source; do not let the loop reaper mask it."
        )
    return len(closed_harness)
