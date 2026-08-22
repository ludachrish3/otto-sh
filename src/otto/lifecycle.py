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
delivery is exercised by the tier-2 subprocess tests in
tests/integration/chaos/, which signal a real otto process over a loopback
sshd rather than calling the handler.
"""

import asyncio
import contextlib
import logging
import os
import select
import signal
import sys
import threading
from collections.abc import Callable, Coroutine, Iterator
from typing import Any, TypeVar, overload

R = TypeVar("R")

logger = logging.getLogger(__name__)

DEFAULT_TEARDOWN_DEADLINE = 10.0
"""Fallback graceful-teardown bound (seconds) when ``OTTO_TEARDOWN_DEADLINE`` is unset."""

_force_exit_hooks: "list[Callable[[], None]]" = []


def register_force_exit_hook(hook: "Callable[[], None]") -> "Callable[[], None]":
    """Register *hook* to run if a command force-exits (teardown abandoned).

    Returns an unregister callable; unregistering twice is a no-op. Hooks are
    last-resort **local** restoration (e.g. termios state) — they run after
    the event loop has closed, so they must be synchronous and idempotent.
    They may also run on :func:`sync_phase`'s force-path watchdog thread
    while the MAIN thread is suspended or wedged at an arbitrary bytecode
    boundary, possibly holding stdio, logging, or import locks — so hooks
    must acquire no lock the main thread could hold: os-level calls only
    (``os.write``), no stdio ``print``, no ``logging``, no imports.
    """
    _force_exit_hooks.append(hook)

    def _unregister() -> None:
        with contextlib.suppress(ValueError):
            _force_exit_hooks.remove(hook)

    return _unregister


_FORCE_FLUSH_JOIN = 2.0
"""Bound (seconds) on the forced exit's log flush: the force path trades
buffered log lines for a guaranteed exit, never the other way around."""

_FORCE_AFTER_DELIVERIES = 2
"""Signal deliveries that trip the force path: the first arms the teardown
deadline, the second stops waiting. The two-stage policy's whole content."""


# Deliberately NOT rooted on otto.errors.OttoError: re-parenting onto an
# Exception-rooted base would make this catchable by `except Exception`,
# breaking the 128 + signum signal contract. It is the documented exception
# to the OttoError rule.
class SyncPhaseInterrupt(KeyboardInterrupt):
    """``KeyboardInterrupt`` raised by :func:`sync_phase`'s own handler.

    A subclass so pytest and user teardown code observe a plain
    ``KeyboardInterrupt``, while callers can recover WHICH signal fired —
    including for the irreducible entry/exit windows where the raise escapes
    ``sync_phase`` itself rather than the phase body, so the
    ``128 + signum`` exit contract holds there too.
    """

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


