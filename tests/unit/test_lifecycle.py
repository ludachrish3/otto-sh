"""Tier-1 tests for otto.lifecycle (chaos spec, plan 1).

No real signals anywhere in this module: loop.add_signal_handler would
replace the chained SIGINT faulthandler tests/conftest.py installs, and
remove_signal_handler restores SIG_DFL, not the chain. The state machine is
driven by calling _CommandRun._on_signal directly (install_handlers=False).
Real delivery is tier 2 (subprocess tests, chaos plan 3). Everything here
counts work — no wall-clock waits.
"""

import asyncio
import signal

import pytest

from otto.config.lab import Lab
from otto.context import OttoContext, reset_context, set_context, try_get_context
from otto.lifecycle import (
    DEFAULT_TEARDOWN_DEADLINE,
    _CommandRun,
    _force_exit_hooks,
    _InterruptedCommand,
    _run_force_exit_hooks,
    register_force_exit_hook,
    run_command,
)


class _FakeHost:
    """Minimal RemoteHost stand-in: identity, _connected, counted close()."""

    def __init__(self, host_id: str = "h") -> None:
        self.id = host_id
        self._connected = True
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        self._connected = False


@pytest.fixture
def ctx():
    """Install a real OttoContext (empty lab) for the duration of a test."""
    context = OttoContext(lab=Lab(name="t"))
    token = set_context(context)
    yield context
    reset_context(token)


def _controller(deadline: float = DEFAULT_TEARDOWN_DEADLINE) -> _CommandRun:
    """Build a tier-1 controller: never installs real signal handlers.

    Post state-machine, the default (no ``_controller``) ``run_command`` path
    installs real SIGINT/SIGTERM handlers for the duration of the call. Every
    test in this module — including the bare ``run_command(...)`` calls below
    that predate the state machine — must inject one of these instead: one
    add/remove cycle of a real handler permanently disarms conftest's chained
    SIGINT faulthandler for the rest of the pytest worker (``remove_signal_handler``
    restores ``default_int_handler``, never the chain it replaced).
    """
    return _CommandRun(teardown_deadline=deadline, install_handlers=False)


def test_run_command_returns_result_without_context():
    async def body() -> int:
        return 41 + 1

    assert try_get_context() is None
    assert run_command(body(), _controller=_controller()) == 42


def test_run_command_propagates_body_exception():
    async def body() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        run_command(body(), _controller=_controller())


def test_run_command_sweeps_scope_registered_hosts(ctx):
    host = _FakeHost()

    async def body() -> None:
        ctx.scope.register(host)

    run_command(body(), _controller=_controller())
    assert host.close_calls == 1


def test_run_command_sweeps_scope_even_when_body_raises(ctx):
    host = _FakeHost()

    async def body() -> None:
        ctx.scope.register(host)
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        run_command(body(), _controller=_controller())
    assert host.close_calls == 1


def test_sequential_run_commands_do_not_reclose_swept_hosts(ctx):
    """suite/run.py runs several run_commands per command invocation."""
    host = _FakeHost()

    async def opens() -> None:
        ctx.scope.register(host)

    async def empty() -> None:
        pass

    run_command(opens(), _controller=_controller())
    run_command(empty(), _controller=_controller())
    assert host.close_calls == 1


def test_force_exit_hook_register_unregister_and_run():
    calls: list[str] = []
    before = len(_force_exit_hooks)
    unregister = register_force_exit_hook(lambda: calls.append("ran"))
    assert len(_force_exit_hooks) == before + 1
    _run_force_exit_hooks()
    assert calls == ["ran"]
    unregister()
    assert len(_force_exit_hooks) == before
    unregister()  # idempotent
    assert len(_force_exit_hooks) == before


def test_force_exit_hooks_isolate_errors():
    calls: list[str] = []

    def bad() -> None:
        raise RuntimeError("hook boom")

    u1 = register_force_exit_hook(bad)
    u2 = register_force_exit_hook(lambda: calls.append("ran"))
    try:
        _run_force_exit_hooks()
    finally:
        u1()
        u2()
    assert calls == ["ran"]


