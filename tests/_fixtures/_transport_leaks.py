"""Leaked-asyncio-transport registry — pure logic, wired into the test session
by ``tests/conftest.py``.

Attributes leaked asyncio transports (the things that fire ``ResourceWarning``
from ``__del__`` at gc time, escalated by pytest's ``[unraisable]`` plugin onto
whichever *later* test happens to be running) to the test that created them.

This replaces the original whole-heap recipe — per-test ``gc.collect()`` plus a
``gc.get_objects()`` isinstance sweep — which was measured at 2.3-3.4x suite
CPU under ``OTTO_DETECT_ASYNCIO_LEAKS=1`` (roughly half the cost in the
collect, half in iterating the ~10^5-object heap per test). Instead, every
transport is recorded at its ``__init__`` chokepoint together with the test
running when it was built, mirroring the loop-origin tracker in
``tests/conftest.py``. The per-test check then iterates only the tracked
transports still alive — typically zero to a handful — and never calls into
``gc``. Attribution improves as a side effect: the registry knows which test
*created* a leaked transport, not merely which test's boundary first noticed
it. A transport kept alive only by a reference cycle is still caught: until
the cycle is collected, the object is alive and its weakref entry remains.

Known trade-off vs. the heap scan, and it is bigger than this file used to
claim: the old per-test ``gc.collect()`` also promptly finalized transports
that had become *unreferenced*, firing their warning inside the leaking test.
This registry cannot, because its keys are weak — a transport that nothing
references is collected before the next boundary, drops out of the registry,
and is attributed to nobody, while its ``ResourceWarning`` still lands on some
later test. That is not a hypothetical corner: it is exactly how the
exec-timeout leak fixed in ``dab13a7b`` behaved. Positive-controlled on
2026-08-09 by mutating that fix back out — the armed detector reported
nothing, and a deliberately blatant synthetic leak was equally invisible.

So a clean run under ``OTTO_DETECT_ASYNCIO_LEAKS=1`` is NOT proof that nothing
leaked. What the registry does catch, and catch with much better attribution
than a heap scan, is a leak whose transport stays *referenced* long enough to
be seen — how the ConnectionManager lazy-init race was found. The complementary
instrument for the unreferenced case is the FD watermark
(``tests/_fixtures/fd_watermark.py``), which counts descriptors and is
therefore immune to collection timing; ``tests/unit/host`` and the chaos lanes
run under it. Closing the gap here instead would need a per-transport
finalizer that outlives collection (detached by a wrapped ``close()``, with
surviving finalizers read as leaks) — deliberately not attempted.

Secondary limit, unchanged: ``scan_leaked_transports`` only reports transports
whose loop is already closed.
"""

import gc
import weakref
from collections.abc import Callable

# transport -> creating-test nodeid. Weak keys: a released transport drops out
# on its own, so the registry only ever holds live (or cycle-trapped)
# transports and the scan is O(live transports), not O(heap).
_TRANSPORT_INFO: "weakref.WeakKeyDictionary[object, str]" = weakref.WeakKeyDictionary()
_tracker_installed = False


def _uninstalled_current_test() -> str:
    return "(tracker not installed)"


_current_test_getter: Callable[[], str] = _uninstalled_current_test


def install_transport_tracker(current_test: Callable[[], str]) -> None:
    """Record every asyncio transport at creation, tagged with the creating test.

    Wraps ``_SelectorTransport.__init__`` and ``BaseSubprocessTransport.__init__``
    — the chokepoints every socket/pipe and subprocess transport passes through
    (subclasses all chain through them via ``super().__init__``). Idempotent;
    ``current_test`` is read through a module global at call time so it can be
    re-pointed (tests pin it to a sentinel).
    """
    global _current_test_getter, _tracker_installed  # noqa: PLW0603 — module-level singleton
    _current_test_getter = current_test
    if _tracker_installed:
        return
    from asyncio.base_subprocess import BaseSubprocessTransport
    from asyncio.selector_events import _SelectorTransport

    for cls in (_SelectorTransport, BaseSubprocessTransport):
        orig_init = cls.__init__

        def _tracking_init(self, *args, _orig=orig_init, **kwargs):
            _orig(self, *args, **kwargs)
            _TRANSPORT_INFO[self] = _current_test_getter()

        cls.__init__ = _tracking_init  # type: ignore[method-assign]
    _tracker_installed = True


def _would_warn(transport: object) -> bool:
    """Would ``__del__`` fire a ``ResourceWarning`` for this transport?

    ``_SelectorTransport.__del__`` warns while ``_sock`` is still set;
    ``BaseSubprocessTransport.__del__`` warns while ``_closed`` is False.
    """
    if getattr(transport, "_sock", None) is not None:
        return True
    return not getattr(transport, "_closed", True)


def scan_leaked_transports() -> list[tuple[object, str]]:
    """Find tracked transports that leaked: still open, loop already closed.

    Nothing can close such a transport any more, so its ``ResourceWarning`` is
    guaranteed to fire at some later gc point — the misattributed-unraisable
    flake this detector exists to explain. Returns ``(transport, description)``
    pairs so the reporter can dig further (e.g. :func:`describe_referrers`).
    Cheap by contract: iterates only live tracked transports and never calls
    into ``gc`` (``tests/unit/test_transport_leaks.py`` enforces this).
    """
    leaks = []
    for transport, creator in list(_TRANSPORT_INFO.items()):
        loop = getattr(transport, "_loop", None)
        if loop is None or not loop.is_closed():
            continue
        if not _would_warn(transport):
            continue
        leaks.append((transport, f"{transport!r} (created during {creator})"))
    return leaks


def describe_referrers(transport: object) -> str:
    """Summarize who is keeping ``transport`` alive (leak-path diagnostics only)."""
    return ", ".join(
        f"{type(r).__module__}.{type(r).__name__}" for r in gc.get_referrers(transport)[:5]
    )
