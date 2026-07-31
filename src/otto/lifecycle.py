"""The one blessed asyncio entry for otto commands: scope entry, interrupts, teardown.

Every command-path ``asyncio.run`` in otto goes through :func:`run_command` —
a unit guard (tests/unit/test_no_bare_asyncio_run.py) enforces this. It
enters the active ``OttoContext``'s host scope, so hosts opened during the
command are swept when the command's loop exits (sync command paths used to
skip this entirely), and owns the two-stage SIGINT/SIGTERM interrupt policy
(chaos spec: docs/superpowers/specs/2026-07-30-chaos-hardening-design.md).

Unit tests drive the state machine by calling ``_CommandRun._on_signal``
directly (``install_handlers=False``): installing real handlers in-process
would replace the test harness's chained SIGINT faulthandler. Real signal
delivery is exercised by the tier-2 subprocess tests (chaos plan 3).
"""

import asyncio
import contextlib
import signal
import sys
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

R = TypeVar("R")

DEFAULT_TEARDOWN_DEADLINE = 10.0
"""Fallback graceful-teardown bound (seconds) when ``OTTO_TEARDOWN_DEADLINE`` is unset."""

_force_exit_hooks: "list[Callable[[], None]]" = []


def register_force_exit_hook(hook: "Callable[[], None]") -> "Callable[[], None]":
    """Register *hook* to run if a command force-exits (teardown abandoned).

    Returns an unregister callable; unregistering twice is a no-op. Hooks are
    last-resort **local** restoration (e.g. termios state) — they run after
    the event loop has closed, so they must be synchronous and idempotent.
    """
    _force_exit_hooks.append(hook)

    def _unregister() -> None:
        with contextlib.suppress(ValueError):
            _force_exit_hooks.remove(hook)

    return _unregister


def _run_force_exit_hooks() -> None:
    """Run registered hooks newest-first; a failing hook never masks the exit."""
    for hook in reversed(list(_force_exit_hooks)):
        with contextlib.suppress(Exception):
            hook()


_INTERRUPT_STATUS_LINE = (
    "otto: interrupted — cleaning up remote sessions (interrupt again to abandon cleanup)"
)


class _InterruptedCommand(Exception):  # noqa: N818 — name fixed by the chaos-plan spec
    """Internal: the command was interrupted; carries signal + force flag out of the loop."""

    def __init__(self, signum: int, forced: bool) -> None:
        super().__init__(signum, forced)
        self.signum = signum
        self.forced = forced


class _ForcedAbandon(Exception):  # noqa: N818 — see _InterruptedCommand: interface-fixed name
    """Internal: the force event fired while awaiting a lifecycle stage."""