class SyncPhaseGuard:
    """State handed back by :func:`sync_phase`; read after the phase returns.

    ``interrupted_signum`` is ``None`` for an undisturbed phase, else the
    first signal received (``signal.SIGINT`` / ``signal.SIGTERM``) — the
    caller exits ``128 + interrupted_signum`` to match :func:`run_command`'s
    contract.
    """

    def __init__(self, bound: float, what: str, shutdown_listener: "Callable[[], None]") -> None:
        # Everything the signal handler and the watchdog need is resolved
        # HERE, in normal execution context: a signal handler interrupts the
        # main thread at an arbitrary bytecode boundary, possibly while it
        # holds the import lock, a logging handler lock, or a stdio buffer
        # lock — so handler code below may not import, may not use the
        # logging module, and may not print. (Empirically found: a second
        # SIGINT landing inside the phase's own print() made a printing
        # force path raise reentrant-IO and lose its output; an import
        # inside a handler can deadlock the force path outright, which would
        # defeat the guard entirely.)
        self._bound = bound
        self._shutdown_listener = shutdown_listener
        self._notice = (
            f"\notto: interrupt received — finishing {what} teardown "
            f"(bounded, {bound:g}s); interrupt again to force-exit\n"
        ).encode()
        self.interrupted_signum: "int | None" = None
        self._closed = False
        # Created by _start(); an inert guard (install_handlers=False) never
        # owns a pipe or a thread.
        self._wake_r = -1
        self._wake_w = -1
        self._owns_wakeup_fd = False
        self._watchdog: "threading.Thread | None" = None

    def _start(self) -> None:
        """Spawn the watchdog BEFORE any handler can fire (normal context).

        The self-pipe is the only handler-to-watchdog channel: ``os.write``
        is genuinely async-signal-safe, while every in-process alternative
        (``Event.set``, ``Timer.start``, ``Lock.acquire``) can block on an
        internal lock the interrupted frame already holds — a same-thread
        deadlock of the exact mechanism whose one job is "always gets out".

        The pipe ALSO becomes the interpreter's signal-wakeup fd, but only if
        nothing else owns it — see ``_watch`` for what that buys and
        ``_claim_wakeup_fd`` for why the offer is conditional.
        """
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_w, False)  # set_wakeup_fd requires it
        self._watchdog = threading.Thread(
            target=self._watch, name="otto-sync-phase-watchdog", daemon=True
        )
        self._watchdog.start()
        self._claim_wakeup_fd()

    def _claim_wakeup_fd(self) -> None:
        """Take the signal-wakeup fd, but ONLY if it is unowned.

        The wakeup fd is process-global: exactly one owner per interpreter,
        and no way to ask who that is — reading it means setting it. So
        taking it unconditionally is an API change to every other component
        in the process, however local the diff looks. asyncio owns it for as
        long as a loop has signal handlers installed
        (``asyncio/unix_events.py``), and an owner that loses it simply stops
        being woken by signals; ``_watch`` measures what that costs a guard,
        and there is no reason to inflict it on anyone else.

        So the claim is an offer, not a seizure. Standalone ``otto`` — the
        case that actually needs it, a synchronous command with no loop —
        finds the fd unowned and gets the stronger force path. Where an owner
        exists it is handed straight back and the guard runs on the
        handler-written bytes alone, exactly as it always did. The probe
        itself is destructive for the two statements it spans; that window is
        unavoidable with this API and is the reason the fd is claimed once,
        at entry, rather than polled.

        ``warn_on_full_buffer=False``: a full pipe means the watchdog already
        has more than enough to force on, so a dropped byte changes nothing
        and the warning would be noise on an already-interrupted stderr.
        """
        with contextlib.suppress(ValueError, OSError):
            prior = signal.set_wakeup_fd(self._wake_w, warn_on_full_buffer=False)
            if prior == -1:
                self._owns_wakeup_fd = True
                return
            signal.set_wakeup_fd(prior)  # occupied — put it back untouched

    def _wake(self, byte: bytes) -> None:
        with contextlib.suppress(OSError):
            os.write(self._wake_w, byte)

    def _read_wake(self, timeout: "float | None") -> "bytes | None":
        """Next wake byte; ``b''`` on EOF (write end closed), ``None`` on timeout."""
        ready, _, _ = select.select([self._wake_r], [], [], timeout)
        if not ready:
            return None
        return os.read(self._wake_r, 1)

    def _watch(self) -> None:
        """Watchdog thread: owns the force path end to end.

        Runs in ordinary thread context — never signal-handler context — so
        the force path cannot self-deadlock against a lock held by the frame
        a signal interrupted. It still assumes the MAIN thread may be wedged
        at an arbitrary point holding arbitrary locks, so everything on the
        force path is os-level or explicitly bounded (the listener flush is
        claim-then-join with a timeout; see
        ``logger.management.shutdown_listener``). ``os._exit`` is
        deliberate: nothing else reliably preempts a wedged synchronous
        teardown (there is no loop to cancel), and it skips atexit, hence
        the explicit listener flush.
        """
        # TWO CHANNELS REPORTING THE SAME DELIVERIES, and the loop believes
        # whichever has counted more. The handler writes its b"A"/b"F"
        # opcodes; the interpreter writes each signal's NUMBER as it arrives,
        # whenever this guard holds the wakeup fd (`_claim_wakeup_fd`). Both
        # tick once per delivered signal, so `max` is the true count — and it
        # stays true if a channel goes silent, which is the whole point.
        # Opcode bytes cannot be mistaken for signal numbers: "A"/"C"/"F" are
        # 65/67/70 and Linux has no signal above 64.
        #
        # The wakeup channel exists because THE HANDLER MAY NEVER RUN, which
        # is what the 2026-08-08 investigation found, in the exact case this
        # guard exists for. Signals pending in one dispatch pass are handled
        # in SIGNAL-NUMBER order, not arrival order, so SIGINT (2) always
        # precedes SIGTERM (15); the first handler RAISES, which ends that
        # pass early and leaves the other signal tripped but undispatched;
        # and the phase then wedges in a blocking call (`time.sleep`, `join`,
        # `read`) that reaches no eval checkpoint where the stranded handler
        # could catch up. No force byte was ever written, so the guard sat
        # out its whole teardown deadline before forcing — measured 52/60 in
        # a standalone reproduction, and the live shape behind
        # `test_mixed_signal_pair_forces_regardless_of_order` failing ~7% of
        # coverage runs. A wakeup byte is in the pipe before any of that
        # dispatch logic is reached.
        #
        # The handler channel exists because THE WAKEUP FD CAN BE TAKEN AWAY
        # MID-PHASE, and silently: asyncio claims it in `add_signal_handler`
        # and hands back -1 rather than the previous owner when the loop
        # closes, so one async command run inside a phase (`otto test` runs a
        # whole pytest session inside one) permanently ends our side of that
        # channel. Counting instead of switching modes is what makes that a
        # non-event — the opcodes were arriving all along.
        #
        # Foreign signals ride the same fd while this phase owns it, so
        # anything that is not SIGINT/SIGTERM is skipped rather than counted:
        # a terminal resize must never force-exit a command.
        watched = {int(signal.SIGINT), int(signal.SIGTERM)}
        by_handler = by_wakeup = 0
        first_signum: "int | None" = None
        armed = False
        while True:
            byte = self._read_wake(self._bound if armed else None)
            if self._closed or byte in (b"", b"C"):  # phase completed in time
                os.close(self._wake_r)
                return
            if byte is None:
                break  # deadline expiry after arming; unreachable before it
            if byte in (b"A", b"F"):
                by_handler += 1
            elif byte[0] in watched:
                by_wakeup += 1
                if first_signum is None:
                    first_signum = byte[0]
            else:
                continue  # someone else's signal: not ours to count
            if max(by_handler, by_wakeup) >= _FORCE_AFTER_DELIVERIES:
                break  # a second signal, on either channel: force now
            armed = True  # the graceful-teardown deadline starts here
        # The handler's record is the authoritative "which signal was first",
        # and the exit code names the first. It is None only when no handler
        # ever ran, which is exactly when the wakeup channel has the answer.
        signum = self.interrupted_signum
        if signum is None:
            signum = first_signum if first_signum is not None else signal.SIGINT
        _run_force_exit_hooks()
        with contextlib.suppress(Exception):
            self._shutdown_listener()
        # _wake_r is deliberately left open here: this branch never returns,
        # and os._exit reclaims every fd (uniform ownership: the watchdog
        # closes the read end only on its retire paths).
        os._exit(128 + signum)

    def _close(self) -> None:
        """Retire a started watchdog; called from ``sync_phase``'s finally."""
        # Release the wakeup fd FIRST, and only if this guard is STILL its
        # owner: no further signal may write into a pipe that is about to
        # close, and b"C" must be the last thing the watchdog sees. Taking it
        # back is a read-modify-write because the C API has no query — and
        # the check is not ceremony, because ownership can change under us
        # (asyncio takes it in `add_signal_handler`), and clearing someone
        # else's registration would break their signal delivery exactly the
        # way an unconditional claim broke ours. Restores -1 rather than a
        # remembered number: the claim only happens when it WAS -1, and
        # writing back a stale fd could hand the interpreter one that has
        # since been closed and reused.
        if self._owns_wakeup_fd:
            with contextlib.suppress(ValueError, OSError):
                current = signal.set_wakeup_fd(-1)
                if current != self._wake_w:
                    signal.set_wakeup_fd(current)  # not ours any more — put it back
            self._owns_wakeup_fd = False
        self._wake(b"C")
        with contextlib.suppress(OSError):
            os.close(self._wake_w)
        # ident-check: if Thread.start() itself raised, joining the
        # never-started thread would raise and mask the original error.
        if self._watchdog is not None and self._watchdog.ident is not None:
            self._watchdog.join(timeout=5.0)

    def _on_signal(self, signum: int, _frame: Any) -> None:
        # Handler-reentrancy-safe — the operative criterion for a CPython
        # signal handler (which runs at a bytecode boundary on the main
        # thread, NOT in POSIX async-signal context): acquire no lock the
        # interrupted frame could hold. Attribute writes, os.write to the
        # self-pipe/stderr, and raise — nothing else.
        if self._closed:
            # sync_phase is unwinding (or has left): the restored prior
            # handler owns any later signal. Dropping this one beats raising
            # into the caller's finally blocks.
            return
        # The wake byte is written unconditionally, even while the guard also
        # owns the wakeup fd and the delivery is therefore already in the
        # pipe twice. The watchdog counts the two channels separately and
        # takes the larger, so the duplicate is free — and paying for it
        # keeps this channel live for a phase whose wakeup fd gets taken away
        # mid-flight, which asyncio does without giving it back.
        #
        # Which is not the same as this channel being sufficient: THIS
        # FUNCTION IS NOT GUARANTEED TO RUN. A signal pending alongside one
        # whose handler raises can stay undispatched until an eval checkpoint
        # that a wedged teardown never reaches. Hence two channels.
        if self.interrupted_signum is None:
            self.interrupted_signum = signum
            self._wake(b"A")  # start the watchdog's deadline countdown
            with contextlib.suppress(OSError):
                os.write(2, self._notice)
            # Deliver the phase's own graceful-teardown path (pytest unwinds
            # fixtures on KeyboardInterrupt); SIGTERM maps to the same path.
            raise SyncPhaseInterrupt(signum)
        self._wake(b"F")  # second signal: the watchdog force-exits at once


