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

from otto.context import try_get_context
from otto.host import LocalHost
from otto.invocation import RunPolicy
from otto.lifecycle import (
    _CommandRun,
    _force_exit_hooks,
    _InterruptedCommand,
    _run_force_exit_hooks,
    register_force_exit_hook,
    run_command,
)
from tests._fixtures.registry import register_duck, registered_ids


class _FakeHost:
    """Minimal RemoteHost stand-in: identity, _connected, counted close()."""

    def __init__(self, host_id: str = "h") -> None:
        self.id = host_id
        self._connected = True
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        self._connected = False


def _controller(deadline: float = RunPolicy().teardown_deadline) -> _CommandRun:
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


def test_a_context_less_connect_is_registered_on_the_command_loop():
    seen: list[list[str]] = []

    async def body():
        host = LocalHost()
        host._claim_loop()
        seen.append(registered_ids(asyncio.get_running_loop()))

    run_command(body(), _controller=_controller())
    assert seen == [["local"]]


def test_run_command_closes_the_hosts_registered_on_its_loop():
    host = _FakeHost()

    async def body() -> None:
        register_duck(host, asyncio.get_running_loop())

    run_command(body(), _controller=_controller())
    assert host.close_calls == 1


def test_run_command_closes_its_loops_hosts_even_when_body_raises():
    host = _FakeHost()

    async def body() -> None:
        register_duck(host, asyncio.get_running_loop())
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        run_command(body(), _controller=_controller())
    assert host.close_calls == 1


def test_sequential_run_commands_do_not_reclose_swept_hosts():
    """suite/run.py runs several run_commands per command invocation."""
    host = _FakeHost()

    async def opens() -> None:
        register_duck(host, asyncio.get_running_loop())

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
    """Deferred Task-2 review finding: nothing else pins this default."""
    assert RunPolicy().teardown_deadline == 10.0


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
def test_first_signal_cancels_body_sweeps_and_exits_128_plus_signum(capsys, signum, expected_code):
    host = _FakeHost()
    ctrl = _controller()

    async def body() -> None:
        register_duck(host, asyncio.get_running_loop())
        asyncio.get_running_loop().call_soon(ctrl._on_signal, signum)
        await asyncio.Event().wait()  # cancelled by the handler

    with pytest.raises(SystemExit) as exc_info:
        run_command(body(), _controller=ctrl)
    assert exc_info.value.code == expected_code
    assert host.close_calls == 1  # graceful sweep ran
    assert ctrl.forced is False
    assert "cleaning up" in capsys.readouterr().err


def test_second_signal_during_teardown_abandons_sweep_and_runs_hooks():
    host = _SlowCloseHost()
    hook_ran: list[bool] = []
    unregister = register_force_exit_hook(lambda: hook_ran.append(True))
    ctrl = _controller()
    background: "list[asyncio.Task[None]]" = []  # keeps fire-and-forget tasks referenced

    async def second_signal_when_sweep_starts() -> None:
        await host.close_started.wait()
        ctrl._on_signal(signal.SIGINT)

    async def body() -> None:
        register_duck(host, asyncio.get_running_loop())
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


def test_signal_during_teardown_of_successful_body_still_exits_130():
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
        register_duck(host, asyncio.get_running_loop())
        background.append(asyncio.ensure_future(release_after_signal()))

    with pytest.raises(SystemExit) as exc_info:
        run_command(body(), _controller=ctrl)
    assert exc_info.value.code == 130
    assert host.close_finished == 1  # graceful: sweep completed
    assert ctrl.forced is False


def test_forced_while_body_swallows_cancellation_skips_sweep():
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
        register_duck(host, asyncio.get_running_loop())
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


def test_body_error_takes_precedence_over_interrupt_exit():
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
        register_duck(host, asyncio.get_running_loop())
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