class _CommandRun:
    """Two-stage interrupt state machine for a single command invocation.

    First signal: cancel the body, announce, start the teardown deadline.
    Second signal or deadline expiry: set the force event — every lifecycle
    await races against it via :meth:`_race_force`, so the run abandons
    whatever it was waiting on. ``install_handlers=False`` is the tier-1 test
    mode: tests call :meth:`_on_signal` directly.
    """

    def __init__(self, *, teardown_deadline: float, install_handlers: bool = True) -> None:
        self.teardown_deadline = teardown_deadline
        self.install_handlers = install_handlers
        self.interrupted: "int | None" = None
        self.forced = False
        self._force = asyncio.Event()
        self._deadline_handle: "asyncio.TimerHandle | None" = None
        self._body: "asyncio.Task[Any] | None" = None

    def _on_signal(self, signum: int) -> None:
        """Loop-callback signal entry: first → graceful, repeat → force."""
        if self.interrupted is None:
            self.interrupted = signum
            if self._body is not None:
                self._body.cancel()
            self._deadline_handle = asyncio.get_running_loop().call_later(
                self.teardown_deadline, self._force.set
            )
            # Best-effort status line, emitted LAST: closed/broken stderr
            # (e.g. BrokenPipeError on a closed pipe) must never break
            # interrupt handling — cancellation and the deadline are already
            # committed above, so a raise here can't skip them.
            try:
                sys.stderr.write(_INTERRUPT_STATUS_LINE + "\n")
                sys.stderr.flush()
            except OSError:
                pass
        else:
            self._force.set()

    async def _race_force(self, aw: "Coroutine[Any, Any, Any] | asyncio.Task[Any]") -> Any:
        """Await *aw* unless the force event fires first.

        On force: mark forced and raise :class:`_ForcedAbandon`, leaving *aw*
        running — ``asyncio.run``'s finalization cancels it at loop close,
        which is the hard-abort path (transports close abruptly).
        """
        target = asyncio.ensure_future(aw)
        waiter = asyncio.ensure_future(self._force.wait())
        try:
            await asyncio.wait({target, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
        if target.done():
            return target.result()
        self.forced = True
        raise _ForcedAbandon

    async def _main(self, coro: "Coroutine[Any, Any, R]") -> R:
        from .context import try_get_context

        loop = asyncio.get_running_loop()
        installed: "list[int]" = []
        if self.install_handlers:
            for signum in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(signum, self._on_signal, signum)
                    installed.append(signum)
                except (NotImplementedError, RuntimeError):  # noqa: PERF203 — two-iteration loop, not a hot path
                    # Non-main thread or unsupported platform: default
                    # dispositions stay — exactly today's behavior.
                    break
        ctx = try_get_context()
        if ctx is not None:
            await ctx.scope.__aenter__()
        self._body = asyncio.ensure_future(coro)
        result: Any = None
        body_error: "BaseException | None" = None
        try:
            try:
                result = await self._race_force(self._body)
            except asyncio.CancelledError:
                if self.interrupted is None:
                    raise  # external cancellation, not our signal: propagate as-is
            except _ForcedAbandon:
                pass
            except BaseException as exc:  # noqa: BLE001 — body outcome deferred past the sweep
                body_error = exc
            if ctx is not None and not self.forced:
                with contextlib.suppress(_ForcedAbandon):
                    await self._race_force(ctx.scope.__aexit__(None, None, None))
        finally:
            if self._deadline_handle is not None:
                self._deadline_handle.cancel()
            for signum in installed:
                loop.remove_signal_handler(signum)
        if body_error is not None:
            raise body_error
        if self.interrupted is not None:
            raise _InterruptedCommand(self.interrupted, self.forced)
        return result  # type: ignore[no-any-return]  # None only reachable on the raise paths above


def _resolve_teardown_deadline() -> float:
    """``OTTO_TEARDOWN_DEADLINE`` via the typed env settings, else the default."""
    try:
        from .config import get_env

        return get_env().teardown_deadline
    except Exception:  # noqa: BLE001 — discovery unavailable (bare library use): fall back
        return DEFAULT_TEARDOWN_DEADLINE


def run_command(
    coro: "Coroutine[Any, Any, R]",
    *,
    teardown_deadline: "float | None" = None,
    _controller: "_CommandRun | None" = None,
) -> R:
    """Run *coro* as a command body under otto's lifecycle policy.

    Enters the active ``OttoContext``'s host scope (when one is installed) so
    hosts opened during the command are swept at loop exit. On interruption
    raises ``SystemExit(128 + signum)`` — 130 for SIGINT, 143 for SIGTERM —
    after the graceful sweep, or after abandoning it on a second signal /
    teardown-deadline expiry (then the registered force-exit hooks run, after
    the loop has closed). ``_controller`` is a test seam: tier-1 tests inject
    a ``_CommandRun(install_handlers=False)`` they hold a reference to.
    """
    deadline = _resolve_teardown_deadline() if teardown_deadline is None else teardown_deadline
    ctrl = _controller if _controller is not None else _CommandRun(teardown_deadline=deadline)
    try:
        return asyncio.run(ctrl._main(coro))  # noqa: SLF001 (_main is test-visible seam, required for tier-1 state machine testing)
    except _InterruptedCommand as exc:
        if exc.forced:
            _run_force_exit_hooks()
        raise SystemExit(128 + exc.signum) from None