def test_default_teardown_deadline_is_ten_seconds():
    """Deferred Task-2 review finding: nothing else pins this constant."""
    assert DEFAULT_TEARDOWN_DEADLINE == 10.0


def test_force_exit_hooks_run_newest_first():
    """Deferred Task-2 review finding: ordering was claimed but never pinned."""
    order: list[str] = []
    u1 = register_force_exit_hook(lambda: order.append("older"))
    u2 = register_force_exit_hook(lambda: order.append("newer"))
    try:
        _run_force_exit_hooks()
    finally:
        u1()
        u2()
    assert order == ["newer", "older"]


class _SlowCloseHost:
    """Fake host whose close() blocks until the test releases it."""

    def __init__(self) -> None:
        self.id = "slow"
        self._connected = True
        self.close_started = asyncio.Event()
        self.release = asyncio.Event()
        self.close_calls = 0
        self.close_finished = 0

    async def close(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        await self.release.wait()
        self.close_finished += 1
        self._connected = False


@pytest.mark.parametrize(
    ("signum", "expected_code"),
    [(signal.SIGINT, 130), (signal.SIGTERM, 143)],
)
def test_first_signal_cancels_body_sweeps_and_exits_128_plus_signum(
    ctx, capsys, signum, expected_code
):
    host = _FakeHost()
    ctrl = _controller()

    async def body() -> None:
        ctx.scope.register(host)
        asyncio.get_running_loop().call_soon(ctrl._on_signal, signum)
        await asyncio.Event().wait()  # cancelled by the handler

    with pytest.raises(SystemExit) as exc_info:
        run_command(body(), _controller=ctrl)
    assert exc_info.value.code == expected_code
    assert host.close_calls == 1  # graceful sweep ran
    assert ctrl.forced is False
    assert "cleaning up" in capsys.readouterr().err


def test_second_signal_during_teardown_abandons_sweep_and_runs_hooks(ctx):
    host = _SlowCloseHost()
    hook_ran: list[bool] = []
    unregister = register_force_exit_hook(lambda: hook_ran.append(True))
    ctrl = _controller()
    background: "list[asyncio.Task[None]]" = []  # keeps fire-and-forget tasks referenced

    async def second_signal_when_sweep_starts() -> None:
        await host.close_started.wait()
        ctrl._on_signal(signal.SIGINT)

    async def body() -> None:
        ctx.scope.register(host)
        background.append(asyncio.ensure_future(second_signal_when_sweep_starts()))
        asyncio.get_running_loop().call_soon(ctrl._on_signal, signal.SIGINT)
        await asyncio.Event().wait()

    try:
        with pytest.raises(SystemExit) as exc_info:
            run_command(body(), _controller=ctrl)
    finally:
        unregister()
    assert exc_info.value.code == 130
    assert ctrl.forced is True
    assert host.close_calls == 1
    assert host.close_finished == 0  # sweep abandoned mid-close
    assert hook_ran == [True]


@pytest.mark.asyncio
async def test_first_signal_schedules_deadline_and_expiry_forces():
    """Deadline expiry and a second signal funnel into the same force event —
    asserted by scheduling shape (call_later handle), not by waiting."""
    ctrl = _controller(deadline=1234.0)
    started = asyncio.Event()

    async def body() -> None:
        started.set()
        await asyncio.Event().wait()

    main = asyncio.ensure_future(ctrl._main(body()))
    await started.wait()
    ctrl._on_signal(signal.SIGINT)
    loop = asyncio.get_running_loop()
    assert ctrl._deadline_handle is not None
    # A countdown, so it does shrink under load — but what it asserts is that
    # the handle was SCHEDULED (the docstring's "by scheduling shape, not by
    # waiting"), and breaching either end needs the whole 1234 s deadline to
    # elapse inside the test, which pytest-timeout ends long before. Not a
    # discriminator on elapsed work, so not a serial_timing member.
    assert 0 < ctrl._deadline_handle.when() - loop.time() <= 1234.0
    ctrl._force.set()  # what the deadline callback would do
    with pytest.raises(_InterruptedCommand) as exc_info:
        await main
    assert exc_info.value.signum == signal.SIGINT
    # Body died to cancellation before the force, so nothing was abandoned
    # mid-flight; forced may be False here — the exit-code path is what the
    # sync-level tests pin. What matters: the handle was scheduled with the
    # configured deadline and expiry unblocked the run.


def test_signal_during_teardown_of_successful_body_still_exits_130(ctx):
    """A first signal arriving only during the final sweep: bounded teardown
    continues, exit code still reports the interruption."""
    host = _SlowCloseHost()
    ctrl = _controller()
    background: "list[asyncio.Task[None]]" = []  # keeps the fire-and-forget task referenced

    async def release_after_signal() -> None:
        await host.close_started.wait()
        ctrl._on_signal(signal.SIGINT)
        host.release.set()

    async def body() -> None:
        ctx.scope.register(host)
        background.append(asyncio.ensure_future(release_after_signal()))

    with pytest.raises(SystemExit) as exc_info:
        run_command(body(), _controller=ctrl)
    assert exc_info.value.code == 130
    assert host.close_finished == 1  # graceful: sweep completed
    assert ctrl.forced is False


def test_forced_while_body_swallows_cancellation_skips_sweep(ctx):
    """A body that survives cancel (bad citizen) can't block the force path,
    and the abandoned run never starts the graceful sweep.

    Swallows exactly the first CancelledError (proving the force path does
    not wait around for a body that ignores cancellation) then lets a second
    one through. Without the bound, asyncio.run's own shutdown (which cancels
    every task still alive once, via _cancel_all_tasks) would hand this task
    a second CancelledError that it would swallow forever, hanging
    run_until_complete — a choreography fix, not a behavior weakening: every
    assertion below is unchanged from the brief.
    """
    host = _FakeHost()
    ctrl = _controller()

    async def body() -> None:
        ctx.scope.register(host)
        asyncio.get_running_loop().call_soon(ctrl._on_signal, signal.SIGINT)
        asyncio.get_running_loop().call_soon(ctrl._on_signal, signal.SIGINT)
        survived_once = False
        while True:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:  # noqa: PERF203 — deliberately misbehaving fixture
                if survived_once:
                    raise
                survived_once = True
                continue

    with pytest.raises(SystemExit) as exc_info:
        run_command(body(), _controller=ctrl)
    assert exc_info.value.code == 130
    assert ctrl.forced is True
    assert host.close_calls == 0  # sweep skipped entirely


def test_external_cancellation_is_not_an_interrupt():
    """CancelledError not caused by our signal handler propagates untouched."""
    ctrl = _controller()

    async def body() -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        run_command(body(), _controller=ctrl)


def test_body_error_takes_precedence_over_interrupt_exit(ctx):
    """Signal lands during the sweep after the body already failed: the real
    story is the body's error — it propagates, not SystemExit."""
    host = _SlowCloseHost()
    ctrl = _controller()
    background: "list[asyncio.Task[None]]" = []  # keeps the fire-and-forget task referenced

    async def release_after_signal() -> None:
        await host.close_started.wait()
        ctrl._on_signal(signal.SIGINT)
        host.release.set()

    async def body() -> None:
        ctx.scope.register(host)
        background.append(asyncio.ensure_future(release_after_signal()))
        raise ValueError("body boom")

    with pytest.raises(ValueError, match="body boom"):
        run_command(body(), _controller=ctrl)


@pytest.mark.asyncio
async def test_on_signal_cancels_and_schedules_deadline_despite_raising_stderr(monkeypatch):
    """Finding 2 (final review): closed/broken stderr (e.g. BrokenPipeError
    on a closed pipe) must never skip cancellation or the teardown deadline.
    Order matters: _on_signal must cancel the body and schedule the deadline
    BEFORE attempting the best-effort status line write, and the write must
    be wrapped so a raise there can't escape the loop callback."""
    from otto import lifecycle as lifecycle_module

    class _RaisingStderr:
        def write(self, _text: str) -> int:
            raise BrokenPipeError("closed pipe")

        def flush(self) -> None:
            pass

    monkeypatch.setattr(lifecycle_module.sys, "stderr", _RaisingStderr())
    ctrl = _controller()
    started = asyncio.Event()

    async def body() -> None:
        started.set()
        await asyncio.Event().wait()

    main = asyncio.ensure_future(ctrl._main(body()))
    await started.wait()

    ctrl._on_signal(signal.SIGINT)  # must not raise despite the write() above

    assert ctrl.interrupted == signal.SIGINT
    assert ctrl._deadline_handle is not None
    with pytest.raises(_InterruptedCommand) as exc_info:
        await main
    assert exc_info.value.signum == signal.SIGINT
    assert ctrl._body is not None
    assert ctrl._body.cancelled()  # cancel() landed despite the raising write


class _ClosedStderr:
    """A file object in the CLOSED state: write/flush raise ValueError, not OSError."""

    def write(self, _s: str) -> int:
        raise ValueError("I/O operation on closed file")

    def flush(self) -> None:
        raise ValueError("I/O operation on closed file")


@pytest.mark.asyncio
async def test_on_signal_survives_closed_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    """A closed stderr raises ValueError from write(), not OSError.

    The banner is best-effort diagnostics printed after cancellation and the
    deadline are committed; a diagnostics failure must never unwind the
    signal callback (chaos plan 3, carried over from plan 1's review).
    """
    from otto import lifecycle as lifecycle_module

    monkeypatch.setattr(lifecycle_module.sys, "stderr", _ClosedStderr())
    ctrl = _controller()
    started = asyncio.Event()

    async def body() -> None:
        started.set()
        await asyncio.Event().wait()

    main = asyncio.ensure_future(ctrl._main(body()))
    await started.wait()

    ctrl._on_signal(signal.SIGINT)  # must not raise

    assert ctrl.interrupted == signal.SIGINT
    assert ctrl._deadline_handle is not None
    with pytest.raises(_InterruptedCommand) as exc_info:
        await main
    assert exc_info.value.signum == signal.SIGINT
    assert ctrl._body is not None
    assert ctrl._body.cancelled()  # cancel() landed despite the raising write


def test_resolve_teardown_deadline_uses_env_settings(monkeypatch):
    from otto import lifecycle

    class _Env:
        teardown_deadline = 3.5

    def _get_env() -> "_Env":
        return _Env()

    monkeypatch.setattr("otto.config.get_env", _get_env)
    assert lifecycle._resolve_teardown_deadline() == 3.5


def test_resolve_teardown_deadline_falls_back_when_discovery_unavailable(monkeypatch):
    from otto import lifecycle

    def _boom():
        raise FileNotFoundError("no OTTO_SUT_DIRS")

    monkeypatch.setattr("otto.config.get_env", _boom)
    assert lifecycle._resolve_teardown_deadline() == DEFAULT_TEARDOWN_DEADLINE


@pytest.mark.asyncio
async def test_external_cancellation_still_sweeps_scope():
    """A task-level cancel of the command (no signal seen) must cancel the
    body and sweep scope-registered hosts BEFORE propagating — the old path
    re-raised first, leaking every host and leaving the body task dangling.
    Unreachable from the POSIX main-thread CLI; real for embedders."""
    from otto.config.lab import Lab
    from otto.context import OttoContext, reset_context, set_context

    ctx = OttoContext(lab=Lab(name="test"))
    closed: "list[str]" = []

    class _Host:
        id = "h1"

        async def close(self) -> None:
            closed.append("h1")

    ctx.scope.register(_Host())
    token = set_context(ctx)
    try:
        started = asyncio.Event()

        async def body() -> None:
            started.set()
            await asyncio.Event().wait()  # park until cancelled

        ctrl = _CommandRun(teardown_deadline=60.0, install_handlers=False)
        main = asyncio.ensure_future(ctrl._main(body()))
        await started.wait()
        main.cancel()
        with pytest.raises(asyncio.CancelledError):
            await main
        assert closed == ["h1"], "the sweep was skipped on external cancellation"
        assert ctrl.interrupted is None  # no signal was involved
        assert ctrl.forced is False
    finally:
        reset_context(token)


@pytest.mark.asyncio
@pytest.mark.timeout(5)
async def test_external_cancellation_sweep_is_bounded_by_the_deadline():
    """The external-cancellation path must bound its sweep exactly like the
    signal path (chaos spec: teardown is bounded by a deadline either way).
    No signal ever arrives here, so nothing but the external-cancel arm
    itself can arm the force event — without it, a hung host close would
    stall an embedder's cancellation forever. teardown_deadline=0.0 fires on
    the next loop turn: deterministic, no wall-clock wait. The @timeout(5)
    guard is a backstop only — a regression that drops the deadline-arming
    would otherwise hang up to the module's 180s default."""
    from otto.config.lab import Lab
    from otto.context import OttoContext, reset_context, set_context

    ctx = OttoContext(lab=Lab(name="test"))

    class _HangingHost:
        id = "hang"

        async def close(self) -> None:
            await asyncio.Event().wait()  # never resolves on its own

    ctx.scope.register(_HangingHost())
    token = set_context(ctx)
    try:
        started = asyncio.Event()

        async def body() -> None:
            started.set()
            await asyncio.Event().wait()  # park until cancelled

        ctrl = _CommandRun(teardown_deadline=0.0, install_handlers=False)
        main = asyncio.ensure_future(ctrl._main(body()))
        await started.wait()
        main.cancel()
        with pytest.raises(asyncio.CancelledError):
            await main
        assert ctrl.forced is True  # the hung sweep was abandoned, not awaited forever
        assert ctrl.interrupted is None  # still no signal was involved
    finally:
        reset_context(token)


@pytest.mark.asyncio
@pytest.mark.timeout(5)
async def test_external_cancellation_with_hung_body_unwind_is_bounded():
    """Final-review Finding 2: the deadline used to be armed AFTER joining
    the body — a body whose cancellation UNWIND hangs (catches the first
    cancel and parks again, a bad citizen) stalled the embedder's own
    cancellation forever, since nothing else ever set the force event.
    Arming the deadline BEFORE the join (matching the signal path) bounds
    it: the join races the force event via _race_force, expiry there raises
    _ForcedAbandon (suppressed), and marks ``forced`` — matching force
    semantics. RED first: against the old arm (`await self._body` with no
    force-race) this hangs; the @timeout(5) backstop turns that hang into a
    failure rather than a wedged test run (the same technique the Task 10
    fix round used)."""
    started = asyncio.Event()

    async def body() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.Event().wait()  # catches the cancel, parks forever

    ctrl = _CommandRun(teardown_deadline=0.0, install_handlers=False)
    main = asyncio.ensure_future(ctrl._main(body()))
    await started.wait()
    main.cancel()
    with pytest.raises(asyncio.CancelledError):
        await main
    assert ctrl.forced is True


@pytest.mark.asyncio
async def test_external_cancellation_body_translating_cancel_still_sweeps():
    """Final-review Finding 4: `suppress(asyncio.CancelledError)` on the join
    meant a body that answers cancellation with a DIFFERENT exception (a bad
    citizen translating its own unwind) propagated PAST the sweep entirely —
    leaking every scope-registered host. `suppress(BaseException)` on the
    join implements cancel-wins: whatever the body raises while unwinding is
    swallowed there, the external CancelledError captured in body_error is
    what ultimately propagates, and only AFTER the sweep runs. RED first:
    against the old arm this raises ValueError (not CancelledError) and the
    host is never closed (sweep skipped)."""
    from otto.config.lab import Lab
    from otto.context import OttoContext, reset_context, set_context

    ctx = OttoContext(lab=Lab(name="test"))
    closed: "list[str]" = []

    class _Host:
        id = "h1"

        async def close(self) -> None:
            closed.append("h1")

    ctx.scope.register(_Host())
    token = set_context(ctx)
    try:
        started = asyncio.Event()

        async def body() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise ValueError("translated") from None

        ctrl = _CommandRun(teardown_deadline=60.0, install_handlers=False)
        main = asyncio.ensure_future(ctrl._main(body()))
        await started.wait()
        main.cancel()
        with pytest.raises(asyncio.CancelledError):
            await main
        assert closed == ["h1"], "the sweep was skipped when the body translated its cancellation"
    finally:
        reset_context(token)
