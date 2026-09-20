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


class LeakedRunningLoopError(AssertionError):
    """Raised when a test ends with an event loop still registered as *running*
    on its thread — the state that makes a later, innocent test die with
    ``RuntimeError: Cannot run the event loop while another loop is running``
    (issue #381). The raise names the test that left it, not the victim.
    """


class LeakedProductLoopError(AssertionError):
    """Raised when an event loop created by ``otto/`` product code is found
    open at a test boundary — a real product resource leak that must not be
    masked by the harness reaper.
    """


def classify_loop_origin(stack_filenames: Iterable[str]) -> str:
    """Classify a loop's origin from its creation-stack filenames.

    ``"browser"`` for Playwright's sync-API dispatcher loop; ``"product"`` if
    any frame is in ``otto/`` source (so a leak of it must be surfaced, not
    swept); ``"harness"`` otherwise (pytest-asyncio / pytest / stdlib — safe to
    close).

    ``"browser"`` is deliberately not ``"harness"`` (and never outranks
    ``"product"``):
    Playwright's sync API runs ``loop.run_until_complete`` inside a greenlet
    and parks it there, and asyncio's running-loop slot is *thread*-local, not
    greenlet-local. So for the whole life of the Playwright context that loop
    reads as running on the main thread — by design, and nothing the harness
    leaked. :func:`running_loop_leak_reason` allows exactly that one origin.

    Other main-thread ``asyncio.run(...)`` call sites under ``tests/e2e``
    (``test_tunnel_e2e.py``, ``chaos/conftest.py``, ``test_link_impair_e2e.py``)
    would die the same way if they were ever scheduled after a browser test on
    the same xdist worker; today they live in bed lanes that never share a
    worker with the dashboard lane.
    """
    browser = False
    for filename in stack_filenames:
        # ``/otto/`` wins outright: a product loop created on a stack that
        # happens to pass through a Playwright frame must still be classified
        # "product", or the guard would exempt it AND the reaper would close it
        # instead of raising ``LeakedProductLoopError``.
        if "/otto/" in filename:
            return "product"
        if "/playwright/" in filename:
            browser = True
    return "browser" if browser else "harness"


def running_loop_leak_reason(
    loop: "asyncio.AbstractEventLoop | None",
    origin: str,
    *,
    describe: Callable[["asyncio.AbstractEventLoop"], str] = repr,
) -> str | None:
    """Why *loop*, still registered as running at a test boundary, is a leak.

    Returns ``None`` when nothing leaked (no running loop) or when the running
    loop is Playwright's dispatcher (``origin == "browser"``), which is running
    by design for the whole browser session. Otherwise returns the message for
    :class:`LeakedRunningLoopError`.
    """
    if loop is None or origin == "browser":
        return None
    return (
        f"an event loop is still registered as RUNNING on this thread at the "
        f"test boundary: {describe(loop)} (origin: {origin}). asyncio's "
        "running-loop slot is thread-local, so every later test on this worker "
        "that drives a loop of its own dies with 'Cannot run the event loop "
        "while another loop is running' — a failure attributed to the victim, "
        "not to here (issue #381). Drive the loop on a thread this test owns, "
        "or unregister it before returning."
    )


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