@pytest.mark.asyncio
async def test_external_cancellation_still_releases_the_boundary():
    """A task-level cancel of the command (no signal seen) must cancel the
    body and close the loop's hosts BEFORE propagating — the old path
    re-raised first, leaking every host and leaving the body task dangling.
    Unreachable from the POSIX main-thread CLI; real for embedders. No
    context is installed: the command's own cleanup boundary closes them."""
    closed: "list[str]" = []

    class _Host:
        id = "h1"

        async def close(self) -> None:
            closed.append("h1")

    register_duck(_Host(), asyncio.get_running_loop())
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
    assert closed == ["h1"], "the boundary's sweep was skipped on external cancellation"
    assert ctrl.interrupted is None  # no signal was involved
    assert ctrl.forced is False


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

    class _HangingHost:
        id = "hang"

        async def close(self) -> None:
            await asyncio.Event().wait()  # never resolves on its own

    register_duck(_HangingHost(), asyncio.get_running_loop())
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
    leaking every host registered on the loop. `suppress(BaseException)` on the
    join implements cancel-wins: whatever the body raises while unwinding is
    swallowed there, the external CancelledError captured in body_error is
    what ultimately propagates, and only AFTER the sweep runs. RED first:
    against the old arm this raises ValueError (not CancelledError) and the
    host is never closed (sweep skipped)."""
    closed: "list[str]" = []

    class _Host:
        id = "h1"

        async def close(self) -> None:
            closed.append("h1")

    register_duck(_Host(), asyncio.get_running_loop())
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


# ── interruptible_prompt (#1003) ─────────────────────────────────────────────
# The prompt runs on a daemon thread while the command awaits its answer, so a
# signal is the run's ordinary loop-callback interrupt. Tier 1 delivers it the
# way this module always does, by calling _CommandRun._on_signal on the loop.
# Real delivery at a real prompt: tests/unit/test_lifecycle_prompt_signals.py
# and tests/e2e/cli/test_prompt_interrupt_e2e.py.


@pytest.fixture(autouse=True)
def _abandoned_prompts_finish():
    """An abandoned prompt holds the one-prompt lock until its thread returns.

    A PARKED thread ("otto-prompt-parked") already gave the lock and the
    reader back, and stays parked for the process's life by design.
    """
    yield
    import threading

    from otto.utils import wait_for

    for thread in threading.enumerate():
        if thread.name == "otto-prompt":
            wait_for(
                lambda thread=thread: not thread.is_alive() or thread.name == "otto-prompt-parked",
                5,
                interval=0.01,
                on_timeout=lambda: "a test left its prompt thread blocked",
            )


def test_an_answered_prompt_returns_its_answer():
    from otto.lifecycle import interruptible_prompt

    def prompt(text: str, *, default: str) -> str:
        return f"{text}={default}"

    async def body() -> str:
        return await interruptible_prompt(prompt, "Name", default="x")

    assert run_command(body(), _controller=_controller()) == "Name=x"


def test_what_the_prompt_raises_is_raised_to_the_command():
    """click.Abort (end of input) and the like surface where the command awaits."""
    from otto.lifecycle import interruptible_prompt

    def prompt() -> str:
        raise EOFError

    async def body() -> str:
        return await interruptible_prompt(prompt)

    with pytest.raises(EOFError):
        run_command(body(), _controller=_controller())


@pytest.mark.parametrize(
    ("signum", "expected_code"),
    [(signal.SIGINT, 130), (signal.SIGTERM, 143)],
)
def test_a_signal_while_the_prompt_waits_cancels_the_command_there(capsys, signum, expected_code):
    """The command stops at the await, swept and unforced, while the prompt still waits."""
    import threading

    from otto.lifecycle import interruptible_prompt

    ctrl = _controller()
    release = threading.Event()  # the prompt's "person": never answers here
    reached: "list[str]" = []

    def prompt() -> str:
        release.wait()
        return "too late"

    async def body() -> None:
        asyncio.get_running_loop().call_later(0.05, ctrl._on_signal, signum)
        await interruptible_prompt(prompt)
        reached.append("after the prompt")

    try:
        with pytest.raises(SystemExit) as exc_info:
            run_command(body(), _controller=ctrl)
    finally:
        release.set()  # let the abandoned thread finish

    assert exc_info.value.code == expected_code
    assert reached == []
    assert ctrl.forced is False
    assert "cleaning up" in capsys.readouterr().err


def test_an_answer_after_the_command_gave_up_is_dropped():
    """The abandoned thread's late answer finds the loop gone and is discarded quietly."""
    import threading

    from otto.lifecycle import interruptible_prompt

    ctrl = _controller()
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def prompt() -> str:
        started.set()
        release.wait()
        finished.set()
        return "too late"

    async def body() -> None:
        loop = asyncio.get_running_loop()
        loop.call_later(0.05, ctrl._on_signal, signal.SIGINT)
        await interruptible_prompt(prompt)

    with pytest.raises(SystemExit):
        run_command(body(), _controller=ctrl)
    assert started.is_set()
    release.set()
    assert finished.wait(timeout=5)


