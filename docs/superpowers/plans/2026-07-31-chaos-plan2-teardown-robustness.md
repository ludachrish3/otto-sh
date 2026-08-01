# Chaos Plan 2: Teardown Robustness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every teardown chain unable to silently skip steps — guarded `ConnectionManager.close`, try/finally `UnixHost.close`, dependency-ranked `HostScope` sweep — and shield every compensating action (tunnel rollback, link rollback, `as_user` undo, nc listener reap) behind a new `compensate()` helper, each proven by tier-1 cancellation sweeps.

**Architecture:** A new tier-1 sweep harness (`tests/_fixtures/chaos.py`) counts instrumented await points in a scenario, then re-runs it injecting `CancelledError` / a connection-drop at every point; an oracle asserts nothing was silently skipped. The product changes it guards: per-step log-and-continue guards in `ConnectionManager.close` (order preserved: sftp → ssh → ftp → telnet → hop), try/finally in `UnixHost.close`, children-before-parents ranked close with per-host failure logging in `HostScope.__aexit__`, and `otto.lifecycle.compensate(coro, deadline)` — hold cancellation until a rollback completes, bounded by a deadline — wired into the four rollback/undo sites the spec names. The close chains get exhaustive point-by-point sweeps; the four `compensate()` call sites get targeted cancel-mid-operation / cancel-mid-rollback tests (their non-cancellation failure modes are already point-covered by the existing rollback test suites) — `compensate()`'s own state machine is exhaustively unit-tested once. Two Plan 1 carry-overs land here too: external task-cancellation now sweeps the scope before propagating, and `suite/run.py` drops dead per-loop connection state before its post-run sweep. Spec: `docs/superpowers/specs/2026-07-30-chaos-hardening-design.md` (Phase 1 "Teardown chain robustness" + "Shielded compensating actions"; Phase 2 "Tier 1").