@contextlib.contextmanager
def sync_phase(
    *,
    deadline: "float | None" = None,
    what: str = "synchronous phase",
    install_handlers: bool = True,
) -> "Iterator[SyncPhaseGuard]":
    """Two-stage SIGINT/SIGTERM policy for a synchronous phase.

    The sibling of :func:`run_command` for phases that own their own event
    loops (the in-process pytest session): the first signal raises
    ``KeyboardInterrupt`` (:class:`SyncPhaseInterrupt`) into the phase — its
    graceful teardown — and arms the teardown deadline
    (``OTTO_TEARDOWN_DEADLINE`` unless *deadline* is given); a second signal,
    or the deadline expiring, makes a watchdog thread run the force-exit
    hooks, flush the log listener (bounded), and ``os._exit(128 + signum)``.
    The watchdog is spawned at entry in normal context so the force path
    never executes in signal-handler context, where a single lock
    acquisition can deadlock against the interrupted frame.

    ``install_handlers=False`` yields an inert guard — no handlers, no
    watchdog. This is the library/test seam (mirroring ``_CommandRun``'s),
    and what callers on a non-main thread must use: signal handlers are a
    main-thread-only facility, and installing (the default) off the main
    thread raises ``RuntimeError``.

    Handlers are restored on exit and the watchdog retired. Two irreducible
    one-bytecode windows exist at entry and exit where the guard's handler
    is live but the ``with`` frame cannot observe the raise; the escaping
    :class:`SyncPhaseInterrupt` carries the signal number so callers can
    still honor ``128 + signum``. A phase completing at the same instant
    its deadline expires may still take the force path — same exit code,
    hooks are idempotent, harmless.
    """
    from .logger.management import shutdown_listener

    bound = _resolve_teardown_deadline() if deadline is None else deadline

    def _bounded_flush() -> None:
        # Resolved to a concrete callable here, in normal context; only the
        # watchdog calls it, and only on the force path.
        shutdown_listener(join_timeout=_FORCE_FLUSH_JOIN)

    guard = SyncPhaseGuard(bound, what, _bounded_flush)
    if not install_handlers:
        yield guard
        return
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("sync_phase() must be entered from the main thread")
    prior: "dict[int, Any]" = {}
    try:
        guard._start()  # noqa: SLF001 — sync_phase owns its guard's lifecycle
        for sig in (signal.SIGINT, signal.SIGTERM):
            prior[sig] = signal.signal(sig, guard._on_signal)  # noqa: SLF001
        yield guard
    finally:
        # Order matters: close the guard's ears first (making _on_signal a
        # no-op), THEN restore handlers — built incrementally above, so an
        # entry-window raise restores exactly what was installed — THEN
        # retire the watchdog: cancel-last means a signal that armed it
        # microseconds earlier is still caught by the closed check.
        guard._closed = True  # noqa: SLF001
        for sig, handler in prior.items():
            signal.signal(sig, handler)
        guard._close()  # noqa: SLF001