def test_the_console_reader_is_swapped_for_the_python_line_read_while_the_prompt_waits(
    monkeypatch,
):
    """On a console input() holds the C stdin lock, which an abandoned prompt keeps past exit()."""
    import sys
    import types

    from otto.lifecycle import _read_prompt_line, interruptible_prompt

    termui = types.ModuleType("fake_termui")
    termui.visible_prompt_func = input
    monkeypatch.setitem(sys.modules, "fake_termui", termui)
    seen: "list[object]" = []

    def prompt() -> str:
        seen.append(termui.visible_prompt_func)
        return "ok"

    prompt.__module__ = "fake_termui"

    async def body() -> str:
        return await interruptible_prompt(prompt)

    assert run_command(body(), _controller=_controller()) == "ok"
    assert [getattr(reader, "func", reader) for reader in seen] == [_read_prompt_line]
    assert termui.visible_prompt_func is input  # put back


def test_a_reader_someone_else_installed_is_left_alone(monkeypatch):
    """click's CliRunner installs an echoing reader; that one stays."""
    import sys
    import types

    from otto.lifecycle import interruptible_prompt

    def theirs(_prompt: str = "") -> str:
        return "theirs"

    termui = types.ModuleType("fake_termui")
    termui.visible_prompt_func = theirs
    monkeypatch.setitem(sys.modules, "fake_termui", termui)
    seen: "list[object]" = []

    def prompt() -> str:
        seen.append(termui.visible_prompt_func)
        return "ok"

    prompt.__module__ = "fake_termui"

    async def body() -> str:
        return await interruptible_prompt(prompt)

    run_command(body(), _controller=_controller())
    assert seen == [theirs]
    assert termui.visible_prompt_func is theirs


def test_typer_prompts_reader_is_the_one_swapped():
    """typer.prompt is typer's own click fork, whose termui holds the reader to swap."""
    import sys

    import typer

    termui = sys.modules[typer.prompt.__module__]
    assert termui.visible_prompt_func is input


@pytest.mark.parametrize(
    ("typed", "expected"),
    [("widget\n", "widget"), ("no newline", "no newline"), ("\n", "")],
)
def test_the_python_line_read_is_inputs(monkeypatch, capsys, typed, expected):
    import io
    import sys

    from otto.lifecycle import _read_prompt_line

    monkeypatch.setattr(sys, "stdin", io.StringIO(typed))
    assert _read_prompt_line(": ") == expected
    assert capsys.readouterr().out == ": "


def test_the_python_line_read_raises_eof_at_end_of_input(monkeypatch):
    import io
    import sys

    from otto.lifecycle import _read_prompt_line

    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    with pytest.raises(EOFError):
        _read_prompt_line()


def test_a_prompt_that_reads_after_the_command_gave_up_still_gets_the_python_reader(
    monkeypatch,
):
    """click looks the reader up when it reads, which can be after the cancel.

    Restoring ``input`` from the command's side at that point would hand the
    late read the console path, and its C stdin lock would hold the exit.
    """
    import sys
    import threading
    import types

    from otto.lifecycle import _read_prompt_line, interruptible_prompt

    termui = types.ModuleType("fake_termui")
    termui.visible_prompt_func = input
    monkeypatch.setitem(sys.modules, "fake_termui", termui)
    cancelled = threading.Event()
    seen: "list[object]" = []

    def prompt() -> str:
        cancelled.wait()  # the question is on screen; the read comes after the cancel
        seen.append(termui.visible_prompt_func)
        return "late"

    prompt.__module__ = "fake_termui"
    ctrl = _controller()

    def interrupt() -> None:
        ctrl._on_signal(signal.SIGINT)
        cancelled.set()

    async def body() -> None:
        asyncio.get_running_loop().call_later(0.05, interrupt)
        await interruptible_prompt(prompt)

    with pytest.raises(SystemExit):
        run_command(body(), _controller=ctrl)
    assert [getattr(reader, "func", reader) for reader in seen] == [_read_prompt_line]