**Tech Stack:** Python 3.10 asyncio (`asyncio.shield`, `loop.call_later`, `asyncio.gather` — no `asyncio.Runner`/`asyncio.timeout`, they're 3.11+), pytest + pytest-asyncio (strict mode: every async test needs `@pytest.mark.asyncio`).

## Global Constraints

- Python floor is 3.10 (`requires-python = ">=3.10"`). `X | None` annotations are fine; `asyncio.Runner`, `asyncio.timeout`, `except*` are NOT.
- NEVER add `from __future__ import annotations` (breaks Sphinx nitpicky `-W`; repo-wide ban).
- NEVER install real signal handlers in unit tests (they replace the conftest's chained SIGINT faulthandler; `remove_signal_handler` restores `SIG_DFL`, not the chain). Nothing in this plan needs signal delivery: cancellation is injected by raising `CancelledError` at instrumented points or by `task.cancel()` on plain tasks.
- Tests count work, never wall-clock time: no `asyncio.sleep(x > 0)` as synchronization, no timing assertions. `asyncio.sleep(0)` is a pure scheduling yield and is allowed. Deadline expiry is driven by `deadline=0` (the `call_later(0, ...)` fires on the next loop turn — deterministic), never by waiting out a real deadline.
- Cancellation policy (design commitment, do not drift): close-chain step guards catch `Exception` ONLY — a real `CancelledError` aborts a chain *loudly* (that is the force-abandon contract from Plan 1). `compensate()` is the ONLY place a cancellation is held, and it always re-raises the held cancellation after the compensation resolves.
- Teardown chain order is load-bearing and must be preserved exactly: sftp → ssh → ftp → telnet → hop (`ConnectionManager.close`); sessions → connections (`UnixHost.close`); children before the hosts they name as `parent` (`HostScope`).
- Per-task gate: `make coverage` (there is no `make test`). Scoped pytest passing is NOT sufficient evidence — run the full gate before each task's final commit. Also run `uv run nox -s lint` (ruff check + format) and `make typecheck-python` (`ty` only runs there) after any `src/` edit.
- Never `git push`. Commit in the worktree with a conventional prefix and end every commit message with the trailer: `Assisted-by: Claude (Fable 5)`.
- Worktree setup quirks (execution-time, via superpowers:using-git-worktrees): EnterWorktree branches from **origin/main**, which may lack local squash-merges — run `git reset --hard main` immediately after entering. Fresh worktrees need `uv sync` and `npm ci` in `web/` before `make coverage` (it self-heals a missing web dist, but only if npm deps exist).
- Lint suppressions are a failure mode: prefer restructuring; a `# noqa` needs a written justification on the same line (existing code shows the pattern).

## File Structure

| File | Role in this plan |
| --- | --- |
| `tests/_fixtures/chaos.py` (new) | Tier-1 sweep harness: `ChaosPoints`, `ConnectionDropped`, `sweep_cancellation` |
| `tests/unit/test_chaos_fixture.py` (new) | Meta-tests: the harness flags unguarded chains, passes guarded ones |
| `src/otto/host/connections.py` | `_teardown_step` guard + guarded, take-then-clear `close()` |
| `tests/unit/host/test_connections_close.py` (new) | Close-chain sweep + slot-clearing + logging tests |
| `src/otto/host/unix_host.py` | try/finally `close()` |
| `src/otto/context.py` | Ranked `HostScope.__aexit__` + failure logging + `rebuild_connections()` |
| `src/otto/lifecycle.py` | `compensate()`; external-cancel sweep fix in `_CommandRun._main` |
| `tests/unit/test_lifecycle_compensate.py` (new) | `compensate()` state-machine tests |
| `src/otto/tunnel/manage.py` | `add_tunnel` rollback via `compensate()` |
| `src/otto/link/manage.py` | `impair_link` rollback on `BaseException` + `compensate()` |
| `src/otto/host/privilege.py` | `as_user` undo extracted to `_undo_switch`, run via `compensate()` |
| `src/otto/host/transfer/nc.py` | `_cancel_and_reap` helper, run via `compensate()` |
| `src/otto/suite/run.py` | Post-pytest `scope.rebuild_connections()` before the post-run sweep |

Tasks 2–4 consume Task 1's harness; Tasks 6–9 consume Task 5's `compensate()`. Tasks 10 and 11 are the Plan 1 carry-overs and depend only on Plan 1's landed code.

---

### Task 1: Tier-1 sweep harness (`tests/_fixtures/chaos.py`)

The reusable core of the chaos spec's tier 1: run a scenario once against instrumented fakes to count its await points (N), then N more runs injecting an exception at point 1, 2, … N, calling an oracle after each. Meta-tests prove the harness itself catches an unguarded chain and passes a guarded one — the product chains get their own sweeps in Tasks 2–4.

**Files:**
- Create: `tests/_fixtures/chaos.py`
- Test: `tests/unit/test_chaos_fixture.py` (new)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces (used by Tasks 2, 3, 4):
  - `class ChaosPoints` with attributes `count: int`, `executed: list[str]`, `tripped_at: str | None`; methods `arm(at: int, exc: type[BaseException]) -> None`, `async point(label: str) -> None`, `sync_point(label: str) -> None`
  - `class ConnectionDropped(Exception)`
  - `async sweep_cancellation(scenario_factory, oracle, *, exceptions=(asyncio.CancelledError, ConnectionDropped)) -> None` where `scenario_factory(points: ChaosPoints)` is an awaitable-returning callable that builds FRESH fakes wired to *points* and runs the scenario, and `oracle(points, outcome: BaseException | None, exc_type: type[BaseException], k: int)` asserts invariants after each armed run

- [ ] **Step 1: Write the harness**

Create `tests/_fixtures/chaos.py`:

```python
"""Tier-1 chaos harness: deterministic cancellation/fault sweeps over teardown chains.

``sweep_cancellation()`` runs a scenario once to COUNT its chaos points
(instrumented await checkpoints inside fakes), then re-runs it once per
point per exception class, arming exactly one point each run. After every
armed run the caller's oracle asserts the chain's invariants — every
must-run step behind the injection point still executed, or the chain
failed loudly rather than silently skipping (chaos spec tier 1:
docs/superpowers/specs/2026-07-30-chaos-hardening-design.md).

Determinism: no wall-clock, no randomness — the injection point is an
integer counter. The ``CancelledError`` variant models a cancellation
landing at that await; ``ConnectionDropped`` models the transport dying
there. ``point()`` awaits ``asyncio.sleep(0)`` first so every instrumented
point is a REAL suspension point, keeping cancellation semantics honest.
"""

import asyncio
from collections.abc import Awaitable, Callable


class ConnectionDropped(Exception):
    """Injected stand-in for a transport dying mid-call (tier-1 fault variant)."""


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
        self.tripped_at: "str | None" = None
        self._arm_at: "int | None" = None
        self._exc: "type[BaseException] | None" = None

    def arm(self, at: int, exc: "type[BaseException]") -> None:
        """Make checkpoint number *at* (1-based) raise *exc* instead of executing."""
        self._arm_at = at
        self._exc = exc

    def sync_point(self, label: str) -> None:
        """A synchronous checkpoint: counts, then trips or records."""
        self.count += 1
        if self._arm_at == self.count:
            assert self._exc is not None
            self.tripped_at = label
            raise self._exc(f"chaos injection at point {self.count} ({label})")
        self.executed.append(label)

    async def point(self, label: str) -> None:
        """An async checkpoint: a real suspension, then counts, then trips or records."""
        await asyncio.sleep(0)
        self.sync_point(label)


async def sweep_cancellation(
    scenario_factory: "Callable[[ChaosPoints], Awaitable[object]]",
    oracle: "Callable[[ChaosPoints, BaseException | None, type[BaseException], int], None]",
    *,
    exceptions: "tuple[type[BaseException], ...]" = (asyncio.CancelledError, ConnectionDropped),
) -> None:
    """Sweep a scenario: one baseline run to count points, then one run per (exception, point).

    *scenario_factory* must build FRESH fakes for the ``ChaosPoints`` it
    receives each call — state must never leak between runs. The baseline
    run (nothing armed) must complete without raising: a scenario that
    fails un-injected is a broken scenario, not a chaos finding.
    """
    baseline = ChaosPoints()
    await scenario_factory(baseline)
    n = baseline.count
    assert n > 0, "scenario exercised no chaos points — nothing to sweep"
    for exc_type in exceptions:
        for k in range(1, n + 1):
            points = ChaosPoints()
            points.arm(k, exc_type)
            outcome: "BaseException | None" = None
            try:
                await scenario_factory(points)
            except BaseException as e:  # noqa: BLE001 — the sweep records ANY outcome for the oracle
                outcome = e
            oracle(points, outcome, exc_type, k)
```

- [ ] **Step 2: Write the meta-tests**

Create `tests/unit/test_chaos_fixture.py`:

```python
"""The tier-1 sweep harness itself: it must flag unguarded chains and pass guarded ones.

Toy chains only — the product chains get their own sweeps next to their
modules (test_connections_close.py, test_unix_host.py, test_context.py).
"""

import asyncio

import pytest

from tests._fixtures.chaos import ChaosPoints, ConnectionDropped, sweep_cancellation

_LABELS = ["a", "b", "c"]


async def _guarded(points: ChaosPoints) -> None:
    """Every step individually guarded against a drop — the shape Task 2 builds."""
    for label in _LABELS:
        try:
            await points.point(label)
        except ConnectionDropped:
            pass


async def _unguarded(points: ChaosPoints) -> None:
    """The pre-fix shape: one raising step skips everything behind it."""
    for label in _LABELS:
        await points.point(label)


def _guarded_oracle(
    points: ChaosPoints, outcome: "BaseException | None", exc_type: type, k: int
) -> None:
    if exc_type is ConnectionDropped:
        assert outcome is None, f"drop at point {k} escaped the chain"
        assert points.executed == [s for i, s in enumerate(_LABELS) if i != k - 1], (
            f"steps after point {k} were skipped"
        )
    else:  # CancelledError: aborting loudly is the contract — nothing runs after, nothing hides
        assert isinstance(outcome, asyncio.CancelledError)
        assert points.executed == _LABELS[: k - 1]


@pytest.mark.asyncio
async def test_sweep_passes_a_guarded_chain():
    await sweep_cancellation(_guarded, _guarded_oracle)


@pytest.mark.asyncio
async def test_sweep_fails_an_unguarded_chain():
    with pytest.raises(AssertionError, match="escaped the chain"):
        await sweep_cancellation(_unguarded, _guarded_oracle)


@pytest.mark.asyncio
async def test_sweep_rejects_a_scenario_with_no_points():
    async def empty(points: ChaosPoints) -> None:
        pass

    with pytest.raises(AssertionError, match="no chaos points"):
        await sweep_cancellation(empty, _guarded_oracle)


@pytest.mark.asyncio
async def test_baseline_run_counts_and_records_everything():
    points = ChaosPoints()
    await _unguarded(points)
    assert points.count == 3
    assert points.executed == _LABELS
    assert points.tripped_at is None
```

- [ ] **Step 3: Run the meta-tests**

Run: `uv run pytest tests/unit/test_chaos_fixture.py -v`
Expected: all 4 PASS.

- [ ] **Step 4: Gate and commit**

Run: `uv run nox -s lint && make typecheck-python && make coverage`
Expected: all green.

```bash
git add tests/_fixtures/chaos.py tests/unit/test_chaos_fixture.py
git commit -m "test(chaos): tier-1 cancellation sweep harness

sweep_cancellation() counts a scenario's instrumented await points, then
re-runs it injecting CancelledError / ConnectionDropped at every point;
the caller's oracle asserts nothing was silently skipped. Consumed by the
close-chain sweeps in the next tasks (chaos spec tier 1).

Assisted-by: Claude (Fable 5)"
```

---

### Task 2: `ConnectionManager.close` — per-step guards, take-then-clear

Today one raising step (e.g. `ftp.quit()` on a dead socket) skips every later step, including the SSH-hop teardown, and a failed step leaves its half-dead connection cached. Guard each step log-and-continue, clear every cached slot BEFORE attempting its close, keep the order sftp → ssh → ftp → telnet → hop, and keep the zombie-transport mitigation (now in a `finally` so it survives a raising `wait_closed`).

**Files:**
- Modify: `src/otto/host/connections.py:466-513` (`ConnectionManager.close`; add `_teardown_step` above the class, add `Iterator` import)
- Test: `tests/unit/host/test_connections_close.py` (new)

**Interfaces:**
- Consumes: Task 1's `ChaosPoints`, `ConnectionDropped`, `sweep_cancellation`.
- Produces: `ConnectionManager.close()` with unchanged signature; per-step `Exception`s are logged at WARNING on the `otto.host.connections` logger and never skip later steps; `CancelledError` still propagates. Cached slots (`_sftp_conn`/`_ssh_conn`/`_ftp_conn`/`_telnet_conn`) are `None` after `close()` even when steps raise.

- [ ] **Step 1: Write the failing sweep test**

Create `tests/unit/host/test_connections_close.py`:

```python
"""ConnectionManager.close chain: per-step guards + tier-1 cancellation sweep.

Chain order is sftp -> ssh -> ftp -> telnet -> hop. One raising step (e.g.
``ftp.quit()`` on a dead socket) must not skip the steps behind it — in
particular the hop teardown (chaos spec: teardown chain robustness). A
CancelledError still aborts the chain loudly (force-abandon contract).
"""

import asyncio

import pytest

from otto.host.connections import ConnectionManager
from tests._fixtures.chaos import ChaosPoints, ConnectionDropped, sweep_cancellation

_STEPS = ["sftp", "ssh", "ftp", "telnet", "hop"]


class _FakeTransport:
    def close(self) -> None:
        pass


class _FakeSsh:
    def __init__(self, points: ChaosPoints) -> None:
        self._points = points
        self._transport = _FakeTransport()

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        await self._points.point("ssh")


class _FakeSftp:
    def __init__(self, points: ChaosPoints) -> None:
        self._points = points

    def exit(self) -> None:  # asyncssh SFTPClient.exit is synchronous
        self._points.sync_point("sftp")


class _FakeFtp:
    def __init__(self, points: ChaosPoints) -> None:
        self._points = points

    async def quit(self) -> None:
        await self._points.point("ftp")


class _FakeTelnet:
    def __init__(self, points: ChaosPoints) -> None:
        self._points = points

    async def close(self) -> None:
        await self._points.point("telnet")


class _FakeHop:
    def __init__(self, points: ChaosPoints) -> None:
        self._points = points

    async def close(self) -> None:
        await self._points.point("hop")


def _manager(points: ChaosPoints) -> ConnectionManager:
    """A REAL ConnectionManager (the chain under test) over instrumented fakes."""
    mgr = ConnectionManager(ip="10.0.0.1", creds=[], user="u", term="ssh", name="box")
    mgr._sftp_conn = _FakeSftp(points)
    mgr._ssh_conn = _FakeSsh(points)
    mgr._ftp_conn = _FakeFtp(points)
    mgr._telnet_conn = _FakeTelnet(points)
    mgr._hop = _FakeHop(points)
    return mgr


async def _scenario(points: ChaosPoints) -> None:
    await _manager(points).close()


def _oracle(
    points: ChaosPoints, outcome: "BaseException | None", exc_type: type, k: int
) -> None:
    if exc_type is ConnectionDropped:
        # Guarded chain: the drop is logged, every later step still runs.
        assert outcome is None, f"drop at step {_STEPS[k - 1]!r} escaped ConnectionManager.close"
        assert points.executed == [s for i, s in enumerate(_STEPS) if i != k - 1], (
            f"steps after {_STEPS[k - 1]!r} were skipped"
        )
    else:
        # CancelledError: the chain stops loudly (force-abandon semantics).
        assert isinstance(outcome, asyncio.CancelledError)
        assert points.executed == _STEPS[: k - 1]


@pytest.mark.asyncio
async def test_close_chain_sweep():
    await sweep_cancellation(_scenario, _oracle)


@pytest.mark.asyncio
async def test_close_clears_cached_slots_even_when_a_step_raises():
    """A failing close must not leave a half-dead connection cached for reuse."""
    points = ChaosPoints()
    points.arm(3, ConnectionDropped)  # ftp.quit() blows up
    mgr = _manager(points)
    await mgr.close()
    assert mgr._sftp_conn is None
    assert mgr._ssh_conn is None
    assert mgr._ftp_conn is None
    assert mgr._telnet_conn is None


@pytest.mark.asyncio
async def test_close_logs_the_failing_step(caplog):
    points = ChaosPoints()
    points.arm(3, ConnectionDropped)
    with caplog.at_level("WARNING", logger="otto.host.connections"):
        await _manager(points).close()
    assert any("ftp" in r.message and "box" in r.message for r in caplog.records)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/host/test_connections_close.py -v`
Expected: FAIL — `test_close_chain_sweep` hits the oracle's "escaped ConnectionManager.close" assertion at the first `ConnectionDropped` injection (today the chain is unguarded); the slot-clearing and logging tests also fail.

- [ ] **Step 3: Implement the guarded chain**

In `src/otto/host/connections.py`, add `Iterator` to the `collections.abc` import line (the module already imports `contextlib` and defines `logger`):

```python
from collections.abc import Iterator
```

Add the guard helper directly above `class ConnectionManager` (after the module logger):

```python
@contextlib.contextmanager
def _teardown_step(name: str, step: str) -> "Iterator[None]":
    """Guard one close-chain step: log-and-continue so a raising step can't skip the rest.

    Exception only — CancelledError still propagates: an abandoned teardown
    (second Ctrl+C / deadline expiry) stops the chain loudly rather than
    pretending to finish it (chaos spec: teardown chain robustness).
    """
    try:
        yield
    except Exception as e:  # noqa: BLE001 — teardown chain must not let one step skip the rest
        logger.warning(f"{name}: {step} teardown failed: {e}")
```

Replace the body of `ConnectionManager.close` (keep the docstring's first line, extend it; keep the trailing NOTE comment block about the zombie transport / no `gc.collect()` verbatim):

```python
    async def close(self) -> None:
        """Close all open connections, port forwards, and the tunnel.

        Every step is individually guarded (log-and-continue) so one raising
        step — e.g. ``ftp.quit()`` on a dead socket — cannot skip the steps
        behind it, in particular the SSH-hop teardown (chaos spec: teardown
        chain robustness). Cached slots are cleared take-then-clear BEFORE
        each close attempt so a failing close can't leave a half-dead
        connection cached for reuse. ``CancelledError`` is not guarded: a
        force-abandoned teardown stops the chain, loudly.
        """
        sftp, self._sftp_conn = self._sftp_conn, None
        ssh, self._ssh_conn = self._ssh_conn, None
        ftp, self._ftp_conn = self._ftp_conn, None
        telnet, self._telnet_conn = self._telnet_conn, None

        if sftp:
            with _teardown_step(self._name, "sftp"):
                sftp.exit()

        if ssh:
            with _teardown_step(self._name, "ssh"):
                # asyncssh's ``wait_closed()`` returns when the SSH session
                # finishes — but in some teardown paths (notably hopped
                # connections where the parent tunnel survives the child) the
                # underlying asyncio ``_SelectorSocketTransport`` is left with
                # ``_closing=False`` even though the OS socket is gone (fd=-1).
                # That zombie transport sits in GC until later, when its
                # ``__del__`` fires ``ResourceWarning`` on a closed loop and
                # pytest's ``[unraisable]`` plugin escalates it into a flake on
                # whichever next test happens to be running. Grab the asyncio
                # transport before close and explicitly close() it after — this
                # sets ``_closing=True`` so ``__del__`` is a no-op. In a
                # ``finally`` so a raising/cancelled ``wait_closed`` still gets
                # the mitigation.
                asyncio_transport = getattr(ssh, "_transport", None)
                try:
                    ssh.close()
                    await ssh.wait_closed()
                finally:
                    if asyncio_transport is not None:
                        asyncio_transport.close()

        if ftp:
            with _teardown_step(self._name, "ftp"):
                await ftp.quit()

        if telnet:
            with _teardown_step(self._name, "telnet"):
                await telnet.close()

        if self._hop is not None:
            with _teardown_step(self._name, "hop"):
                await self._hop.close()
```

(The original in-body comment above `asyncio_transport` moves into the ssh step as shown; the big trailing `# NOTE:` block after the chain stays exactly where and as it is.)

- [ ] **Step 4: Run the new tests and the neighbors**

Run: `uv run pytest tests/unit/host/test_connections_close.py tests/unit/host/test_connections.py tests/unit/host/test_unix_host.py tests/unit/host/test_hop.py -v`
Expected: all PASS (the existing `TestClose` tests in test_unix_host.py assert ssh close/wait_closed calls and slot clearing — both preserved).

- [ ] **Step 5: Gate and commit**

Run: `uv run nox -s lint && make typecheck-python && make coverage`
Expected: all green.

```bash
git add src/otto/host/connections.py tests/unit/host/test_connections_close.py
git commit -m "fix(host): guard every ConnectionManager.close step

One raising step (ftp.quit on a dead socket) used to skip every later
step including the SSH-hop teardown, and left its half-dead connection
cached. Steps are now individually log-and-continue guarded with
take-then-clear slots; CancelledError still aborts loudly. Proven by a
tier-1 cancellation sweep (chaos spec plan 2).

Assisted-by: Claude (Fable 5)"
```

---

### Task 3: `UnixHost.close` — transports close even when sessions fail

`UnixHost.close` runs `close_all()` then `_connections.close()` as an unguarded sequence: a session manager that raises (wedged remote shell) leaks every raw transport behind it. try/finally: the failure still propagates (loud), but the transports close.

**Files:**
- Modify: `src/otto/host/unix_host.py:494-498` (`UnixHost.close`)
- Test: `tests/unit/host/test_unix_host.py` (append to `TestClose`)

**Interfaces:**
- Consumes: Task 1's harness.
- Produces: `UnixHost.close()` unchanged signature; `_connections.close()` always runs; `close_all()`'s exception (or cancellation) propagates after.

- [ ] **Step 1: Write the failing tests**

Append to `class TestClose` in `tests/unit/host/test_unix_host.py` (the file already imports `pytest`, `AsyncMock`, `UnixHost`, `Cred`, `LogMode`; add `import asyncio` and `from tests._fixtures.chaos import ChaosPoints, sweep_cancellation` to its imports if not present):

```python
    @pytest.mark.asyncio
    async def test_close_closes_transports_when_session_close_raises(self):
        """A wedged session must not leak the raw transports behind it: the
        failure propagates, but _connections.close() still runs (chaos spec:
        teardown chain robustness)."""
        h = UnixHost(
            ip="10.0.0.1", element="box", creds=[Cred(login="u", password="p")], log=LogMode.QUIET
        )
        h._session_mgr.close_all = AsyncMock(side_effect=RuntimeError("session wedged"))
        conn_close = AsyncMock()
        h._connections.close = conn_close
        with pytest.raises(RuntimeError, match="session wedged"):
            await h.close()
        conn_close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_chain_sweep(self):
        """Tier-1 sweep: whichever step dies (drop OR cancel), the other still
        runs and the failure propagates — try/finally, not log-and-continue."""
        steps = ["sessions", "connections"]

        async def scenario(points: ChaosPoints) -> None:
            h = UnixHost(
                ip="10.0.0.1",
                element="box",
                creds=[Cred(login="u", password="p")],
                log=LogMode.QUIET,
            )

            async def close_all() -> None:
                await points.point("sessions")

            async def conn_close() -> None:
                await points.point("connections")

            h._session_mgr.close_all = close_all
            h._connections.close = conn_close
            await h.close()

        def oracle(points, outcome, exc_type, k) -> None:
            expected = [s for i, s in enumerate(steps) if i != k - 1]
            assert points.executed == expected, f"step behind {steps[k - 1]!r} was skipped"
            assert isinstance(outcome, exc_type), "the failure must stay loud"

        await sweep_cancellation(scenario, oracle)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/host/test_unix_host.py::TestClose -v`
Expected: the two new tests FAIL — `conn_close` never awaited / the sweep's "step behind 'sessions' was skipped" assertion — existing TestClose tests still pass.

- [ ] **Step 3: Implement**

In `src/otto/host/unix_host.py`, replace `close`:

```python
    @override
    async def close(self) -> None:
        # Sessions first, transports second — and the transports MUST close
        # even when a session refuses to (chaos spec: teardown chain
        # robustness). The session failure still propagates afterwards.
        try:
            await self._session_mgr.close_all()
        finally:
            await self._connections.close()
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/host/test_unix_host.py -v`
Expected: all PASS.

- [ ] **Step 5: Gate and commit**

Run: `uv run nox -s lint && make typecheck-python && make coverage`
Expected: all green.

```bash
git add src/otto/host/unix_host.py tests/unit/host/test_unix_host.py
git commit -m "fix(host): UnixHost.close closes transports even when sessions fail

close_all() -> _connections.close() was an unguarded sequence: a wedged
session leaked every raw transport behind it. try/finally keeps the
failure loud while the transports still close. Tier-1 sweep included.

Assisted-by: Claude (Fable 5)"
```

---

### Task 4: `HostScope.__aexit__` — ranked close, per-host failure logging

The sweep currently gathers all closes concurrently, contradicting `DockerContainerHost.close`'s documented close-before-parent requirement, and swallows every failure silently (`return_exceptions=True`, results dropped). Close in dependency ranks — hosts no *remaining* host names as `parent` close first — gathering within each rank, and log every failed close naming the host.

**Files:**
- Modify: `src/otto/context.py:70-82` (`HostScope.__aexit__`; add `logging` import + module logger)
- Test: `tests/unit/test_context.py` (append)

**Interfaces:**
- Consumes: Task 1's harness.
- Produces: `HostScope.__aexit__` with unchanged signature; children close strictly before the hosts they reference via a `parent` attribute; per-host close failures are logged at WARNING on the `otto.context` logger; drain-on-exit and the `_connected` filter are preserved.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_context.py` (add `import logging` and `from tests._fixtures.chaos import ChaosPoints, sweep_cancellation` to its imports; `asyncio`, `pytest`, `HostScope` are already imported):

```python
class _ScopedHost:
    """Standalone fake for ranked-sweep tests: records close order into a shared list."""

    def __init__(
        self,
        name: str,
        order: "list[str]",
        *,
        parent: "object | None" = None,
        fail: bool = False,
        yields: int = 0,
    ) -> None:
        self.id = name
        self._order = order
        self._fail = fail
        self._yields = yields
        if parent is not None:
            self.parent = parent

    async def close(self) -> None:
        for _ in range(self._yields):
            await asyncio.sleep(0)
        self._order.append(self.id)
        if self._fail:
            raise RuntimeError(f"{self.id}: close blew up")


@pytest.mark.asyncio
async def test_hostscope_closes_children_before_their_parent():
    """DockerContainerHost.close documents close-before-parent (its docker
    exec channel drains over the parent's still-open transport); the sweep
    must honor it. The child here closes SLOWER than its parent would, so a
    naive concurrent gather finishes the parent first."""
    order: "list[str]" = []
    parent = _ScopedHost("parent", order)
    child = _ScopedHost("child", order, parent=parent, yields=2)
    scope = HostScope()
    scope.register(child)
    scope.register(parent)
    async with scope:
        pass
    assert order == ["child", "parent"]


@pytest.mark.asyncio
async def test_hostscope_ranks_a_three_level_parent_chain():
    order: "list[str]" = []
    top = _ScopedHost("top", order)
    mid = _ScopedHost("mid", order, parent=top, yields=1)
    leaf = _ScopedHost("leaf", order, parent=mid, yields=2)
    scope = HostScope()
    scope.register(top)
    scope.register(mid)
    scope.register(leaf)
    async with scope:
        pass
    assert order == ["leaf", "mid", "top"]


@pytest.mark.asyncio
async def test_hostscope_child_close_failure_still_closes_the_parent(caplog):
    """One host's close dying must be LOGGED (named) and must not stop the
    remaining ranks — silent swallowing is what this plan removes."""
    order: "list[str]" = []
    parent = _ScopedHost("parent", order)
    child = _ScopedHost("child", order, parent=parent, fail=True)
    scope = HostScope()
    scope.register(child)
    scope.register(parent)
    with caplog.at_level(logging.WARNING, logger="otto.context"):
        async with scope:
            pass
    assert order == ["child", "parent"]
    assert any("'child'" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_hostscope_sweep_chain():
    """Tier-1 sweep: one host's close dying (drop OR injected cancel) never
    skips the other hosts — per-rank gather captures per-host failures."""
    names = ["h1", "h2", "h3"]

    class _PointHost:
        def __init__(self, name: str, points: ChaosPoints) -> None:
            self.id = name
            self._points = points

        async def close(self) -> None:
            await self._points.point(self.id)

    async def scenario(points: ChaosPoints) -> None:
        scope = HostScope()
        for name in names:
            scope.register(_PointHost(name, points))
        async with scope:
            pass

    def oracle(points, outcome, exc_type, k) -> None:
        # Both variants: an injected failure inside ONE host's close is
        # indistinguishable from that close dying — it is captured, logged,
        # and the sweep continues. (Force-abandon cancels the sweep TASK,
        # which is a different mechanism and still aborts everything.)
        assert outcome is None, f"{exc_type.__name__} at host {k} escaped the sweep"
        assert points.executed == [n for i, n in enumerate(names) if i != k - 1]

    await sweep_cancellation(scenario, oracle)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_context.py -v`
Expected: the ordering tests FAIL (`['parent', 'child'] != ['child', 'parent']`), the logging test FAILS (no warning records); `test_hostscope_sweep_chain` PASSES already (the existing gather also captures) — it is the regression guard for the new per-rank shape. Existing tests still pass.

- [ ] **Step 3: Implement the ranked sweep**

In `src/otto/context.py`, add near the top (after the existing imports):

```python
import logging
```

and, after the `T = TypeVar("T")` line:

```python
logger = logging.getLogger(__name__)
```

Replace `HostScope.__aexit__`:

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
        remaining = [h for h in hosts if getattr(h, "_connected", True)]
        # Dependency-ranked sweep (chaos spec): a host that another registered
        # host names as its ``parent`` (DockerContainerHost documents
        # close-before-parent — its docker exec channel drains over the
        # parent's still-open transport) closes only after its dependents.
        # Within a rank closes run concurrently; failures are logged per host
        # — never silently swallowed — and never stop the remaining ranks.
        while remaining:
            parent_ids = {id(getattr(h, "parent", None)) for h in remaining}
            rank = [h for h in remaining if id(h) not in parent_ids]
            if not rank:
                rank = remaining  # parent cycle (impossible by construction): close all, don't spin
            results = await asyncio.gather(
                *(h.close() for h in rank), return_exceptions=True
            )
            for host, result in zip(rank, results, strict=True):
                if isinstance(result, BaseException):
                    logger.warning(
                        f"otto: closing host {getattr(host, 'id', host)!r} failed "
                        f"during scope sweep: {result!r}"
                    )
            closed = {id(h) for h in rank}
            remaining = [h for h in remaining if id(h) not in closed]
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_context.py -v`
Expected: all PASS (including the pre-existing drain / `_connected` / missing-attr tests).

- [ ] **Step 5: Gate and commit**

Run: `uv run nox -s lint && make typecheck-python && make coverage`
Expected: all green.

```bash
git add src/otto/context.py tests/unit/test_context.py
git commit -m "fix(context): rank HostScope sweep children-before-parent, log failures

The scope sweep gathered all closes concurrently — contradicting
DockerContainerHost's documented close-before-parent requirement — and
dropped every failure silently. Closes now run in dependency ranks
(hosts no remaining host names as parent first) with per-host WARNING
logging; drain-on-exit and the _connected filter are unchanged.

Assisted-by: Claude (Fable 5)"
```

---

### Task 5: `compensate()` — shielded, deadline-bounded compensating actions

The spec's helper: `await compensate(coro, deadline)` runs a rollback/undo to completion even if the caller is cancelled mid-flight. Cancellation is HELD (the inner work continues under `asyncio.shield`) and re-raised once the compensation resolves; the first held cancellation arms a deadline so a hung compensation cannot stall teardown forever.

**Files:**
- Modify: `src/otto/lifecycle.py` (add `logging` import + logger; add `compensate` after the force-exit hook registry)
- Test: `tests/unit/test_lifecycle_compensate.py` (new)

**Interfaces:**
- Consumes: `_resolve_teardown_deadline()` (Plan 1, same module).
- Produces (used by Tasks 6–9): `async compensate(coro: Coroutine[Any, Any, R], *, deadline: float | None = None, what: str = "compensating action") -> R` — `deadline=None` resolves `OTTO_TEARDOWN_DEADLINE`; returns the coroutine's result; re-raises any held cancellation after the compensation resolves (abandonment on deadline expiry re-raises the held cancellation too — it never returns `None`); passes results/exceptions through unchanged when no cancellation arrives.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_lifecycle_compensate.py`:

```python
"""compensate(): rollback/undo runs to completion even when the caller is cancelled.

Chaos spec (shielded compensating actions): an interrupt mid-compensation
must not tear the rollback; a hung rollback is bounded by a deadline; both
paths re-raise the held cancellation once the compensation resolves.
Deterministic: expiry is driven by ``deadline=0`` (the call_later fires on
the next loop turn), never by wall-clock waits.
"""

import asyncio

import pytest

from otto.lifecycle import compensate


@pytest.mark.asyncio
async def test_result_passthrough_without_cancellation():
    async def rollback() -> str:
        return "undone"

    assert await compensate(rollback(), deadline=60.0, what="test rollback") == "undone"


@pytest.mark.asyncio
async def test_exception_passthrough_without_cancellation():
    async def rollback() -> None:
        raise ValueError("undo failed")

    with pytest.raises(ValueError, match="undo failed"):
        await compensate(rollback(), deadline=60.0, what="test rollback")


@pytest.mark.asyncio
async def test_cancellation_is_held_until_the_rollback_completes():
    started = asyncio.Event()
    release = asyncio.Event()
    done: "list[bool]" = []

    async def rollback() -> None:
        started.set()
        await release.wait()
        done.append(True)

    task = asyncio.ensure_future(compensate(rollback(), deadline=60.0, what="test rollback"))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)  # let the cancel land in compensate's shield
    assert not done  # rollback still parked on its event — held, not torn, not done
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert done == [True]  # the rollback finished BEFORE the cancellation re-raised


@pytest.mark.asyncio
async def test_second_cancellation_does_not_tear_the_rollback():
    started = asyncio.Event()
    release = asyncio.Event()
    done: "list[bool]" = []

    async def rollback() -> None:
        started.set()
        await release.wait()
        done.append(True)

    task = asyncio.ensure_future(compensate(rollback(), deadline=60.0, what="test rollback"))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()  # a second cancel is the shield's whole point
    await asyncio.sleep(0)
    assert not done
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert done == [True]


@pytest.mark.asyncio
async def test_deadline_abandons_a_hung_rollback(caplog):
    hung = asyncio.Event()  # never set

    async def rollback() -> None:
        await hung.wait()

    task = asyncio.ensure_future(compensate(rollback(), deadline=0.0, what="hung rollback"))
    await asyncio.sleep(0)  # rollback parked
    task.cancel()  # holds the cancel and arms the deadline; 0.0 fires next loop turn
    with caplog.at_level("WARNING", logger="otto.lifecycle"):
        with pytest.raises(asyncio.CancelledError):
            await task
    assert any("hung rollback" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_cancellation_wins_over_a_late_rollback_failure(caplog):
    started = asyncio.Event()
    release = asyncio.Event()

    async def rollback() -> None:
        started.set()
        await release.wait()
        raise ValueError("undo failed late")

    task = asyncio.ensure_future(compensate(rollback(), deadline=60.0, what="late failure"))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with caplog.at_level("WARNING", logger="otto.lifecycle"):
        with pytest.raises(asyncio.CancelledError):
            await task
    assert any("late failure" in r.message for r in caplog.records)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_lifecycle_compensate.py -v`
Expected: FAIL at import — `ImportError: cannot import name 'compensate'`.

- [ ] **Step 3: Implement `compensate`**

In `src/otto/lifecycle.py`, add to the imports:

```python
import logging
```

and after the `R = TypeVar("R")` line:

```python
logger = logging.getLogger(__name__)
```

Add after `_run_force_exit_hooks` (before `_INTERRUPT_STATUS_LINE`):

```python
async def compensate(
    coro: "Coroutine[Any, Any, R]",
    *,
    deadline: "float | None" = None,
    what: str = "compensating action",
) -> "R":
    """Run a rollback/undo coroutine to completion even if the caller is cancelled.

    The chaos spec's shielded-compensating-action helper: an interrupt
    landing mid-compensation must not tear it (a half-run rollback is worse
    than none). Cancellation arriving while *coro* runs is HELD — the inner
    work continues under ``asyncio.shield`` — and re-raised once the
    compensation resolves. The first held cancellation arms *deadline*
    (``None`` resolves ``OTTO_TEARDOWN_DEADLINE``) so a hung compensation
    cannot stall teardown: on expiry the inner task is cancelled, the
    abandonment is logged, and the held cancellation still re-raises. With
    no cancellation this is a plain await — results and exceptions pass
    through unchanged. Once a cancellation is held it wins over a late
    compensation failure (the failure is logged, the cancellation
    re-raises).

    Tier-1 determinism: expiry is a ``call_later`` armed only when a
    cancellation is held — tests drive it with ``deadline=0`` (fires on the
    next loop turn), never wall-clock waits.
    """
    task = asyncio.ensure_future(coro)
    held: "asyncio.CancelledError | None" = None
    timer: "asyncio.TimerHandle | None" = None
    try:
        while True:
            try:
                result = await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                if not task.cancelled():
                    # OUR wrapper was cancelled, not the work: hold the
                    # cancellation, keep the work running, bound it.
                    if held is None:
                        held = exc
                        bound = _resolve_teardown_deadline() if deadline is None else deadline
                        timer = asyncio.get_running_loop().call_later(bound, task.cancel)
                    continue
                # The deadline (or loop shutdown) cancelled the work itself.
                logger.warning(f"otto: {what} abandoned before completion")
                if held is not None:
                    raise held from None
                raise
            except Exception:  # noqa: BLE001 — outcome deferred: a held cancellation wins, else re-raised as-is
                if held is not None:
                    logger.warning(
                        f"otto: {what} failed during shielded unwind", exc_info=True
                    )
                    raise held from None
                raise
            if held is not None:
                raise held from None
            return result
    finally:
        if timer is not None:
            timer.cancel()
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_lifecycle_compensate.py tests/unit/test_lifecycle.py -v`
Expected: all PASS.

- [ ] **Step 5: Gate and commit**

Run: `uv run nox -s lint && make typecheck-python && make coverage`
Expected: all green.

```bash
git add src/otto/lifecycle.py tests/unit/test_lifecycle_compensate.py
git commit -m "feat(lifecycle): compensate() shields rollbacks from cancellation

await compensate(coro, deadline) holds any cancellation until the
rollback/undo completes (asyncio.shield), bounds a hung compensation by
the teardown deadline, and re-raises the held cancellation afterwards.
Call sites (tunnel/link rollback, as_user undo, nc reap) follow in the
next tasks (chaos spec: shielded compensating actions).

Assisted-by: Claude (Fable 5)"
```

---

### Task 6: `add_tunnel` rollback runs under `compensate()`

`add_tunnel`'s `except BaseException` already reaps launched processes on failure — but the reap itself is an interruptible await: a Ctrl+C landing *during* the rollback tears it and leaves half-tunnels. Wrap it.

**Files:**
- Modify: `src/otto/tunnel/manage.py:476-479` (the `except BaseException` block in `add_tunnel`; add the import)
- Test: `tests/unit/tunnel/test_manage_add.py` (append to `TestRollback`)

**Interfaces:**
- Consumes: Task 5's `compensate`.
- Produces: no signature changes; `add_tunnel`'s rollback completes even when a further cancellation arrives mid-reap.

- [ ] **Step 1: Write the failing test**

Append to `class TestRollback` in `tests/unit/tunnel/test_manage_add.py` (the file already imports `asyncio`, `pytest`, `FREE_PORT_PROBE_COMMAND`, `DISCOVERY_PS_COMMAND`, `Direction`, `Role`, `_ps_line`, `_pair`, `_LO`):

```python
    @pytest.mark.asyncio
    async def test_cancel_during_rollback_still_reaps(self) -> None:
        """A cancellation landing DURING the rollback reap must not tear it
        (lifecycle.compensate shield): the kill still reaches the host that
        actually launched a process (spec §6.4 — no half-tunnels)."""
        lab, _calls, tunnel = _pair()
        a, b = lab.hosts["a"], lab.hosts["b"]
        # Rollback scans: a never started anything; b's FWD egress is running.
        a.ps_texts = ["", ""]
        b.ps_texts = ["", _ps_line(tunnel, Direction.FWD, Role.EGRESS, 1, _LO, 999)]

        launch_started = asyncio.Event()
        rollback_scanning = asyncio.Event()

        def _gate(host) -> None:
            orig = host.exec  # bound method of the FakeHost instance

            async def gated(cmd: str, timeout: "float | None" = None, **kw: object):
                if cmd == DISCOVERY_PS_COMMAND and launch_started.is_set():
                    # The rollback's reap scan: rendezvous so the test can
                    # deliver the second cancel mid-rollback, then proceed.
                    rollback_scanning.set()
                    await asyncio.sleep(0)
                elif not (
                    "command -v" in cmd
                    or cmd == FREE_PORT_PROBE_COMMAND
                    or cmd == DISCOVERY_PS_COMMAND
                    or cmd.startswith("kill ")
                ):
                    # A launch command: park until the test cancels the add.
                    launch_started.set()
                    await asyncio.Event().wait()
                return await orig(cmd, timeout=timeout, **kw)

            host.exec = gated  # type: ignore[method-assign]

        _gate(a)
        _gate(b)

        task = asyncio.ensure_future(add_tunnel(lab, [("a", None), ("b", None)], port=8080))
        await launch_started.wait()
        task.cancel()  # 1st cancel: tears the parked launch, triggers rollback
        await rollback_scanning.wait()
        task.cancel()  # 2nd cancel: lands inside the shielded reap — must be held
        with pytest.raises(asyncio.CancelledError):
            await task
        assert any(cmd.startswith("kill ") for cmd in b.commands), (
            "the second cancel tore the rollback reap"
        )
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/tunnel/test_manage_add.py::TestRollback -v`
Expected: the new test FAILS — the second cancel lands in `_kill_tunnel_on`'s scan await and aborts the reap, so no `kill` command reaches host b. Existing rollback tests still pass.

- [ ] **Step 3: Implement**

In `src/otto/tunnel/manage.py`, add to the relative imports at the top of the file:

```python
from ..lifecycle import compensate
```

Replace the `except BaseException` block at the end of `add_tunnel`:

```python
        except BaseException:
            if launched:
                # Shielded: a Ctrl+C landing during the rollback itself must
                # not tear it — the reap runs to completion (bounded by the
                # teardown deadline) before the cancellation continues
                # (chaos spec: shielded compensating actions).
                await compensate(
                    _kill_tunnel_on([r.host for r in resolved], tunnel.id),
                    what=f"tunnel {tunnel.id} rollback",
                )
            raise
```

- [ ] **Step 4: Run the tunnel tests**

Run: `uv run pytest tests/unit/tunnel/ -v`
Expected: all PASS.

- [ ] **Step 5: Gate and commit**

Run: `uv run nox -s lint && make typecheck-python && make coverage`
Expected: all green.

```bash
git add src/otto/tunnel/manage.py tests/unit/tunnel/test_manage_add.py
git commit -m "fix(tunnel): shield add_tunnel rollback with compensate()

The launch-failure reap was itself an interruptible await: a Ctrl+C
landing during the rollback tore it and left half-tunnels running.
compensate() holds the cancellation until the reap completes.

Assisted-by: Claude (Fable 5)"
```

---

### Task 7: `impair_link` rolls back on cancellation, shielded

`impair_link`'s no-half-impairments restore hangs off `except Exception:` — a `CancelledError` (Ctrl+C) between placements skips the rollback entirely, leaving a half-impaired link. Catch `BaseException` and shield the restore.

**Files:**
- Modify: `src/otto/link/manage.py:644-646` (the `except Exception` block in `impair_link`; add the import)
- Test: `tests/unit/link/test_manage_impair.py` (append to `TestRefusalsAndSafety`, next to `test_rollback_restores_prior_state_on_partial_failure`)

**Interfaces:**
- Consumes: Task 5's `compensate`.
- Produces: no signature changes; cancellation mid-impair now restores every touched placement to its prior state before propagating.

- [ ] **Step 1: Write the failing test**

Append to `class TestRefusalsAndSafety` in `tests/unit/link/test_manage_impair.py` (add `import asyncio` to the file's imports):

```python
    @pytest.mark.asyncio
    async def test_cancellation_mid_impair_still_rolls_back(self) -> None:
        """Ctrl+C between placements (CancelledError) must trigger the same
        no-half-impairments restore an Exception does; the restore itself is
        shielded by lifecycle.compensate. Today `except Exception` misses
        cancellation entirely and leaves the first placement half-impaired."""
        lab, carrot, tomato, _ = _bed()
        carrot.qdisc_texts = [
            "qdisc netem 8001: root refcnt 2 limit 1000 delay 20ms\n",  # prior state
            "qdisc netem 8001: root refcnt 2 limit 1000 delay 50ms\n",  # verify ok
        ]

        second_placement_reached = asyncio.Event()

        def _park(host) -> None:
            """Hang every non-addr call on *host* until the test cancels."""
            async def parked(cmd: str, timeout: "float | None" = None, **kw: object):
                if cmd == "ip -o addr show":
                    return host._result(cmd)
                second_placement_reached.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")  # parked forever; cancel unwinds

            host.exec = parked  # type: ignore[method-assign]
            host.run = parked  # type: ignore[method-assign]

        _park(tomato)

        task = asyncio.ensure_future(impair_link(lab, "edge", ImpairmentParams(delay_ms=50.0)))
        await second_placement_reached.wait()  # carrot's placement is fully applied by now
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # carrot (the completed first placement) restored to its PRIOR params
        assert carrot.sudo_commands[-1] == "tc qdisc replace dev eth1.100 root netem delay 20ms"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/link/test_manage_impair.py -v`
Expected: the new test FAILS — with `except Exception:` the cancellation skips `_rollback`, so carrot's last sudo command is the 50 ms apply, not the 20 ms restore. Existing tests still pass.

- [ ] **Step 3: Implement**

In `src/otto/link/manage.py`, add to the relative imports:

```python
from ..lifecycle import compensate
```

Replace the `except Exception` block in `impair_link`:

```python
    except BaseException:
        # BaseException, not Exception: a Ctrl+C (CancelledError) mid-impair
        # must trigger the same no-half-impairments restore. compensate()
        # shields the restore from a further interrupt (chaos spec: shielded
        # compensating actions) and re-raises the cancellation after.
        await compensate(
            _rollback(link.id, rollback_entries, selector=selector),
            what=f"link {link.id} rollback",
        )
        raise
```

- [ ] **Step 4: Run the link tests**

Run: `uv run pytest tests/unit/link/ -v`
Expected: all PASS.

- [ ] **Step 5: Gate and commit**

Run: `uv run nox -s lint && make typecheck-python && make coverage`
Expected: all green.

```bash
git add src/otto/link/manage.py tests/unit/link/test_manage_impair.py
git commit -m "fix(link): roll back impairments on cancellation, shielded

impair_link's no-half-impairments restore hung off 'except Exception',
so a Ctrl+C between placements skipped the rollback and left the link
half-impaired. BaseException + compensate(): cancellation restores every
touched placement, and the restore itself survives a further interrupt.

Assisted-by: Claude (Fable 5)"
```

---

### Task 8: `as_user` undo chain runs under `compensate()`

The `finally` block of `as_user` unwinds the applied `su` hops with plain awaits: a cancellation landing mid-undo strands the session as the wrong user — every later command on that session then runs with the wrong identity. Extract the undo into `_undo_switch` and run it via `compensate()`.

**Files:**
- Modify: `src/otto/host/privilege.py:153-166` (the `finally` block of `as_user`; new `_undo_switch` method; add the import)
- Test: `tests/unit/host/test_privilege.py` (append)

**Interfaces:**
- Consumes: Task 5's `compensate`.
- Produces: `PosixPrivilege._undo_switch(applied: list[Cred], prev: str) -> None` (async); `as_user` behavior unchanged on the happy path (same undo ordering, same `_set_current_user(prev)` restore).

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/host/test_privilege.py` (the file already imports `pytest`, `AsyncMock`/`MagicMock` via `_mock_session_mgr`, `LogMode`, and defines `_MULTI_HOP_CREDS`; add `import asyncio` to its imports):

```python
@pytest.mark.asyncio
async def test_as_user_undo_survives_cancellation():
    """A cancellation landing while the undo chain runs must not strand the
    session as the switched user: every hop still unwinds and current_user
    is restored (the undo is a shielded compensating action)."""
    from otto.host.unix_host import UnixHost

    host = UnixHost(
        ip="10.0.0.1", element="box", creds=_MULTI_HOP_CREDS, user="root", log=LogMode.QUIET
    )
    mgr = _mock_session_mgr()
    mgr.current_user = "root"

    async def _yielding_send(*_a, **_k) -> None:
        await asyncio.sleep(0)  # a real suspension per send, so a cancel CAN land mid-undo

    mgr.send.side_effect = _yielding_send
    host._session_mgr = mgr

    inside = asyncio.Event()
    release = asyncio.Event()

    async def body() -> None:
        async with host.as_user("mysql"):
            inside.set()
            await release.wait()

    task = asyncio.ensure_future(body())
    await inside.wait()
    task.cancel()  # lands at release.wait(); the finally-undo starts
    await asyncio.sleep(0)
    task.cancel()  # second cancel, mid-undo: must be held by compensate
    with pytest.raises(asyncio.CancelledError):
        await task

    sent = [c.args[0] for c in mgr.send.await_args_list]
    assert sent.count("exit\n") == 2, "the undo chain was torn mid-unwind"
    set_user_calls = [c.args[0] for c in mgr._set_current_user.call_args_list]
    assert set_user_calls == ["mysql", "root"]  # entered as mysql, restored to root
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/host/test_privilege.py -v`
Expected: the new test FAILS — the second cancel lands in the finally's `run_undo` await and tears the unwind (`exit` count < 2, no restore to `"root"`). Existing tests still pass.

- [ ] **Step 3: Implement**

In `src/otto/host/privilege.py`, add to the relative imports:

```python
from ..lifecycle import compensate
```

Replace `as_user`'s `try/finally` tail (from `try:` through the end of the method) and add the extracted undo method after it:

```python
        try:
            yield self
        finally:
            # The undo chain is a compensating action: an interrupt landing
            # while it runs must not strand the session as the switched user
            # (chaos spec: shielded compensating actions). compensate() holds
            # the cancellation until every hop is unwound (bounded by the
            # teardown deadline), then re-raises it.
            await compensate(
                self._undo_switch(applied, prev),
                what=f"{getattr(self, 'name', '')}: as_user undo to {prev or 'login user'!r}",
            )

    async def _undo_switch(self, applied: "list[Cred]", prev: str) -> None:
        """Unwind *applied* innermost-first, restoring ``current_user`` to *prev*."""
        creds = self._switch_creds()
        for i, hop in enumerate(reversed(applied)):
            via_login = applied[-i - 2].login if i + 1 < len(applied) else prev
            # Look up the full via cred (password/params intact), mirroring
            # perform_switch's forward path — so a custom undo that needs
            # the via user's password sees it, and forward/undo stay symmetric.
            via = cred_for(creds, via_login) or Cred(login=via_login)
            await run_undo(
                _HostProxyIO(self), hop, via, getattr(self, "name", ""), self._history_prefix()
            )
        self._session_mgr._set_current_user(prev)  # noqa: SLF001 — intra-package access to SessionManager._set_current_user to restore prior user  # ty: ignore[unresolved-attribute]
```

(The loop body is the existing `finally` code moved verbatim; only the `compensate` wrapper is new.)

- [ ] **Step 4: Run the privilege tests**

Run: `uv run pytest tests/unit/host/test_privilege.py tests/unit/host/test_login_proxy.py -v`
Expected: all PASS (in particular `test_as_user_restores_previous_user` and `test_as_user_multi_hop_undoes_in_reverse` — the happy-path ordering is unchanged).

- [ ] **Step 5: Gate and commit**

Run: `uv run nox -s lint && make typecheck-python && make coverage`
Expected: all green.

```bash
git add src/otto/host/privilege.py tests/unit/host/test_privilege.py
git commit -m "fix(host): shield the as_user undo chain with compensate()

A cancellation landing mid-undo tore the unwind and stranded the session
as the switched user — every later command on that session ran with the
wrong identity. The undo chain (extracted to _undo_switch, behavior
unchanged) now runs under compensate().

Assisted-by: Claude (Fable 5)"
```

---

### Task 9: cancelled nc put reaps its listener under `compensate()`

`_put_files_nc`'s `except asyncio.CancelledError` handler joins the listener task and reaps the remote `nc -l` — with plain awaits. A second cancellation tears the reap and strands the listener until its `-w` timeout after all. Extract the join+reap into `_cancel_and_reap` and run it via `compensate()`.

**Files:**
- Modify: `src/otto/host/transfer/nc.py:955-970` (the `except asyncio.CancelledError` handler in `_attempt`; new `_cancel_and_reap` method; add the import)
- Test: `tests/unit/host/test_transfer_nc_put.py` (append)

**Interfaces:**
- Consumes: Task 5's `compensate`.
- Produces: `NcFileTransfer._cancel_and_reap(listen_task: asyncio.Task, port: int) -> None` (async); the cancel-handler behavior is otherwise unchanged (same suppression, same reap, `raise` still propagates the cancellation).

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/host/test_transfer_nc_put.py` (the file already imports `asyncio`, `pytest`, `AsyncMock`, `patch`, `transfer_mod`, `NcFileTransfer`, `_make_ft`, `_ok`):

```python
class TestNcPutCancellationReap:
    """A cancelled put must reap its remote listener even under a second cancel."""

    @pytest.mark.asyncio
    async def test_cancel_mid_put_reaps_listener_despite_second_cancel(self, tmp_path: Path):
        src = tmp_path / "small.bin"
        src.write_bytes(b"hello world")

        connect_reached = asyncio.Event()
        reap_started = asyncio.Event()
        reap_done: "list[bool]" = []

        async def scripted_exec(cmd: str, timeout: "float | None" = None, **kw: object):
            if " -l " in cmd:
                await asyncio.Event().wait()  # the remote listener runs until reaped
            return _ok("9000\n")

        async def parked_connect(host: str, port: int, timeout: float = 2.0):
            connect_reached.set()
            await asyncio.Event().wait()  # park until the test cancels the put
            raise AssertionError("unreachable")

        async def recording_reap(self, port: int) -> None:
            reap_started.set()
            await asyncio.sleep(0)  # a real suspension: a torn reap stops HERE
            reap_done.append(True)

        exec_cmd = AsyncMock(side_effect=scripted_exec)
        ft = _make_ft(exec_cmd)

        with (
            patch.object(transfer_mod, "_connect_with_retry", new=parked_connect),
            patch.object(
                NcFileTransfer, "_wait_for_remote_listener", new=AsyncMock(return_value=None)
            ),
            patch.object(NcFileTransfer, "_reap_nc_listener", new=recording_reap),
        ):
            task = asyncio.ensure_future(ft._put_files_nc([src], tmp_path / "dst"))
            await connect_reached.wait()
            task.cancel()  # 1st cancel: tears the transfer, triggers the reap
            await reap_started.wait()
            task.cancel()  # 2nd cancel: lands during the reap — must be held
            with pytest.raises(asyncio.CancelledError):
                await task

        assert reap_done == [True], "the second cancellation tore the listener reap"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/host/test_transfer_nc_put.py -v`
Expected: the new test FAILS — the second cancel lands in the handler's reap await, so `reap_done` stays empty. Existing tests still pass.

- [ ] **Step 3: Implement**

In `src/otto/host/transfer/nc.py`, add to the relative imports:

```python
from ...lifecycle import compensate
```

Add the helper method to `NcFileTransfer`, directly after `_reap_nc_listener`:

```python
    async def _cancel_and_reap(self, listen_task: "asyncio.Task[CommandResult]", port: int) -> None:
        """Join a cancelled put's listener task, then reap the remote ``nc -l``."""
        listen_task.cancel()
        with suppress(Exception):
            await asyncio.gather(listen_task, return_exceptions=True)
        with suppress(Exception):
            await self._reap_nc_listener(port)
```

Replace the `except asyncio.CancelledError` handler in `_put_files_nc._attempt` (keep the existing comment, extend its tail):

```python
            except asyncio.CancelledError:
                # External cancellation mid-transfer skips listen_task's
                # normal join points (the success / ConnectionError / timeout
                # branches below the create_task). Cancel it and reap the
                # remote `nc -l` so it doesn't linger until its `-w` timeout.
                # A writer opened in the send loop is already closed by that
                # loop's own `finally` — which makes nc exit on its own — so
                # this matters mainly for a cancel landing before the sender
                # ever connects. compensate() holds any FURTHER cancellation
                # until the reap resolves (chaos spec: shielded compensating
                # actions) — without it a second Ctrl+C tears the reap and
                # strands the listener after all.
                if listen_task is not None and not listen_task.done():
                    await compensate(
                        self._cancel_and_reap(listen_task, port),
                        what=f"{self._name}: nc listener reap (port {port})",
                    )
                raise
```

- [ ] **Step 4: Run the nc transfer tests**

Run: `uv run pytest tests/unit/host/test_transfer_nc_put.py tests/unit/host/test_transfer_nc_get.py tests/unit/host/test_transfer_port.py -v`
Expected: all PASS.

- [ ] **Step 5: Gate and commit**

Run: `uv run nox -s lint && make typecheck-python && make coverage`
Expected: all green.

```bash
git add src/otto/host/transfer/nc.py tests/unit/host/test_transfer_nc_put.py
git commit -m "fix(host): shield the cancelled nc put's listener reap

The CancelledError handler joined the listener task and reaped the
remote nc -l with plain awaits — a second Ctrl+C tore the reap and
stranded the listener until its -w timeout after all. The join+reap
(extracted to _cancel_and_reap) now runs under compensate().

Assisted-by: Claude (Fable 5)"
```

---

### Task 10: external cancellation sweeps the scope (Plan 1 carry-over)

`_CommandRun._main` re-raises an external `CancelledError` (task-level cancel with no signal seen) BEFORE the scope sweep — every scope-registered host leaks, and the body task is left dangling. Unreachable from the POSIX main-thread CLI (cancellation only arrives via `_on_signal` there), but real for embedders and for any future in-loop use. Sweep first, cancel the body, re-raise after.

**Files:**
- Modify: `src/otto/lifecycle.py:153-155` (the `except asyncio.CancelledError` arm in `_CommandRun._main`)
- Test: `tests/unit/test_lifecycle.py` (append)

**Interfaces:**
- Consumes: Plan 1's `_CommandRun` (already in the module).
- Produces: on external cancellation `_main` cancels and joins the body, runs the scope sweep, then re-raises the `CancelledError`; the signal path is untouched.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_lifecycle.py` (it already imports `asyncio`, `pytest`, and `_CommandRun`; add the context imports shown if the file doesn't have them):

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_lifecycle.py -v`
Expected: the new test FAILS — `closed == []` (the old arm re-raises before the sweep). Existing lifecycle tests still pass.

- [ ] **Step 3: Implement**

In `src/otto/lifecycle.py`, replace the `except asyncio.CancelledError` arm inside `_CommandRun._main`:

```python
            except asyncio.CancelledError as exc:
                if self.interrupted is None:
                    # External task-level cancellation, not our signal (an
                    # embedder cancelled the command task — unreachable from
                    # the POSIX main-thread CLI, where cancellation only
                    # arrives via _on_signal). The cancel hit OUR await, not
                    # necessarily the body's: cancel and join the body, then
                    # let the sweep below run; the cancellation re-raises
                    # after it (body_error path).
                    self._body.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._body
                    body_error = exc
```

(The signal-path behavior — `interrupted is not None` falls through to the sweep — is unchanged.)

- [ ] **Step 4: Run the lifecycle tests**

Run: `uv run pytest tests/unit/test_lifecycle.py tests/unit/test_context.py -v`
Expected: all PASS.

- [ ] **Step 5: Gate and commit**

Run: `uv run nox -s lint && make typecheck-python && make coverage`
Expected: all green.

```bash
git add src/otto/lifecycle.py tests/unit/test_lifecycle.py
git commit -m "fix(lifecycle): sweep the host scope on external cancellation

_CommandRun._main re-raised an external CancelledError before the scope
sweep, leaking every registered host and leaving the body task dangling.
Now: cancel+join the body, sweep, then re-raise (Plan 1 carry-over;
unreachable from the POSIX main-thread CLI, real for embedders).

Assisted-by: Claude (Fable 5)"
```

---

### Task 11: `suite/run.py` drops dead per-loop host state before the post-run sweep (Plan 1 carry-over)

Hosts a suite registers during the in-process pytest session were opened on pytest's own event loops — closed by the time `run_command(_post_run_coverage(...))` sweeps them on a fresh loop. A cross-loop close can only fail (asyncssh schedules on its dead home loop), and used to fail *silently* into `return_exceptions=True`. Task 4 made those failures loud; this task removes them: rebuild the registered hosts' connection state (the documented `rebuild_connections` pattern `otto test --cov` already uses) before the post-run sweep, so the sweep closes only what the current loop actually owns.

**Files:**
- Modify: `src/otto/context.py` (new `HostScope.rebuild_connections`)
- Modify: `src/otto/suite/run.py:588-599` (`run_suite`) and `:675-707` (`run_selection`)
- Test: `tests/unit/test_context.py`, `tests/unit/suite/test_run_api.py` (append)

**Interfaces:**
- Consumes: `UnixHost.rebuild_connections()` / `DockerContainerHost.rebuild_connections()` (existing product API; duck-typed via `getattr`).
- Produces: `HostScope.rebuild_connections() -> None` (sync) — calls `rebuild_connections()` on every registered host that has one, leaves others untouched; `run_suite`/`run_selection` call it after their pytest session(s), before the post-run `run_command`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_context.py`:

```python
def test_hostscope_rebuild_connections_hits_every_host_with_the_hook():
    scope = HostScope()
    calls: "list[str]" = []

    class _WithHook:
        def __init__(self, name: str) -> None:
            self.id = name

        def rebuild_connections(self) -> None:
            calls.append(self.id)

    class _WithoutHook:
        id = "plain"

    scope.register(_WithHook("a"))
    scope.register(_WithoutHook())
    scope.register(_WithHook("b"))
    scope.rebuild_connections()
    assert calls == ["a", "b"]
```

Append to `tests/unit/suite/test_run_api.py`:

```python
def test_run_suite_rebuilds_scope_hosts_registered_by_the_inner_session(tmp_path, monkeypatch):
    """Hosts a suite registers during the in-process pytest session were
    opened on pytest's own (now-closed) loops — run_suite must drop that
    per-loop state (rebuild_connections) BEFORE the post-run sweep closes
    them on a fresh loop (Plan 1 carry-over: cross-loop closes can only fail)."""
    import otto.config
    from otto.config.lab import Lab
    from otto.context import OttoContext, reset_context, set_context, try_get_context

    monkeypatch.setattr(otto.config, "get_repos", list)

    events: "list[str]" = []

    class _SuiteHost:
        id = "bed1"

        def rebuild_connections(self) -> None:
            events.append("rebuild")

        async def close(self) -> None:
            events.append("close")

    def fake_pytest_main(args, **_kw):
        # Simulate a suite calling ctx.get_host(...) mid-session.
        active = try_get_context()
        assert active is not None
        active.scope.register(_SuiteHost())
        return pytest.ExitCode.OK

    monkeypatch.setattr("pytest.main", fake_pytest_main)

    token = set_context(OttoContext(lab=Lab(name="test")))
    try:

        class _Suite:
            pass

        run_suite(_Suite, output_dir=tmp_path)
    finally:
        reset_context(token)

    assert events == ["rebuild", "close"], (
        "dead per-loop state must be dropped BEFORE the post-run sweep closes the host"
    )
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_context.py tests/unit/suite/test_run_api.py -v`
Expected: the context test FAILS (`HostScope` has no `rebuild_connections`); the run_api test FAILS with `events == ["close"]`. Existing tests still pass.

- [ ] **Step 3: Implement**

In `src/otto/context.py`, add to `HostScope` (after `register`):

```python
    def rebuild_connections(self) -> None:
        """Drop per-loop connection state on every registered host.

        For hosts opened inside an inner pytest session (``otto test`` /
        ``run_suite``): their transports are bound to pytest's now-closed
        event loops, and no later loop can drive them — a cross-loop close
        only raises into the sweep's failure logging. Rebuilding (the same
        ``rebuild_connections`` pattern ``otto test --cov`` already uses to
        refresh hosts after ``pytest.main()`` returns) abandons the dead
        per-loop state so the post-run sweep closes only what the CURRENT
        loop actually owns. Real remote cleanup for suite-opened hosts
        belongs to the suite's own fixtures, on the loop that opened them.
        Hosts without the hook (fakes, minimal BaseHosts) are left as-is.
        """
        for host in self._hosts:
            rebuild = getattr(host, "rebuild_connections", None)
            if rebuild is not None:
                rebuild()
```

In `src/otto/suite/run.py`, in `run_suite`, between `outcome = _run_pytest_session(...)` and `run_command(_post_run_coverage(...))`:

```python
        # Hosts the in-process pytest session registered were opened on
        # pytest's own (now-closed) event loops; drop that dead per-loop
        # state so the post-run sweep below doesn't attempt cross-loop
        # closes (they can only fail — see HostScope.rebuild_connections).
        session_ctx = try_get_context()
        if session_ctx is not None:
            session_ctx.scope.rebuild_connections()
        run_command(_post_run_coverage(repos, log_dir, run_options))
```

and add `try_get_context` to `run_suite`'s local imports:

```python
    from ..context import try_get_context
```

In `run_selection`, make the identical change — after the `for match in per_repo:` loop ends, replace its `run_command(_post_run_coverage(repos, log_dir, opts))` line with:

```python
        # Hosts the in-process pytest sessions registered were opened on
        # pytest's own (now-closed) event loops; drop that dead per-loop
        # state so the post-run sweep below doesn't attempt cross-loop
        # closes (they can only fail — see HostScope.rebuild_connections).
        session_ctx = try_get_context()
        if session_ctx is not None:
            session_ctx.scope.rebuild_connections()
        run_command(_post_run_coverage(repos, log_dir, opts))
```

and add `from ..context import try_get_context` to `run_selection`'s local imports (next to its existing `from ..config import get_repos`).

- [ ] **Step 4: Run the suite tests**

Run: `uv run pytest tests/unit/suite/ tests/unit/test_context.py -v`
Expected: all PASS.

- [ ] **Step 5: Gate and commit**

Run: `uv run nox -s lint && make typecheck-python && make coverage`
Expected: all green.

```bash
git add src/otto/context.py src/otto/suite/run.py tests/unit/test_context.py tests/unit/suite/test_run_api.py
git commit -m "fix(suite): drop dead per-loop host state before the post-run sweep

Hosts registered by the in-process pytest session live on pytest's
now-closed loops; the post-run run_command sweep runs on a fresh loop,
where closing them can only fail. run_suite/run_selection now rebuild
registered hosts' connection state (the existing otto-test-cov pattern)
via HostScope.rebuild_connections() before the sweep.

Assisted-by: Claude (Fable 5)"
```

---

## Out of scope (later plans)

- Tier-2 real-signal integration harness, loopback-or-bed host fixture, `tests/integration/chaos/` → Plan 3 (which also owns the Plan 1 carry-overs listed for it: force-by-losing-the-race semantics, third-signal bypass, uvicorn `capture_signals` ownership, `_on_signal`'s `except OSError` breadth).
- `nox -s chaos` / `make chaos`, BedHygiene oracle, seeded injection → Plan 4.
- Docker + privilege/login chaos scenarios → Plan 5.
- The accepted trade-off from Plan 1 (one stale idempotent force-hook per exceptional-but-unforced login unwind) is documented in-code and unchanged here.