def _run_force_exit_hooks() -> None:
    """Run registered hooks newest-first; a failing hook never masks the exit."""
    for hook in reversed(list(_force_exit_hooks)):
        with contextlib.suppress(Exception):
            hook()


class _CompensationExpired(Exception):  # noqa: N818 — internal control-flow signal, never leaves compensate()
    """Internal: :func:`compensate`'s always-armed *timeout* fired first."""


def _mark_expired(expiry: "asyncio.Future[None]") -> None:
    """Timer callback for *timeout*: SIGNAL the bound, never touch the work.

    Deliberately not ``task.cancel``: see :func:`_shielded_or_expired` for
    why cancelling the work from a timer makes the expiry indistinguishable
    from the caller's own interrupt.
    """
    if not expiry.done():
        expiry.set_result(None)


async def _shielded_or_expired(
    task: "asyncio.Task[R]", expiry: "asyncio.Future[None] | None"
) -> "R":
    """Await *task* under a shield; raise :class:`_CompensationExpired` if *expiry* fires first.

    Without *expiry* this IS the plain shielded await :func:`compensate` has
    always done, statement for statement — the bound-free path is not
    re-implemented, it is the first branch.

    With one, the shield and the expiry signal are raced through
    :func:`asyncio.wait`, which never reports a CHILD's outcome as an
    exception. So a ``CancelledError`` leaving this coroutine means the
    CALLER was cancelled and nothing else, and expiry arrives as its own
    exception rather than as a cancellation. That distinction is the whole
    reason the bound is not a simple ``call_later(timeout, task.cancel)``:
    cancelling the work under a shield raises ``CancelledError`` in the
    awaiting frame too, and before 3.11's ``Task.cancelling`` there is no way
    to tell that from an interrupt the caller sent — which would leave
    :func:`compensate` either inventing a cancellation for a caller who never
    asked for one, or swallowing a real one.

    The shield wrapper is dropped on every exit but the one that consumed it,
    exactly as a bare ``await asyncio.shield(task)`` does when cancelled:
    cancelling the wrapper is also what marks the inner task's eventual
    exception retrieved.

    On the expiry side the TASK, not the wrapper, decides whether there was
    anything left to abandon — see the second branch below for the
    one-callback-hop window that makes the difference.
    """
    if expiry is None:
        return await asyncio.shield(task)
    shielded = asyncio.shield(task)
    try:
        await asyncio.wait({shielded, expiry}, return_when=asyncio.FIRST_COMPLETED)
        if shielded.done():
            # A compensation that resolves in the same turn its bound expires
            # is honored, not abandoned: result, failure, or the inner task's
            # own cancellation, all raised/returned as the bound-free path
            # would.
            return shielded.result()
        if task.done():
            # SAME RULE, ONE CALLBACK HOP LATER, and the wrapper cannot be
            # trusted to say so: ``Future.set_result`` schedules done-callbacks
            # with ``call_soon``, so the expiry signal can reach
            # ``asyncio.wait``'s waiter while the shield wrapper's own callback
            # is STILL QUEUED over work that has already finished. Reading the
            # wrapper alone then reports a completed compensation as abandoned
            # and throws its result away — reproduced deterministically at a
            # two-hop offset on 3.10 and 3.14 (a rollback that yields twice
            # before returning). The task is the authority on whether the work
            # is done; the wrapper is only how we wait for it.
            return task.result()
        raise _CompensationExpired
    finally:
        if not shielded.done():
            shielded.cancel()