def test_a_hidden_prompt_is_refused_before_it_starts():
    """getpass restores echo in a finally an abandoned thread never reaches."""
    from otto.lifecycle import interruptible_prompt

    asked: "list[str]" = []

    def prompt(text: str, *, hide_input: bool) -> str:
        asked.append(text)
        return "secret"

    async def body() -> str:
        return await interruptible_prompt(prompt, "Password", hide_input=True)

    with pytest.raises(ValueError, match="hidden prompt"):
        run_command(body(), _controller=_controller())
    assert asked == []


def test_a_second_prompt_while_an_abandoned_one_still_reads_is_refused():
    """One reader per terminal: the first prompt's thread still owns the reader swap.

    Let in, the second prompt would find the Python reader already installed,
    and the first thread's return would put ``input`` back under it.
    """
    import threading

    from otto.lifecycle import interruptible_prompt

    started, release = threading.Event(), threading.Event()
    asked: "list[str]" = []

    def first() -> str:
        started.set()
        release.wait()
        return "too late"

    def second() -> str:
        asked.append("second")
        return "ok"

    ctrl = _controller()

    async def abandon() -> None:
        asyncio.get_running_loop().call_later(0.05, ctrl._on_signal, signal.SIGINT)
        await interruptible_prompt(first)

    async def ask_second() -> str:
        return await interruptible_prompt(second)

    try:
        with pytest.raises(SystemExit):
            run_command(abandon(), _controller=ctrl)
        assert started.is_set()
        with pytest.raises(RuntimeError, match="still reading"):
            run_command(ask_second(), _controller=_controller())
        assert asked == []
    finally:
        release.set()
    for thread in threading.enumerate():
        if thread.name == "otto-prompt":
            thread.join(timeout=5)
    assert run_command(ask_second(), _controller=_controller()) == "ok"


@pytest.mark.parametrize(
    ("args", "kwargs"),
    [(("Password",), {"hide_input": True}), (("Password", None, True), {})],
    ids=["keyword", "positional"],
)
def test_typer_prompts_hidden_input_is_refused_however_it_is_passed(args, kwargs):
    """typer.prompt(text, default, hide_input, ...) takes hide_input by position too."""
    import typer

    from otto.lifecycle import interruptible_prompt

    async def body() -> str:
        return await interruptible_prompt(typer.prompt, *args, **kwargs)

    with pytest.raises(ValueError, match="hidden prompt"):
        run_command(body(), _controller=_controller())


def test_hidden_input_through_a_forwarding_wrapper_or_a_partial_is_refused():
    """A wrapper's **kwargs hides the keyword from the signature; a partial presets it."""
    import functools

    import typer

    from otto.lifecycle import interruptible_prompt

    def wrapper(*args: object, **kwargs: object) -> object:
        return typer.prompt(*args, **kwargs)

    calls = [
        (wrapper, ("Password",), {"hide_input": True}),
        (functools.partial(typer.prompt, hide_input=True), ("Password",), {}),
    ]
    for prompt, args, kwargs in calls:

        async def body(prompt=prompt, args=args, kwargs=kwargs) -> object:
            return await interruptible_prompt(prompt, *args, **kwargs)

        with pytest.raises(ValueError, match="hidden prompt"):
            run_command(body(), _controller=_controller())


def test_the_python_line_read_is_quiet_only_for_the_read(monkeypatch, capsys):
    """Quiet while it reads (no stream lock held); not once it returns: click may write next."""
    import sys

    from otto.lifecycle import _PromptHandoff, _read_prompt_line

    loop = asyncio.new_event_loop()
    try:
        handoff = _PromptHandoff(loop)
        at_read: "list[bool]" = []

        class Stdin:
            def readline(self) -> str:
                at_read.append(handoff._quiet)
                return "widget\n"

        monkeypatch.setattr(sys, "stdin", Stdin())
        assert _read_prompt_line(": ", handoff=handoff) == "widget"
        assert at_read == [True]
        assert handoff._quiet is False
        assert capsys.readouterr().out == ": "
    finally:
        loop.close()


