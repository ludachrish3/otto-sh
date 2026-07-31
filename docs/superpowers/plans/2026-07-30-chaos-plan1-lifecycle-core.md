# Chaos Plan 1: Lifecycle Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every otto command one blessed asyncio entry (`run_command`) that enters the host scope, implements the two-stage SIGINT/SIGTERM policy (shielded teardown, second signal forces), and fixes the CLI's leaked context token.

**Architecture:** A new `src/otto/lifecycle.py` owns a `_CommandRun` state machine per command invocation: per-loop signal handlers route first signal → cancel body + bounded graceful sweep, second signal / deadline expiry → abandon sweep, run force-exit hooks, exit 128+signum. `async_typer_command` and all eleven bare `asyncio.run` command-path call sites delegate to it; an AST guard test makes the invariant permanent. Spec: `docs/superpowers/specs/2026-07-30-chaos-hardening-design.md` (Phase 1, "Canonical lifecycle entry" + "Context hygiene" + "Terminal restore").

**Tech Stack:** Python 3.10 asyncio (`loop.add_signal_handler`, `asyncio.wait` — no `asyncio.Runner`/`asyncio.timeout`, they're 3.11+), Typer CLI, pytest + pytest-asyncio (strict mode: every async test needs `@pytest.mark.asyncio`), pydantic-settings for the env knob.

## Global Constraints

- Python floor is 3.10 (`requires-python = ">=3.10"`). `X | None` annotations are fine; `asyncio.Runner`, `asyncio.timeout`, `except*` are NOT.
- NEVER add `from __future__ import annotations` (breaks Sphinx nitpicky `-W`; repo-wide ban).
- NEVER install real signal handlers in unit tests: `loop.add_signal_handler(SIGINT, ...)` replaces the chained SIGINT faulthandler that `tests/conftest.py` registers for the whole session, and `remove_signal_handler` restores `SIG_DFL`, not the chain. All tier-1 tests drive `_CommandRun._on_signal` directly with `install_handlers=False`. Real signal delivery is tier 2 (chaos spec plan 3), out of scope here.
- Tests count work, never wall-clock time: no `asyncio.sleep` as synchronization, no timing-ratio assertions. Deadline behavior is tested by asserting the scheduled `call_later` handle and by setting the force event directly (expiry and second-signal funnel into the same event by construction).
- Per-task gate: `make coverage` (there is no `make test`). Scoped pytest passing is NOT sufficient evidence — run the full gate before each task's final commit. Also run `uv run nox -s lint` (ruff check + format) and `make typecheck-python` (`ty` only runs there) after any `src/` edit.
- Never `git push`. Commit in the worktree with a conventional prefix and end every commit message with the trailer: `Assisted-by: Claude (Fable 5)`.
- Worktree setup quirks (execution-time, via superpowers:using-git-worktrees): EnterWorktree branches from **origin/main**, which may lack local squash-merges — run `git reset --hard main` immediately after entering. Fresh worktrees need `uv sync` and `npm ci` in `web/` before `make coverage` (it self-heals a missing web dist, but only if npm deps exist).
- Lint suppressions are a failure mode: prefer restructuring; a `# noqa` needs a written justification on the same line (existing code shows the pattern).

---

### Task 1: HostScope drains its host list on exit

`run_command` will enter/exit the scope once per `asyncio.run` — and `suite/run.py` calls it twice per command invocation. Today `HostScope.__aexit__` leaves `_hosts` populated, so a second exit re-closes every host. Drain the list on exit.

**Files:**
- Modify: `src/otto/context.py:70-78` (`HostScope.__aexit__`)
- Test: `tests/unit/test_context.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `HostScope.__aexit__` empties `self._hosts` before gathering closes. Later tasks rely on scope re-entry being a no-op for already-swept hosts.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_context.py` (the `_FakeHost` class at the top of the file already exists — reuse it):

```python
@pytest.mark.asyncio
async def test_hostscope_exit_drains_registered_hosts():
    """Scope exit sweeps AND forgets: a second enter/exit cycle (run_command
    is invoked multiple times per command in suite/run.py) must not re-close
    hosts swept by the first."""
    scope = HostScope()
    h = _FakeHost("a")
    scope.register(h)
    async with scope:
        pass
    assert h.close_calls == 1
    async with scope:
        pass
    assert h.close_calls == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_context.py::test_hostscope_exit_drains_registered_hosts -v`
Expected: FAIL — `assert 2 == 1` (close was called again on the second exit; `_FakeHost.close()` increments unconditionally).

- [ ] **Step 3: Implement the drain**

In `src/otto/context.py`, replace `HostScope.__aexit__`:

```python
    async def __aexit__(self, *exc: object) -> None:
        # Close on the Host *contract* (idempotent close()), not the
        # RemoteHost-private ``_connected``: DockerContainerHost / LocalHost are
        # BaseHosts without ``_connected``, so treat a missing attr as "needs
        # closing" (close() no-ops when nothing is open).
        # Drain the list first: the lifecycle wrapper enters/exits this scope
        # once per asyncio.run, and a command may run several (suite pre/post
        # phases), so a swept host must not be re-closed by the next cycle.
        hosts, self._hosts = self._hosts, []
        await asyncio.gather(
            *(h.close() for h in hosts if getattr(h, "_connected", True)),
            return_exceptions=True,
        )
```

- [ ] **Step 4: Run the scope tests**

Run: `uv run pytest tests/unit/test_context.py -v`
Expected: all PASS (the four existing `test_hostscope_*` tests plus the new one).

- [ ] **Step 5: Gate and commit**

Run: `uv run nox -s lint && make typecheck-python && make coverage`
Expected: all green.

```bash
git add src/otto/context.py tests/unit/test_context.py
git commit -m "fix(context): drain HostScope host list on exit

Scope exit is about to become per-asyncio.run (lifecycle run_command), and
suite/run.py runs several per command — a swept host must not be re-closed
by the next cycle.

Assisted-by: Claude (Fable 5)"
```

---

### Task 2: `lifecycle.py` core — run_command normal paths + force-exit hook registry

The module skeleton: `run_command` runs a coroutine under `asyncio.run`, entering the active context's scope (this is what fixes the sync-command scope bypass); the force-exit hook registry exists but nothing triggers hooks yet (Task 3 does).

**Files:**
- Create: `src/otto/lifecycle.py`
- Test: `tests/unit/test_lifecycle.py` (new)

**Interfaces:**
- Consumes: `otto.context.try_get_context() -> OttoContext | None`; `OttoContext.scope` (a `HostScope` with `__aenter__`/`__aexit__`, Task 1 semantics).
- Produces (used by Tasks 3-7):
  - `run_command(coro: Coroutine[Any, Any, R], *, teardown_deadline: float | None = None, _controller: _CommandRun | None = None) -> R`
  - `register_force_exit_hook(hook: Callable[[], None]) -> Callable[[], None]` (returns unregister; both idempotent)
  - `_run_force_exit_hooks() -> None`, `_force_exit_hooks: list` (test-visible)
  - `DEFAULT_TEARDOWN_DEADLINE = 10.0`
  - class `_CommandRun(*, teardown_deadline: float, install_handlers: bool = True)` with `async _main(coro)` (Task 3 adds `_on_signal`, `interrupted`, `forced`)

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_lifecycle.py`:

```python
"""Tier-1 tests for otto.lifecycle (chaos spec, plan 1).

No real signals anywhere in this module: loop.add_signal_handler would
replace the chained SIGINT faulthandler tests/conftest.py installs, and
remove_signal_handler restores SIG_DFL, not the chain. The state machine is
driven by calling _CommandRun._on_signal directly (install_handlers=False).
Real delivery is tier 2 (subprocess tests, chaos plan 3). Everything here
counts work — no wall-clock waits.
"""

import asyncio

import pytest

from otto.config.lab import Lab
from otto.context import HostScope, OttoContext, reset_context, set_context, try_get_context
from otto.lifecycle import (
    DEFAULT_TEARDOWN_DEADLINE,
    _force_exit_hooks,
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


def test_run_command_returns_result_without_context():
    async def body() -> int:
        return 41 + 1

    assert try_get_context() is None
    assert run_command(body()) == 42


def test_run_command_propagates_body_exception():
    async def body() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        run_command(body())


def test_run_command_sweeps_scope_registered_hosts(ctx):
    host = _FakeHost()

    async def body() -> None:
        ctx.scope.register(host)

    run_command(body())
    assert host.close_calls == 1


def test_run_command_sweeps_scope_even_when_body_raises(ctx):
    host = _FakeHost()

    async def body() -> None:
        ctx.scope.register(host)
        raise ValueError("boom")

    with pytest.raises(ValueError):
        run_command(body())
    assert host.close_calls == 1


def test_sequential_run_commands_do_not_reclose_swept_hosts(ctx):
    """suite/run.py runs several run_commands per command invocation."""
    host = _FakeHost()

    async def opens() -> None:
        ctx.scope.register(host)

    async def empty() -> None:
        pass

    run_command(opens())
    run_command(empty())
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_lifecycle.py -v`
Expected: FAIL at import — `ModuleNotFoundError: No module named 'otto.lifecycle'`.

- [ ] **Step 3: Write the minimal module**

Create `src/otto/lifecycle.py`:

```python
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


class _CommandRun:
    """One command invocation's lifecycle (Task 3 adds the interrupt machine)."""

    def __init__(self, *, teardown_deadline: float, install_handlers: bool = True) -> None:
        self.teardown_deadline = teardown_deadline
        self.install_handlers = install_handlers

    async def _main(self, coro: "Coroutine[Any, Any, R]") -> R:
        from .context import try_get_context

        ctx = try_get_context()
        if ctx is None:
            return await coro
        await ctx.scope.__aenter__()
        try:
            return await coro
        finally:
            await ctx.scope.__aexit__(None, None, None)


def run_command(
    coro: "Coroutine[Any, Any, R]",
    *,
    teardown_deadline: "float | None" = None,
    _controller: "_CommandRun | None" = None,
) -> R:
    """Run *coro* as a command body under otto's lifecycle policy.

    Enters the active ``OttoContext``'s host scope (when one is installed) so
    hosts opened during the command are swept at loop exit. Returns the
    coroutine's result. ``_controller`` is a test seam: tier-1 tests inject a
    ``_CommandRun(install_handlers=False)`` they hold a reference to.
    """
    deadline = DEFAULT_TEARDOWN_DEADLINE if teardown_deadline is None else teardown_deadline
    ctrl = _controller if _controller is not None else _CommandRun(teardown_deadline=deadline)
    return asyncio.run(ctrl._main(coro))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_lifecycle.py -v`
Expected: all 7 PASS.

- [ ] **Step 5: Gate and commit**

Run: `uv run nox -s lint && make typecheck-python && make coverage`
Expected: all green.

```bash
git add src/otto/lifecycle.py tests/unit/test_lifecycle.py
git commit -m "feat(lifecycle): add run_command scope-entering asyncio entry + force-exit hook registry

First slice of chaos plan 1: the canonical command entry that will replace
every bare asyncio.run in command paths. Scope entry here is what ends the
sync-command host-sweep bypass.

Assisted-by: Claude (Fable 5)"
```

---

### Task 3: The two-stage interrupt state machine

The heart of the plan. First signal: cancel body, print status line, run the scope sweep bounded by the teardown deadline. Second signal or deadline expiry: abandon the sweep (asyncio.run's finalization then hard-cancels it and closes transports), run force-exit hooks, exit 128+signum. This is the tier-1 "sweep" for the state machine: a parametrized suite injecting the interrupt at every phase.

**Files:**
- Modify: `src/otto/lifecycle.py` (extend `_CommandRun`, `run_command`)
- Test: `tests/unit/test_lifecycle.py`

**Interfaces:**
- Consumes: Task 2's module surface.
- Produces:
  - `_CommandRun._on_signal(signum: int) -> None` (loop-callback-safe; tests call it directly)
  - `_CommandRun.interrupted: int | None`, `_CommandRun.forced: bool`, `_CommandRun._force: asyncio.Event`, `_CommandRun._deadline_handle: asyncio.TimerHandle | None`
  - `run_command` raises `SystemExit(130)` on SIGINT, `SystemExit(143)` on SIGTERM, and runs force-exit hooks when `forced`
  - internal exceptions `_InterruptedCommand(signum, forced)`, `_ForcedAbandon`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_lifecycle.py`:

```python
import signal

from otto.lifecycle import _CommandRun, _InterruptedCommand


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


def _controller(deadline: float = DEFAULT_TEARDOWN_DEADLINE) -> _CommandRun:
    return _CommandRun(teardown_deadline=deadline, install_handlers=False)


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

    async def second_signal_when_sweep_starts() -> None:
        await host.close_started.wait()
        ctrl._on_signal(signal.SIGINT)

    async def body() -> None:
        ctx.scope.register(host)
        asyncio.ensure_future(second_signal_when_sweep_starts())
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

    async def release_after_signal() -> None:
        await host.close_started.wait()
        ctrl._on_signal(signal.SIGINT)
        host.release.set()

    async def body() -> None:
        ctx.scope.register(host)
        asyncio.ensure_future(release_after_signal())

    with pytest.raises(SystemExit) as exc_info:
        run_command(body(), _controller=ctrl)
    assert exc_info.value.code == 130
    assert host.close_finished == 1  # graceful: sweep completed
    assert ctrl.forced is False


def test_forced_while_body_swallows_cancellation_skips_sweep(ctx):
    """A body that survives cancel (bad citizen) can't block the force path,
    and the abandoned run never starts the graceful sweep."""
    host = _FakeHost()
    ctrl = _controller()

    async def body() -> None:
        ctx.scope.register(host)
        asyncio.get_running_loop().call_soon(ctrl._on_signal, signal.SIGINT)
        asyncio.get_running_loop().call_soon(ctrl._on_signal, signal.SIGINT)
        while True:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:  # noqa: PERF203 — deliberately misbehaving fixture
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

    async def release_after_signal() -> None:
        await host.close_started.wait()
        ctrl._on_signal(signal.SIGINT)
        host.release.set()

    async def body() -> None:
        ctx.scope.register(host)
        asyncio.ensure_future(release_after_signal())
        raise ValueError("body boom")

    with pytest.raises(ValueError, match="body boom"):
        run_command(body(), _controller=ctrl)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_lifecycle.py -v`
Expected: the new tests FAIL (`AttributeError: '_CommandRun' object has no attribute '_on_signal'` / `ImportError: cannot import name '_InterruptedCommand'`); Task 2's tests still PASS.

- [ ] **Step 3: Implement the state machine**

Replace `src/otto/lifecycle.py`'s `_CommandRun` and `run_command` with the full version (module docstring, hook registry, and `DEFAULT_TEARDOWN_DEADLINE` stay as Task 2 wrote them; add `signal` and `sys` to the imports):

```python
import signal
import sys

_INTERRUPT_STATUS_LINE = (
    "otto: interrupted — cleaning up remote sessions (interrupt again to abandon cleanup)"
)


class _InterruptedCommand(Exception):
    """Internal: the command was interrupted; carries signal + force flag out of the loop."""

    def __init__(self, signum: int, forced: bool) -> None:
        super().__init__(signum, forced)
        self.signum = signum
        self.forced = forced


class _ForcedAbandon(Exception):
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
            print(_INTERRUPT_STATUS_LINE, file=sys.stderr, flush=True)
            if self._body is not None:
                self._body.cancel()
            self._deadline_handle = asyncio.get_running_loop().call_later(
                self.teardown_deadline, self._force.set
            )
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
                except (NotImplementedError, RuntimeError):
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
    deadline = DEFAULT_TEARDOWN_DEADLINE if teardown_deadline is None else teardown_deadline
    ctrl = _controller if _controller is not None else _CommandRun(teardown_deadline=deadline)
    try:
        return asyncio.run(ctrl._main(coro))
    except _InterruptedCommand as exc:
        if exc.forced:
            _run_force_exit_hooks()
        raise SystemExit(128 + exc.signum) from None
```

Design notes the implementer must preserve:
- `SystemExit` is raised OUTSIDE the loop (after `asyncio.run` returns). A `SystemExit` raised inside an asyncio task re-raises into the loop — a known repo bug class.
- Force-exit hooks run after loop close, so termios restoration works even though transports were killed mid-flight.
- The deadline handle and the force event are the SAME mechanism: `call_later(deadline, self._force.set)`. There is no separate expiry code path to test.
- `remove_signal_handler` in the `finally` restores default dispositions between the sequential `run_command` calls a suite command makes — brief unhandled windows, same as today's behavior, strictly better during the loops.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_lifecycle.py -v`
Expected: all PASS (7 from Task 2 + 8 new).

- [ ] **Step 5: Gate and commit**

Run: `uv run nox -s lint && make typecheck-python && make coverage`
Expected: all green.

```bash
git add src/otto/lifecycle.py tests/unit/test_lifecycle.py
git commit -m "feat(lifecycle): two-stage SIGINT/SIGTERM policy with bounded, forceable teardown

First signal cancels the body and runs the host sweep under the teardown
deadline; a second signal or expiry abandons the sweep, runs force-exit
hooks, and exits 128+signum. Tier-1 suite drives _on_signal directly at
every lifecycle phase (chaos spec plan 1).

Assisted-by: Claude (Fable 5)"
```

---

### Task 4: `OTTO_TEARDOWN_DEADLINE` env knob

**Files:**
- Modify: `src/otto/models/settings.py` (`OttoEnvSettings`, after `log_rich`)
- Modify: `src/otto/lifecycle.py` (`_resolve_teardown_deadline`)
- Modify: `docs/getting-started.md:393-397` (env var table)
- Test: `tests/unit/test_lifecycle.py`; the existing `OttoEnvSettings` test module (find it with `grep -rl OttoEnvSettings tests/unit/` — add there)

**Interfaces:**
- Consumes: `otto.config.get_env() -> OttoEnvSettings` (cached discovery accessor).
- Produces: `OttoEnvSettings.teardown_deadline: float = 10.0` (env `OTTO_TEARDOWN_DEADLINE`); `run_command(teardown_deadline=None)` resolves env → default.

- [ ] **Step 1: Write the failing tests**

In the existing `OttoEnvSettings` test module:

```python
def test_teardown_deadline_reads_env(monkeypatch):
    monkeypatch.setenv("OTTO_TEARDOWN_DEADLINE", "3.5")
    assert OttoEnvSettings().teardown_deadline == 3.5


def test_teardown_deadline_default():
    assert OttoEnvSettings().teardown_deadline == 10.0
```

In `tests/unit/test_lifecycle.py`:

```python
def test_resolve_teardown_deadline_uses_env_settings(monkeypatch):
    from otto import lifecycle

    class _Env:
        teardown_deadline = 3.5

    monkeypatch.setattr("otto.config.get_env", lambda: _Env())
    assert lifecycle._resolve_teardown_deadline() == 3.5


def test_resolve_teardown_deadline_falls_back_when_discovery_unavailable(monkeypatch):
    from otto import lifecycle

    def _boom():
        raise FileNotFoundError("no OTTO_SUT_DIRS")

    monkeypatch.setattr("otto.config.get_env", _boom)
    assert lifecycle._resolve_teardown_deadline() == DEFAULT_TEARDOWN_DEADLINE
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_lifecycle.py -k teardown_deadline -v` (plus the settings module tests)
Expected: FAIL — `OttoEnvSettings` has no field `teardown_deadline`; `lifecycle` has no `_resolve_teardown_deadline`.

- [ ] **Step 3: Implement**

`src/otto/models/settings.py`, in `OttoEnvSettings` after `log_rich: bool = False`:

```python
    teardown_deadline: float = 10.0
    """Seconds an interrupted command's graceful cleanup may run before it is
    abandoned (second Ctrl+C / SIGTERM abandons it sooner). OTTO_TEARDOWN_DEADLINE."""
```

`src/otto/lifecycle.py` — add, and change `run_command`'s resolution line to use it:

```python
def _resolve_teardown_deadline() -> float:
    """``OTTO_TEARDOWN_DEADLINE`` via the typed env settings, else the default."""
    try:
        from .config import get_env

        return get_env().teardown_deadline
    except Exception:  # noqa: BLE001 — discovery unavailable (bare library use): fall back
        return DEFAULT_TEARDOWN_DEADLINE
```

```python
    deadline = _resolve_teardown_deadline() if teardown_deadline is None else teardown_deadline
```

`docs/getting-started.md` env table — add the row:

```markdown
| `OTTO_TEARDOWN_DEADLINE` | Seconds an interrupted command's cleanup may run before being abandoned | `10` |
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_lifecycle.py tests/unit -k "teardown_deadline" -v`
Expected: PASS. Also run `make docs-lint` for the table edit.

- [ ] **Step 5: Gate and commit**

Run: `uv run nox -s lint && make typecheck-python && make coverage`
Expected: all green.

```bash
git add src/otto/models/settings.py src/otto/lifecycle.py docs/getting-started.md tests/
git commit -m "feat(lifecycle): OTTO_TEARDOWN_DEADLINE env knob for the graceful-teardown bound

Assisted-by: Claude (Fable 5)"
```

---

### Task 5: Wire the CLI — async_typer_command delegates; context token finally resets

**Files:**
- Modify: `src/otto/utils.py:92-113` (`async_typer_command`)
- Modify: `src/otto/context.py` (add `set_cli_context` / `reset_cli_context` below `reset_context`)
- Modify: `src/otto/cli/invoke.py:383-387` (use `set_cli_context`)
- Modify: `src/otto/cli/main.py:693` (`entry()`: wrap `app()` in try/finally)
- Test: `tests/unit/test_context.py`

**Interfaces:**
- Consumes: `run_command` (Task 3 signature).
- Produces: `set_cli_context(ctx: OttoContext) -> None`; `reset_cli_context() -> None` (idempotent). `async_typer_command` keeps its exact public signature — all existing decorated commands (`cli/expose.py`, `cli/registry.py`, `cli/docker.py`, `cli/link.py`, `cli/tunnel.py`, `cli/run.py`) are untouched.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_context.py`:

```python
def test_set_and_reset_cli_context_pair():
    from otto.context import reset_cli_context, set_cli_context

    baseline = try_get_context()
    ctx = OttoContext(lab=_lab_with())
    set_cli_context(ctx)
    assert try_get_context() is ctx
    reset_cli_context()
    assert try_get_context() is baseline
    reset_cli_context()  # idempotent: second reset is a no-op
    assert try_get_context() is baseline
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_context.py::test_set_and_reset_cli_context_pair -v`
Expected: FAIL — `ImportError: cannot import name 'reset_cli_context'`.

- [ ] **Step 3: Implement**

`src/otto/context.py`, directly below `reset_context`:

```python
_cli_token: "Token[OttoContext | None] | None" = None


def set_cli_context(ctx: "OttoContext") -> None:
    """Install *ctx* as the CLI invocation's context, remembering the reset token.

    The CLI installs the context from deep inside the Typer callback
    (``cli.invoke.ensure_lab_context``) while the natural reset point is the
    console-script entry's ``finally`` — the two can't share a stack frame, so
    the token lives module-side. One CLI invocation per process; tests that
    drive the app via CliRunner are covered by the autouse ContextVar
    snapshot fixture in tests/conftest.py either way.
    """
    global _cli_token
    _cli_token = set_context(ctx)


def reset_cli_context() -> None:
    """Undo :func:`set_cli_context` if it ran; safe to call unconditionally."""
    global _cli_token
    if _cli_token is not None:
        reset_context(_cli_token)
        _cli_token = None
```

`src/otto/cli/invoke.py` — replace the install at :383-387:

```python
    # Install the runtime context: lab + dry_run flag. Token kept module-side
    # in otto.context; entry()'s finally calls reset_cli_context().
    from ..context import OttoContext, set_cli_context

    set_cli_context(OttoContext(lab=lab, dry_run=opts.dry_run))
    meta["_otto_lab_ready"] = True
    return get_context()
```

`src/otto/cli/main.py` — replace the bare `app()` at :693:

```python
    from ..context import reset_cli_context

    try:
        app()
    finally:
        reset_cli_context()
```

`src/otto/utils.py` — replace `async_typer_command`'s body (keep the decorator's signature and `functools.wraps`; drop the now-unused `asyncio` import if nothing else in the module uses it):

```python
def async_typer_command(f: Callable[P, Coroutine[Any, Any, R]]) -> Callable[P, R]:
    """Wrap an async Typer command so it runs under otto's command lifecycle.

    Delegates to :func:`otto.lifecycle.run_command`: host-scope entry, the
    two-stage SIGINT/SIGTERM policy, and the bounded teardown deadline.
    """

    @functools.wraps(f)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        from .lifecycle import run_command

        return run_command(f(*args, **kwargs))

    return wrapper
```

- [ ] **Step 4: Run the context + CLI unit tests**

Run: `uv run pytest tests/unit/test_context.py tests/unit/test_lifecycle.py -v`
Expected: all PASS — including the pre-existing `test_async_typer_command_enters_and_exits_scope` and `test_async_typer_command_runs_without_active_context`, which now exercise the delegation.

- [ ] **Step 5: Gate and commit**

Run: `uv run nox -s lint && make typecheck-python && make coverage`
Expected: all green (the e2e CLI suites in `make coverage` are the real regression net for this task).

```bash
git add src/otto/utils.py src/otto/context.py src/otto/cli/invoke.py src/otto/cli/main.py tests/unit/test_context.py
git commit -m "refactor(cli): route async_typer_command through lifecycle.run_command; pair the CLI context token

Every async command now gets the two-stage interrupt policy, and entry()
finally resets the ContextVar token ensure_lab_context used to discard.

Assisted-by: Claude (Fable 5)"
```

---

### Task 6: Migrate the bare `asyncio.run` call sites + AST guard (proven red)

Eleven bare `asyncio.run` calls in command paths bypass the lifecycle. Write the guard first and watch it name exactly these; then migrate; then watch it pass. AST-based so the docstring examples in `src/otto/monitor/__init__.py` (strings, not calls) don't false-positive.

**Files:**
- Create: `tests/unit/test_no_bare_asyncio_run.py`
- Modify: `src/otto/cli/cov.py:340,810,911`
- Modify: `src/otto/cli/monitor.py:155,244`
- Modify: `src/otto/suite/run.py:577,589,668,698`
- Modify: `src/otto/config/repo.py:807,820`

**Interfaces:**
- Consumes: `run_command` (Task 3 signature).
- Produces: the repo invariant "no `asyncio.run` outside `src/otto/lifecycle.py`", enforced forever.

- [ ] **Step 1: Write the guard test**

Create `tests/unit/test_no_bare_asyncio_run.py`:

```python
"""Every command-path ``asyncio.run`` must go through ``otto.lifecycle.run_command``.

A bare ``asyncio.run`` bypasses the host-scope sweep and the two-stage
interrupt policy — the exact bug class chaos plan 1 closed (sync command
paths never swept their hosts). AST-based: docstring example snippets (e.g.
``otto/monitor/__init__.py``) are string constants, not Call nodes.
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "otto"

# The one module allowed to call asyncio.run: the lifecycle entry itself.
ALLOWED = {SRC / "lifecycle.py"}


def _bare_asyncio_run_lines(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "asyncio"
    ]


def test_no_bare_asyncio_run_outside_lifecycle():
    offenders = {
        str(path.relative_to(SRC)): lines
        for path in sorted(SRC.rglob("*.py"))
        if path not in ALLOWED and (lines := _bare_asyncio_run_lines(path))
    }
    assert offenders == {}, (
        f"bare asyncio.run() outside otto.lifecycle: {offenders} — route "
        "command bodies through otto.lifecycle.run_command instead"
    )
```

- [ ] **Step 2: Run the guard to prove it red**

Run: `uv run pytest tests/unit/test_no_bare_asyncio_run.py -v`
Expected: FAIL, naming exactly: `cli/cov.py` (340, 810, 911), `cli/monitor.py` (155, 244), `suite/run.py` (577, 589, 668, 698), `config/repo.py` (807, 820). If it names anything else, STOP and investigate before migrating — do not extend `ALLOWED` to make it pass.

- [ ] **Step 3: Migrate the call sites**

In each file, add the import once (`from ..lifecycle import run_command` — note `config/repo.py` and `suite/run.py` also use `..lifecycle`; `cli/*.py` files use `..lifecycle` too since `cli` is one package deep) and mechanically replace each call. The coroutine argument is unchanged in every case:

- `src/otto/cli/cov.py:340`: `store = asyncio.run(run_coverage_report(...))` → `store = run_command(run_coverage_report(...))`
- `src/otto/cli/cov.py:810`: `asyncio.run(...)` → `run_command(...)` (same inner coroutine)
- `src/otto/cli/cov.py:911`: `asyncio.run(_do_clean())` → `run_command(_do_clean())`
- `src/otto/cli/monitor.py:155`: `asyncio.run(_serve_review(export, source.name, tls, archive_path))` → `run_command(_serve_review(export, source.name, tls, archive_path))`
- `src/otto/cli/monitor.py:244`: `asyncio.run(_run_monitor(...))` → `run_command(_run_monitor(...))`
- `src/otto/suite/run.py:577`: `asyncio.run(_pre_run_cov_clean(repos, run_options))` → `run_command(_pre_run_cov_clean(repos, run_options))`
- `src/otto/suite/run.py:589`: `asyncio.run(_post_run_coverage(repos, log_dir, run_options))` → `run_command(_post_run_coverage(repos, log_dir, run_options))`
- `src/otto/suite/run.py:668` and `:698`: same two replacements with `opts`
- `src/otto/config/repo.py:807`: `asyncio.run(self.set_commit_hash())` → `run_command(self.set_commit_hash())`
- `src/otto/config/repo.py:820`: `asyncio.run(self.set_git_description())` → `run_command(self.set_git_description())`

Then remove each file's `import asyncio` ONLY if nothing else in that file uses `asyncio` (ruff's F401 in the lint gate will confirm; `suite/run.py` and `cli/monitor.py` likely still use asyncio elsewhere — check before removing).

Rationale note for the reviewer (include in the commit message): `config/repo.py`'s two sites are lazy property getters that open no hosts — they're migrated for the guard invariant, not for sweep behavior; `run_command` is behavior-identical for them (no active-context change, and a Ctrl+C during a local git call now exits 130 instead of raising bare KeyboardInterrupt).

- [ ] **Step 4: Run the guard and the touched suites**

Run: `uv run pytest tests/unit/test_no_bare_asyncio_run.py tests/unit -v -x`
Expected: guard PASSES; no unit regressions.

- [ ] **Step 5: Gate and commit**

Run: `uv run nox -s lint && make typecheck-python && make coverage`
Expected: all green — `make coverage` matters most here: the cov/monitor/suite e2e paths all execute these call sites.

```bash
git add tests/unit/test_no_bare_asyncio_run.py src/otto/cli/cov.py src/otto/cli/monitor.py src/otto/suite/run.py src/otto/config/repo.py
git commit -m "refactor: route all command-path asyncio.run calls through lifecycle.run_command

Guard test (proven red against the eleven prior call sites) makes the
invariant permanent: only otto/lifecycle.py may call asyncio.run. Sync
command paths (cov, monitor, suite pre/post, repo props) now sweep their
host scope and honor the interrupt policy.

Assisted-by: Claude (Fable 5)"
```

---

### Task 7: Terminal restore on the force path (`interact.py`)

A forced exit abandons `_run_bridge`'s `finally`, stranding the terminal in raw mode. Register the same restore as a force-exit hook for exactly the raw-mode window.

**Files:**
- Modify: `src/otto/host/interact.py` (new `_force_restore_guard` helper + one `with` in `_run_bridge`)
- Test: `tests/unit/host/test_interact_force_restore.py` (new)

**Interfaces:**
- Consumes: `register_force_exit_hook` (Task 2), `_restore_terminal(stdin_fd, saved_attrs)` and `_setup_raw_mode` (existing module-private helpers in `interact.py`).
- Produces: `interact._force_restore_guard(stdin_fd: int, saved_attrs) -> ContextManager[None]`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/host/test_interact_force_restore.py`:

```python
"""The raw-mode window registers a lifecycle force-exit hook (chaos plan 1)."""

from otto import lifecycle
from otto.host import interact


def test_guard_registers_restore_hook_for_the_raw_mode_window(monkeypatch):
    calls: list[tuple[int, object]] = []
    monkeypatch.setattr(
        interact, "_restore_terminal", lambda fd, attrs: calls.append((fd, attrs))
    )
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/host/test_interact_force_restore.py -v`
Expected: FAIL — `AttributeError: module 'otto.host.interact' has no attribute '_force_restore_guard'`.

- [ ] **Step 3: Implement**

In `src/otto/host/interact.py` (near `_restore_terminal`; the module already imports `contextlib`):

```python
@contextlib.contextmanager
def _force_restore_guard(stdin_fd: int, saved_attrs: "Any") -> "Iterator[None]":
    """Keep the terminal recoverable across a forced exit while raw mode is live.

    ``_run_bridge``'s ``finally`` restores termios on every graceful path; a
    forced exit (second interrupt / teardown-deadline expiry) abandons that
    unwind, so the same restore is registered as a lifecycle force-exit hook
    for exactly the raw-mode window. ``saved_attrs is None`` (stdin not a
    tty — raw mode never engaged) registers nothing.
    """
    if saved_attrs is None:
        yield
        return
    from ..lifecycle import register_force_exit_hook

    unregister = register_force_exit_hook(lambda: _restore_terminal(stdin_fd, saved_attrs))
    try:
        yield
    finally:
        unregister()
```

(`interact.py` is `otto/host/interact.py`, so `..lifecycle` reaches `otto/lifecycle.py`.)

In `_run_bridge` (`interact.py:449` onward), wrap everything after raw mode engages — from the banner print through the existing `try`/`finally` — in the guard, indenting the existing block one level:

```python
    saved_attrs = _setup_raw_mode(stdin_fd) if stdin_is_tty else None

    with _force_restore_guard(stdin_fd, saved_attrs):
        if banner is not None:
            _print_stderr(banner)
        ...  # existing code through the finally that calls _restore_terminal
```

(`Iterator` may need adding to the module's `collections.abc` imports.)

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/host/test_interact_force_restore.py tests/unit/host -v`
Expected: PASS, no regressions in the host unit tree.

- [ ] **Step 5: Gate and commit**

Run: `uv run nox -s lint && make typecheck-python && make coverage`
Expected: all green (the login e2e paths under `make coverage` exercise `_run_bridge` with the guard in place).

```bash
git add src/otto/host/interact.py tests/unit/host/test_interact_force_restore.py
git commit -m "feat(host): restore the terminal on force-exit during interactive login

Raw-mode window registers the termios restore as a lifecycle force-exit
hook, so an abandoned teardown can no longer strand the terminal.

Assisted-by: Claude (Fable 5)"
```

---

### Task 8: Full verification + branch finish

- [ ] **Step 1: Full gates from a clean state**

Run, in order: `uv run nox -s lint`, `make typecheck-python`, `make coverage`, `make docs-lint`.
Expected: all green. If `make coverage` shows failures anywhere (not just in touched files), investigate before proceeding — a scoped pass with a broken full suite is the known trap for exactly this kind of cross-cutting change.

- [ ] **Step 2: Re-run the tier-1 chaos suite once more, single file, verbose**

Run: `uv run pytest tests/unit/test_lifecycle.py tests/unit/test_no_bare_asyncio_run.py tests/unit/test_context.py -v`
Expected: every test PASS; skim the names — they should read as the plan's behavior catalog.

- [ ] **Step 3: Finish the branch**

Use superpowers:finishing-a-development-branch. Chris merges (squash-merge is this repo's norm) and pushes; never push from the session.

---

## Self-review (completed at write time)

- **Spec coverage:** run_command (Task 2-3) ✓; two-stage policy + exit codes + status line (Task 3) ✓; deadline settings-overridable (Task 4) ✓; scope entry for sync commands (Tasks 2+6) ✓; `reset_context` fix (Task 5) ✓; tier-1 sweeps for the state machine (Task 3's parametrized suite) ✓; proven-red first (Task 6's guard red run; every TDD step also runs red first) ✓; terminal-restore-on-force hook (Tasks 2+7) ✓. NOT in this plan by design: guarded close chains, ranked HostScope, `compensate()` (plan 2); real-signal tests (plan 3).
- **Placeholder scan:** no TBDs; every step has runnable code or an exact command.
- **Type consistency:** `run_command(coro, *, teardown_deadline=None, _controller=None)` used identically in Tasks 3, 5, 6; `register_force_exit_hook` returns the unregister callable everywhere; `_CommandRun(teardown_deadline=..., install_handlers=...)` consistent across Tasks 2-3 and tests.