# Two overloads for one runtime signature: the ``None`` in the return type is
# the *timeout* expiry's answer (see the docstring), so it exists only for
# callers that opt in — a caller that passes no bound still gets ``R``, and the
# static contract of every pre-existing call site is unchanged.
@overload
async def compensate(
    coro: "Coroutine[Any, Any, R]",
    *,
    deadline: "float | None" = ...,
    timeout: None = ...,
    what: str = ...,
) -> "R": ...


@overload
async def compensate(
    coro: "Coroutine[Any, Any, R]",
    *,
    deadline: "float | None" = ...,
    timeout: float,
    what: str = ...,
) -> "R | None": ...


async def compensate(
    coro: "Coroutine[Any, Any, R]",
    *,
    deadline: "float | None" = None,
    timeout: "float | None" = None,
    what: str = "compensating action",
) -> "R | None":
    """Run a rollback/undo coroutine to completion even if the caller is cancelled.

    The chaos spec's shielded-compensating-action helper: an interrupt
    landing mid-compensation must not tear it (a half-run rollback is worse
    than none). Cancellation arriving while *coro* runs is HELD — the inner
    work continues under ``asyncio.shield`` — and re-raised once the
    compensation resolves. With no cancellation this is a plain await —
    results and exceptions pass through unchanged. Once a cancellation is
    held it wins over a late compensation failure (the failure is logged,
    the cancellation re-raises).

    TWO BOUNDS, AND THEY ARE NOT ALTERNATIVES — they differ in WHEN THE CLOCK
    STARTS, which is what the two names say:

    *deadline* is TEARDOWN PRESSURE. It is armed by the FIRST HELD
    CANCELLATION and by nothing else (``None`` resolves
    ``OTTO_TEARDOWN_DEADLINE``): with nobody interrupting, a compensating
    action is allowed to take as long as it takes, because a rollback torn
    off half-run is worse than a slow one. A caller that passes neither bound
    gets exactly that, forever.

    *timeout* is the CALLER'S OWN BOUND on the work, counted from the call
    (the sense :func:`asyncio.wait_for` gives the word) and armed whether or
    not anyone ever interrupts. It is opt-in for the same reason *deadline*
    is conditional: it is right only where the compensating action is itself
    best-effort AND its healthy cost is known — a single remote ``rm`` (the
    shell backend's interrupted-put cleanup), a listener reap (the nc
    backend's) — so that truncating it beats holding teardown. Do not give it
    to a rollback that must not be left half-run.

    With BOTH set the earlier expiry wins, and neither restarts: *timeout*
    counts from the call and *deadline* from the interrupt, so a *timeout* no
    larger than *deadline* makes *deadline* dead weight, and a second
    interrupt re-enters the shielded await without rearming either timer.

    A TIE GOES TO THE WORK: a compensation that has finished when its bound
    is observed is honored — result, failure or cancellation — never reported
    abandoned, at every scheduling offset between the two events: the task,
    not the shield wrapper, is what gets asked, because the wrapper's own
    done-callback can still be a hop behind the work.

    ON EXPIRY, either bound: the inner task is cancelled and joined, so
    nothing outlives this call, and the abandonment is logged at WARNING
    naming *what* — a compensating action that did not finish is
    operator-visible state (a temp left on a device, a rollback half-applied),
    whatever the caller does next. The OUTCOME follows the caller's own: a
    held cancellation still re-raises, so an interrupted caller keeps its
    interrupt; with no cancellation held — reachable only under *timeout* —
    the call RETURNS ``None`` instead of inventing a ``CancelledError`` for a
    caller who never asked for one. Hence the ``R | None`` return: the
    ``None`` is the "bound fired, nothing to report" answer, and it exists
    only for callers that opt in.

    Tier-1 determinism: both expiries are ``call_later`` timers — tests drive
    them with ``deadline=0`` / ``timeout=0`` (fires on the next loop turn),
    never wall-clock waits.
    """
    task = asyncio.ensure_future(coro)
    held: "asyncio.CancelledError | None" = None
    deadline_timer: "asyncio.TimerHandle | None" = None
    timeout_timer: "asyncio.TimerHandle | None" = None
    expiry: "asyncio.Future[None] | None" = None
    if timeout is not None:
        loop = asyncio.get_running_loop()
        expiry = loop.create_future()
        timeout_timer = loop.call_later(timeout, _mark_expired, expiry)
    try:
        while True:
            try:
                result = await _shielded_or_expired(task, expiry)
            except _CompensationExpired:
                # The caller's own bound, and the one path that reaches here
                # with the work still RUNNING: cancel it, then join it, so a
                # compensation this call gave up on cannot outlive the call.
                task.cancel()
                logger.warning(f"otto: {what} abandoned before completion")
                try:
                    # gather(return_exceptions=True), so the join neither
                    # raises the work's own failure nor leaves it unretrieved;
                    # a cancellation landing HERE is the caller's, and is held
                    # like any other.
                    await asyncio.gather(task, return_exceptions=True)
                except asyncio.CancelledError as exc:
                    if held is None:
                        held = exc
                if held is not None:
                    raise held from None
                return None
            except asyncio.CancelledError as exc:
                if not task.cancelled():
                    # OUR wrapper was cancelled, not the work: hold the
                    # cancellation, keep the work running, bound it.
                    if held is None:
                        held = exc
                        bound = _resolve_teardown_deadline() if deadline is None else deadline
                        deadline_timer = asyncio.get_running_loop().call_later(bound, task.cancel)
                    continue
                # The deadline (or loop shutdown) cancelled the work itself.
                logger.warning(f"otto: {what} abandoned before completion")
                if held is not None:
                    raise held from None
                raise
            except Exception:
                if held is not None:
                    logger.warning(f"otto: {what} failed during shielded unwind", exc_info=True)
                    raise held from None
                raise
            if held is not None:
                raise held from None
            return result
    finally:
        for timer in (deadline_timer, timeout_timer):
            if timer is not None:
                timer.cancel()


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
            except (OSError, ValueError):
                # Best-effort: a broken pipe raises OSError, a CLOSED file
                # object raises ValueError. Either way the cancellation and
                # deadline above are already committed; diagnostics must not
                # unwind a signal callback.
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
            except asyncio.CancelledError as exc:
                if self.interrupted is None:
                    # External task-level cancellation, not our signal (an
                    # embedder cancelled the command task — unreachable from
                    # the POSIX main-thread CLI, where cancellation only
                    # arrives via _on_signal). The cancel hit OUR await, not
                    # necessarily the body's: cancel and join the body, then
                    # let the sweep below run; the cancellation re-raises
                    # after it (body_error path).
                    #
                    # Arm the deadline BEFORE the join (final-review Finding
                    # 2): a body whose cancellation UNWIND hangs (catches the
                    # first cancel and parks again) must not stall this
                    # embedder's cancellation forever — nothing else arms
                    # the force event when no signal was seen. Racing the
                    # join via _race_force bounds it exactly like the signal
                    # path: on expiry _race_force marks ``forced`` and raises
                    # _ForcedAbandon, which the sweep's own ``not
                    # self.forced`` guard below then also skips, matching
                    # force semantics.
                    #
                    # suppress(BaseException), not CancelledError (final-
                    # review Finding 4): a body that answers cancellation
                    # with a DIFFERENT exception must not let that exception
                    # propagate PAST the sweep — cancel wins. Whatever the
                    # join raises (the body's own translated exception, or
                    # _ForcedAbandon on deadline expiry) is discarded here;
                    # the external cancellation captured in body_error below
                    # is what ultimately propagates, and only AFTER the
                    # sweep has had its chance to run.
                    self._deadline_handle = asyncio.get_running_loop().call_later(
                        self.teardown_deadline, self._force.set
                    )
                    self._body.cancel()
                    with contextlib.suppress(BaseException):
                        await self._race_force(self._body)
                    body_error = exc
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