def _fake_termui(monkeypatch):
    import sys
    import types

    termui = types.ModuleType("fake_termui")
    termui.visible_prompt_func = input
    monkeypatch.setitem(sys.modules, "fake_termui", termui)
    return termui


def test_an_interrupted_prompt_finishes_writing_before_the_command_goes_on(monkeypatch):
    """The cancel waits for the question to be written: shutdown must find no write in progress."""
    import io
    import sys
    import threading

    from otto.lifecycle import interruptible_prompt
    from otto.utils import wait_for

    termui = _fake_termui(monkeypatch)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # never read: the thread parks first
    cancelled = threading.Event()
    order: "list[str]" = []

    def prompt() -> str:
        cancelled.wait()  # the interrupt lands before the question is written
        order.append("question written")
        return termui.visible_prompt_func("Name: ")  # abandoned by now, normally: parks

    prompt.__module__ = "fake_termui"
    ctrl = _controller()

    def interrupt() -> None:
        ctrl._on_signal(signal.SIGINT)
        cancelled.set()

    async def body() -> None:
        asyncio.get_running_loop().call_later(0.05, interrupt)
        try:
            await interruptible_prompt(prompt)
        finally:
            order.append("command goes on")

    with pytest.raises(SystemExit):
        run_command(body(), _controller=ctrl)
    assert order == ["question written", "command goes on"]
    wait_for(
        lambda: termui.visible_prompt_func is input,  # the thread gave the reader back
        5,
        interval=0.01,
        on_timeout=lambda: "the prompt thread never put the reader back",
    )


def test_an_abandoned_prompt_never_writes_again(monkeypatch):
    """A line that arrives after the command gave up is not answered with click's next write."""
    import sys
    import threading

    from otto.lifecycle import interruptible_prompt
    from otto.utils import wait_for

    termui = _fake_termui(monkeypatch)
    gone = threading.Event()
    after_read: "list[str]" = []

    class Stdin:
        def readline(self) -> str:
            gone.wait()  # the line arrives once the command has gone on
            return "maybe\n"

    monkeypatch.setattr(sys, "stdin", Stdin())

    asked_on: "list[threading.Thread]" = []

    def prompt() -> str:
        asked_on.append(threading.current_thread())
        line = termui.visible_prompt_func("Name: ")
        after_read.append("Error: invalid input")  # what click would write next
        return line

    prompt.__module__ = "fake_termui"
    ctrl = _controller()

    async def body() -> None:
        asyncio.get_running_loop().call_later(0.05, ctrl._on_signal, signal.SIGINT)
        try:
            await interruptible_prompt(prompt)
        finally:
            gone.set()

    with pytest.raises(SystemExit):
        run_command(body(), _controller=ctrl)
    (thread,) = asked_on
    wait_for(
        lambda: thread.name == "otto-prompt-parked",
        5,
        interval=0.01,
        on_timeout=lambda: "the abandoned prompt thread never parked",
    )
    assert after_read == []


def test_a_second_signal_forces_the_run_while_the_prompt_is_still_writing(monkeypatch):
    """The cancel awaits the write on the loop, so the force stage still works during it."""
    import threading

    from otto.lifecycle import interruptible_prompt

    termui = _fake_termui(monkeypatch)
    release = threading.Event()  # the question's write never finishes on its own

    def prompt() -> str:
        release.wait()
        return termui.visible_prompt_func("Name: ")

    prompt.__module__ = "fake_termui"
    ctrl = _controller()

    async def body() -> None:
        loop = asyncio.get_running_loop()
        loop.call_later(0.05, ctrl._on_signal, signal.SIGINT)
        loop.call_later(0.1, ctrl._on_signal, signal.SIGINT)
        await interruptible_prompt(prompt)

    try:
        with pytest.raises(SystemExit) as exc_info:
            run_command(body(), _controller=ctrl)
    finally:
        release.set()
    assert exc_info.value.code == 130
    assert ctrl.forced is True
