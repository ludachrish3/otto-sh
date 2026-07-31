"""The raw-mode window registers a lifecycle force-exit hook (chaos plan 1)."""

import asyncio
import contextlib
import signal

import pytest

from otto import lifecycle
from otto.host import interact


def test_guard_registers_restore_hook_for_the_raw_mode_window(monkeypatch):
    calls: list[tuple[int, object]] = []
    monkeypatch.setattr(interact, "_restore_terminal", lambda fd, attrs: calls.append((fd, attrs)))
    before = len(lifecycle._force_exit_hooks)
    with interact._force_restore_guard(5, ["fake-attrs"]):
        assert len(lifecycle._force_exit_hooks) == before + 1
        lifecycle._run_force_exit_hooks()
        assert calls == [(5, ["fake-attrs"])]
    assert len(lifecycle._force_exit_hooks) == before  # unregistered on exit


def test_guard_is_inert_when_raw_mode_was_not_engaged():
    before = len(lifecycle._force_exit_hooks)
    with interact._force_restore_guard(5, None):
        assert len(lifecycle._force_exit_hooks) == before


def test_force_exit_hook_survives_forced_unwind_of_a_bridge_shaped_drain(monkeypatch):
    """Composing regression (final-review finding 1): a FORCED exit must
    still restore the terminal via the force-exit hook, even though the
    guarded body's own unwind is a bare ``CancelledError``.

    Shaped like ``_run_bridge``: the guard wraps a body whose ``finally``
    drains a never-resolving future inside ``asyncio.wait_for(asyncio.shield(...))``
    guarded by ``contextlib.suppress(asyncio.TimeoutError, Exception)`` — the
    same shape ``_run_bridge`` uses to drain its stdin-reader future.
    ``CancelledError`` is a ``BaseException``, not an ``Exception``, so it
    escapes that suppress. Two ``_on_signal`` calls drive the real two-stage
    interrupt policy: the first cancels the body (landing it in the drain
    await), the second sets the force event, causing ``_race_force`` to
    abandon the still-running body. ``asyncio.run``'s finalization then
    re-cancels that abandoned body while it sits in the drain await — the
    exact composing scenario from the review: a bare ``CancelledError``
    unwinds through ``_force_restore_guard`` at that point.

    Before the fix, the guard's plain ``finally: unregister()`` removed the
    hook as that unwind passed through it — *before* ``run_command`` reaches
    its post-loop ``_run_force_exit_hooks()`` call, so the hook it looked for
    was already gone and the terminal was never restored. After the fix, the
    guard keeps the hook registered across any exceptional unwind, so
    ``_run_force_exit_hooks()`` finds it and calls it.
    """
    restore_calls: list[tuple[int, object]] = []
    monkeypatch.setattr(
        interact, "_restore_terminal", lambda fd, attrs: restore_calls.append((fd, attrs))
    )
    ctrl = lifecycle._CommandRun(
        teardown_deadline=lifecycle.DEFAULT_TEARDOWN_DEADLINE, install_handlers=False
    )
    reached_drain = asyncio.Event()
    background: "list[asyncio.Task[None]]" = []  # keeps the fire-and-forget task referenced

    async def force_once_draining() -> None:
        """Deliver the second (force) signal only once the body is parked
        in the drain await — never a wall-clock guess."""
        await reached_drain.wait()
        ctrl._on_signal(signal.SIGINT)

    async def bridge_like_body() -> None:
        loop = asyncio.get_running_loop()
        background.append(asyncio.ensure_future(force_once_draining()))
        with interact._force_restore_guard(5, ["fake-attrs"]):
            asyncio.get_running_loop().call_soon(ctrl._on_signal, signal.SIGINT)
            try:
                await asyncio.Event().wait()  # cancelled by the first signal
            finally:
                # Mirrors _run_bridge's finally: a drain await on a future
                # that never resolves. The timeout (30s) never actually
                # elapses — the forced re-cancel below arrives first — it
                # only needs to be finite so this goes through wait_for's
                # real internal-waiter code path (timeout=None short-circuits
                # to a bare await, which wouldn't reproduce the bug).
                never_resolves: "asyncio.Future[None]" = loop.create_future()
                reached_drain.set()
                with contextlib.suppress(asyncio.TimeoutError, Exception):
                    await asyncio.wait_for(asyncio.shield(never_resolves), timeout=30)

    # The fix under test makes the guard deliberately KEEP the hook
    # registered across this exceptional unwind (that's the point) — and
    # _run_force_exit_hooks() only calls hooks, it never removes them. In
    # real usage the process exits right after, so the leak never outlives
    # the run; in-process here it would otherwise haunt every later test
    # that hits the forced-exit path. Snapshot/restore the process-global
    # list so this test's hook doesn't leak into the rest of the suite.
    before = len(lifecycle._force_exit_hooks)
    try:
        with pytest.raises(SystemExit) as exc_info:
            lifecycle.run_command(bridge_like_body(), _controller=ctrl)
        assert exc_info.value.code == 130
        assert ctrl.forced is True
        assert restore_calls == [(5, ["fake-attrs"])]  # via the hook, post-loop
        assert len(lifecycle._force_exit_hooks) == before + 1  # the leak this fix accepts
    finally:
        del lifecycle._force_exit_hooks[before:]
