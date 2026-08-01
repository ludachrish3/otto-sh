# Chaos Plan 6: Reboot Hardening Implementation Plan

> **Status: APPROVED by Chris 2026-07-31** ("I think this looks good for plan 6"), with one review amendment applied: the down-timeout default lives in a named module constant, `DEFAULT_REBOOT_DOWN_TIMEOUT = 60.0` in `src/otto/host/host.py`, so it has a central place to view and change. The other reviewed judgment calls stand as drafted: the `exec("true")` recovery probe, and the two new `@cli_exposed` keyword parameters (`down_timeout`, `poll_interval`). The "Spec amendment" section below is mirrored into `docs/superpowers/specs/2026-07-30-chaos-hardening-design.md`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `Host.reboot(wait=True)` truthful and robust — probes that dial fresh instead of reading dead caches, a two-phase down-then-up wait, and shell-liveness-gated recovery — with deterministic tier-1 tests for every pitfall; the bed/docker reboot *scenarios* ride Plans 4/5 per the spec amendment below.

**Architecture:** All product changes sit in the existing reboot pipeline: `BaseHost.reboot` (`src/otto/host/host.py:1003`) orchestrates; `UnixHost._soft_reboot` issues the in-shell command; `is_reachable`/`wait_until_up`/`wait_until_down` probe. Three defects get fixed in order: (Task 1) reachability probes read `ConnectionManager`'s cached connection — `ssh()` has no aliveness check — so after issuing a reboot every probe vacuously succeeds; fix = `rebuild_connections()` at issue time + disconnect-tolerant `_soft_reboot`. (Task 2) `reboot(wait=True)` never waits for *down*, so a probe landing before the link drops declares success against the old OS; fix = two-phase wait with distinct failure messages. (Task 3) "reachable" means "accepted a connection", which early-boot sshd does and then stalls; fix = a `_confirm_recovered` hook, default permissive, overridden on `UnixHost` as a bounded `exec("true")` retry loop. Reboot scenarios against real hosts are deliberately NOT tasks here (see the amendment): the deterministic failure modes are unit-testable now; the end-to-end scenarios need Plan 3's phase markers and Plan 4's BedHygiene and land in those plans' scenario sets.

**Tech Stack:** Python 3.10 asyncio, pytest + pytest-asyncio (strict mode), existing test scaffolding in `tests/unit/host/test_power.py` (scripted `is_reachable` sequences with `interval=0`, monkeypatched `_soft_reboot`).

## Spec amendment (lift into the chaos spec on approval)

**Reboot becomes the 8th chaos surface.** Rationale: early termination of the *remote* host is the dual of every surface the spec already covers — a reboot is the remote-side SIGKILL. Daemons (tunnels), qdiscs and transient timers (link impairment), and cached transports do not survive it; the product cannot make them survive, so scenarios characterize what is lost and assert the recovery commands reconcile cleanly, exactly the spec's SIGKILL pattern.

- **Tier 1 (this plan):** the deterministic pitfalls — probe-through-dead-cache, up-before-down race, accept-then-stall false start — as unit tests with scripted probes, `poll_interval=0`, no wall clock.
- **Tier 2/3 scenario catalog additions (ride Plans 4/5 when those plan documents are written):**
  - *Bed (lab leg, `make chaos`):* happy-path `reboot(wait=True)` on the leased bed host; reboot delivered at phase markers mid-`run` and mid-transfer (sessions die at different points — asserts loud, named failures, then recovery); reboot × tunnel — reboot one chain host, assert discovery reports the half-tunnel and `otto tunnel remove --all` reaps the survivors on the non-rebooted hosts; reboot × link — reboot one endpoint, assert its own qdiscs/timers are gone, the peer's remain, BedHygiene names them, and `repair-link` is idempotent against the half-clean state.
  - *Docker analog (GitHub nightly, loopback-sshd runner):* `docker restart` of a container host mid-exec and mid-session — the CI-viable reboot stand-in (the runner itself cannot reboot); asserts loud session failure, clean recovery via rebuilt connections, no stack/staging accumulation across N restart cycles.
- **Venue rule:** real reboots are bed-only, against leased bed hosts exclusively — never dev/lab infrastructure; the docker-restart analog is the only reboot-shaped scenario GitHub CI runs.

## Global Constraints

- Python floor is 3.10 (`requires-python = ">=3.10"`). `X | None` annotations are fine; `asyncio.Runner`, `asyncio.timeout`, `except*` are NOT.
- NEVER add `from __future__ import annotations` (breaks Sphinx nitpicky `-W`; repo-wide ban).
- Tests count work, never wall-clock time: scripted `is_reachable`/`exec` sequences resolve in a fixed number of probes; every wait is driven with `poll_interval=0` (pure `sleep(0)` yields); timeout-expiry paths use `timeout=0` or sub-100ms bounded budgets with instant-failing probes. No test performs real network I/O — `ssh_connect` (`src/otto/host/connections.py:163`) is the patchable dial seam.
- **Unit tests only in this plan. Never reboot, power-cycle, or probe real lab hosts while implementing** (test1/2/3/zephyr are off-limits without Chris's explicit say-so; this plan needs none of them).
- Behavior changes are user-visible and belong in Chris's merge notes: `reboot(wait=True)` gains a down-phase (a reboot that "never took" now fails loudly instead of instantly succeeding) and a liveness gate (success now means "shell answers", not "TCP accepts"); two new keyword params appear on the CLI surface (`down_timeout`, `poll_interval` — floats, Typer-safe, no Unions).
- pytest-asyncio strict mode: every async test carries `@pytest.mark.asyncio`.
- Lint suppressions are a failure mode: prefer restructuring; a `# noqa` needs a written justification on the same line (the two specified below are the only ones this plan introduces).
- Per-task gate: `make coverage` (there is no `make test`). Scoped pytest passing is NOT sufficient — run the full gate before each task's final commit. Also `uv run nox -s lint` and `make typecheck-python` after any `src/` edit.
- Never `git push`. Commit in the worktree with a conventional prefix; end every commit message with the trailer: `Assisted-by: Claude (Fable 5)`
- Worktree setup quirks: EnterWorktree branches from **origin/main** — run `git reset --hard main` immediately after entering; fresh worktrees need `uv sync` and `npm ci` in `web/` before `make coverage`.

## File Structure

| File | Role in this plan |
| --- | --- |
| `src/otto/host/host.py` | `BaseHost.reboot` rewrite (rebuild-at-issue, two-phase wait, recovery gate call); `_confirm_recovered` default |
| `src/otto/host/unix_host.py` | Disconnect-tolerant `_soft_reboot`; `_confirm_recovered` exec-probe override; module logger |
| `tests/unit/host/test_reboot_recovery.py` (new) | All new tests — the reboot pipeline storylines and pitfall regressions |
| `tests/unit/host/test_power.py` | Existing reboot/wait tests — two of them patch only `wait_until_up` and gain a `wait_until_down` patch in Task 2 (exact edits given there); everything else stays green unchanged |

---

### Task 1: Truthful probes — drop stale connection state at reboot issue

Today `reboot(wait=True)` on an SSH host is vacuous: `_soft_reboot` caches a live connection while issuing `reboot`, and `wait_until_up` → `is_reachable` → `verify_connection` → `ConnectionManager.ssh()` returns that cached object with **no aliveness check** (`connections.py`, the `if self._ssh_conn is not None: return self._ssh_conn` fast path) — so the first probe "succeeds" without touching the network, forever. Also: issuing `reboot` races the transport teardown, so `run("reboot")` may raise on a fast host; today that exception escapes `_soft_reboot`. Fix both, and thread `poll_interval` through so later tasks (and tests) can drive the waits deterministically.

**Files:**
- Modify: `src/otto/host/host.py:1003-1024` (`BaseHost.reboot`)
- Modify: `src/otto/host/unix_host.py:889-892` (`_soft_reboot`; add `import logging` + module logger near the top imports)
- Test: `tests/unit/host/test_reboot_recovery.py` (new)

**Interfaces:**
- Consumes: `RemoteHost.rebuild_connections()` (`unix_host.py:398` — drops the ConnectionManager/SessionManager/file-transfer trio for lazy re-open), `wait_until_up(timeout, interval)` (`host.py:1045`).
- Produces (Tasks 2-3 build on this): `reboot(self, hard: bool = False, wait: bool = False, timeout: float = 600.0, poll_interval: float = 2.0) -> Result` — after issuing (soft or hard), calls `rebuild_connections()` when the host has one, BEFORE any wait; `wait_until_up` receives `interval=poll_interval`. `UnixHost._soft_reboot` never lets the issue-race exception escape.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/host/test_reboot_recovery.py`:

```python
"""Reboot pipeline hardening: truthful probes, two-phase wait, liveness gate.

Chaos plan 6. Everything here is deterministic: scripted probe sequences,
``poll_interval=0`` (pure scheduling yields), the patchable ``ssh_connect``
dial seam — no real network, no wall-clock waits, and NEVER a real host.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

import otto.host.connections as connections_mod
from otto.host.local_host import LocalHost
from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode
from otto.result import CommandResult, Result
from otto.utils import Status


def _unix_host() -> UnixHost:
    return UnixHost(
        ip="10.0.0.1", element="box", creds=[Cred(login="u", password="p")], log=LogMode.QUIET
    )


def _ok(cmd: str = "true") -> CommandResult:
    return CommandResult(status=Status.Success, value="", command=cmd, retcode=0)


def _timed_out(cmd: str = "true") -> CommandResult:
    return CommandResult(
        status=Status.Error, value="timed out", command=cmd, retcode=-1, timed_out=True
    )


@pytest.mark.asyncio
async def test_soft_reboot_tolerates_connection_drop(monkeypatch):
    """Issuing `reboot` races the transport teardown: a raise from run() is
    indistinguishable from the host obeying quickly, so it must be tolerated
    — the (later) down-wait is the loud check for 'never actually took'."""
    h = _unix_host()
    monkeypatch.setattr(
        h, "run", AsyncMock(side_effect=ConnectionResetError("dropped mid-command"))
    )
    result = await h._soft_reboot()
    assert result.status == Status.Success


@pytest.mark.asyncio
async def test_reboot_rebuilds_connections_before_waiting(monkeypatch):
    """The cached transports are dead the moment the reboot is issued; every
    later probe must dial fresh. Order matters: issue, then rebuild, then wait."""
    h = _unix_host()
    order: "list[str]" = []
    monkeypatch.setattr(
        h, "_soft_reboot", AsyncMock(side_effect=lambda: order.append("issue") or Result(Status.Success))
    )
    monkeypatch.setattr(h, "rebuild_connections", lambda: order.append("rebuild"))

    async def up(self, timeout=10.0):
        order.append("probe")
        return True

    monkeypatch.setattr(type(h), "is_reachable", up)
    await h.reboot(wait=False)
    assert order == ["issue", "rebuild"]  # no wait requested: no probe, but rebuild still ran


@pytest.mark.asyncio
async def test_reboot_wait_does_not_trust_the_cached_connection(monkeypatch):
    """THE headline regression: with a cached (dead) ssh connection and a
    dial seam that refuses every fresh attempt, reboot(wait=True) must FAIL —
    today it vacuously succeeds because verify_connection() reads the cache."""
    h = _unix_host()
    monkeypatch.setattr(h, "_soft_reboot", AsyncMock(return_value=Result(Status.Success)))
    h._connections._ssh_conn = MagicMock()  # the dead cached connection
    monkeypatch.setattr(
        connections_mod, "ssh_connect", AsyncMock(side_effect=ConnectionRefusedError("booting"))
    )
    result = await h.reboot(wait=True, timeout=0.05, poll_interval=0)
    assert result.status == Status.Failed
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/host/test_reboot_recovery.py -v`
Expected: `test_soft_reboot_tolerates_connection_drop` FAILS (the `ConnectionResetError` escapes `_soft_reboot`); `test_reboot_rebuilds_connections_before_waiting` FAILS (`order == ["issue"]` — no rebuild call exists); `test_reboot_wait_does_not_trust_the_cached_connection` FAILS (result is Success — the cached connection satisfied the probe). Note: after Task 2 lands, the third test's failure message changes character (the two-phase wait fails at the down phase instead) — that is fine; the assertion is on `Status.Failed` either way.

- [ ] **Step 3: Implement**

In `src/otto/host/unix_host.py`, add to the imports (with the other stdlib imports):

```python
import logging
```

and after the imports, next to the other module-level names:

```python
logger = logging.getLogger(__name__)
```

Replace `_soft_reboot`:

```python
    @override
    async def _soft_reboot(self) -> Result:
        # Issuing `reboot` races the connection teardown: on a fast host the
        # transport can drop before the command's round-trip completes, and
        # that failure is indistinguishable from the host obeying quickly.
        # Tolerate it — reboot(wait=True)'s down-wait is the loud check for
        # "the command never actually took".
        try:
            await self.run("reboot", sudo=True, timeout=10.0)
        except Exception as e:  # noqa: BLE001 — expected issue-race disconnect; the down-wait disambiguates
            logger.debug(f"{self.name}: connection dropped while issuing reboot ({e})")
        return Result(Status.Success)
```

In `src/otto/host/host.py`, replace `BaseHost.reboot` (keep the `@cli_exposed` decorator):

```python
    @cli_exposed
    async def reboot(
        self,
        hard: bool = False,
        wait: bool = False,
        timeout: float = 600.0,
        poll_interval: float = 2.0,
    ) -> Result:
        """Reboot this host.

        ``hard=False`` (default) issues the in-shell reboot command
        (``_soft_reboot``); ``hard=True`` power-cycles via the
        :class:`~otto.host.power.PowerController`. When *wait*, block until
        the host is reachable again (up to *timeout*, probing every
        *poll_interval* seconds); on expiry the result is downgraded to
        :attr:`~otto.utils.Status.Failed`.
        """
        if hard:
            result = await self._require_power_control().cycle(cast("Host", self))
        else:
            result = await self._soft_reboot()
        # The just-issued reboot kills every cached transport, but the caches
        # don't know it: ConnectionManager.ssh() returns a cached connection
        # object without an aliveness check, so any reachability probe below
        # would read the dead cache and vacuously succeed. Drop the stale
        # per-connection state now — probes must dial fresh.
        rebuild = getattr(self, "rebuild_connections", None)
        if rebuild is not None:
            rebuild()
        if result.is_ok and wait and not await self.wait_until_up(timeout, interval=poll_interval):
            return Result(
                Status.Failed,
                msg=f"{self.name!r} did not become reachable within {timeout}s after reboot",
            )
        return result
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/host/test_reboot_recovery.py tests/unit/host/test_power.py -v`
Expected: all PASS (test_power.py's `test_unix_soft_reboot_issues_reboot_sudo` and `test_hard_reboot_cycles_controller` exercise the happy paths and must stay green).

- [ ] **Step 5: Gate and commit**

Run: `uv run nox -s lint && make typecheck-python && make coverage`
Expected: all green.

```bash
git add src/otto/host/host.py src/otto/host/unix_host.py tests/unit/host/test_reboot_recovery.py
git commit -m "fix(host): reboot probes dial fresh instead of reading dead caches

reboot(wait=True) probed reachability through ConnectionManager's cached
connection — ssh() has no aliveness check — so the wait vacuously
succeeded the moment a reboot was issued over a cached transport. reboot
now drops per-connection state at issue time (rebuild_connections), and
UnixHost._soft_reboot tolerates the issue-race disconnect instead of
letting it escape. poll_interval threads through for deterministic tests.

Assisted-by: Claude (Fable 5)"
```

---

### Task 2: Two-phase wait — down, then up, with distinct failures

`reboot(wait=True)` goes straight to `wait_until_up`: a probe landing in the seconds before the link actually drops sees the old OS answering and declares success just as the host goes dark. Wait for *down* first (bounded by `down_timeout`), then *up* (bounded by the remaining overall budget), and fail each phase with a message naming what actually happened.

**Files:**
- Modify: `src/otto/host/host.py` (`BaseHost.reboot` — the `wait` branch from Task 1)
- Modify: `tests/unit/host/test_power.py` (`test_reboot_wait_timeout_downgrades_to_failed`, `test_reboot_wait_success_keeps_status` — Step 3a)
- Test: `tests/unit/host/test_reboot_recovery.py` (append)

**Interfaces:**
- Consumes: Task 1's `reboot` shape; `wait_until_down(timeout, interval)` (`host.py:1054`).
- Produces (Task 3 extends the same branch): `DEFAULT_REBOOT_DOWN_TIMEOUT = 60.0` module constant in `host.py`; `reboot(self, hard=False, wait=False, timeout=600.0, down_timeout=DEFAULT_REBOOT_DOWN_TIMEOUT, poll_interval=2.0) -> Result`; failure messages: `"never went down within {down_timeout}s"` (reboot didn't take) vs `"did not become reachable within {timeout}s"` (never came back).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/host/test_reboot_recovery.py`:

```python
def _scripted_reachable(monkeypatch, host, sequence: "list[bool]") -> None:
    """Drive is_reachable from a finite script; the last value repeats."""
    seq = list(sequence)

    async def fake(self, timeout=10.0):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(type(host), "is_reachable", fake)


def _reboot_ready_local(monkeypatch) -> LocalHost:
    """A LocalHost whose _soft_reboot is a recorder — drives BaseHost.reboot's
    orchestration without any real family behavior."""
    h = LocalHost()
    monkeypatch.setattr(h, "_soft_reboot", AsyncMock(return_value=Result(Status.Success)))
    return h


@pytest.mark.asyncio
async def test_reboot_wait_requires_down_before_up(monkeypatch):
    """Old OS still answering (True), then down (False), then back (True):
    the wait must observe the down transition and then succeed."""
    h = _reboot_ready_local(monkeypatch)
    _scripted_reachable(monkeypatch, h, [True, False, True])
    result = await h.reboot(wait=True, timeout=5.0, down_timeout=5.0, poll_interval=0)
    assert result.status == Status.Success


@pytest.mark.asyncio
async def test_reboot_that_never_takes_fails_loudly(monkeypatch):
    """Host never goes down: the up-before-down race of the old code turned
    this into instant false success; now it must fail naming the down phase."""
    h = _reboot_ready_local(monkeypatch)
    _scripted_reachable(monkeypatch, h, [True])  # reachable forever
    result = await h.reboot(wait=True, timeout=0.05, down_timeout=0.05, poll_interval=0)
    assert result.status == Status.Failed
    assert "never went down" in (result.msg or "")


@pytest.mark.asyncio
async def test_reboot_that_never_returns_fails_loudly(monkeypatch):
    """Host goes down and stays down: the failure must name the up phase."""
    h = _reboot_ready_local(monkeypatch)
    _scripted_reachable(monkeypatch, h, [False])  # down immediately, forever
    result = await h.reboot(wait=True, timeout=0.05, down_timeout=5.0, poll_interval=0)
    assert result.status == Status.Failed
    assert "did not become reachable" in (result.msg or "")
```

- [ ] **Step 2: Run to verify the split**

Run: `uv run pytest tests/unit/host/test_reboot_recovery.py -v`
Expected: `test_reboot_wait_requires_down_before_up` PASSES already (single-phase up-wait sees the eventual True — it is the regression guard); `test_reboot_that_never_takes_fails_loudly` FAILS (old behavior: first probe True → instant Success, and no "never went down" message exists); `test_reboot_that_never_returns_fails_loudly` PASSES already on status but FAILS on the message only if the wording differs — keep the existing "did not become reachable" wording so it passes untouched.

- [ ] **Step 3: Implement**

In `src/otto/host/host.py`, add a module constant next to `DEFAULT_COMMAND_TIMEOUT` (Chris's review request — one central place to view and change it):

```python
DEFAULT_REBOOT_DOWN_TIMEOUT = 60.0
"""Default bound (seconds) for ``reboot(wait=True)``'s down phase — a host
still reachable this long after the reboot command means the reboot didn't
take, and the wait fails loudly instead of watching the old OS."""
```

Then replace the `wait` branch of `reboot` (and extend the signature and docstring):

```python
    @cli_exposed
    async def reboot(
        self,
        hard: bool = False,
        wait: bool = False,
        timeout: float = 600.0,
        down_timeout: float = DEFAULT_REBOOT_DOWN_TIMEOUT,
        poll_interval: float = 2.0,
    ) -> Result:
        """Reboot this host.

        ``hard=False`` (default) issues the in-shell reboot command
        (``_soft_reboot``); ``hard=True`` power-cycles via the
        :class:`~otto.host.power.PowerController`. When *wait*, block through
        a two-phase watch: first the host must go DOWN (within *down_timeout*
        — a host that never goes down means the reboot didn't take), then
        come back UP (within the remainder of *timeout*), probing every
        *poll_interval* seconds. Either phase expiring downgrades the result
        to :attr:`~otto.utils.Status.Failed` with a message naming the phase.
        """
        if hard:
            result = await self._require_power_control().cycle(cast("Host", self))
        else:
            result = await self._soft_reboot()
        # The just-issued reboot kills every cached transport, but the caches
        # don't know it: ConnectionManager.ssh() returns a cached connection
        # object without an aliveness check, so any reachability probe below
        # would read the dead cache and vacuously succeed. Drop the stale
        # per-connection state now — probes must dial fresh.
        rebuild = getattr(self, "rebuild_connections", None)
        if rebuild is not None:
            rebuild()
        if result.is_ok and wait:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            if not await self.wait_until_down(
                min(down_timeout, timeout), interval=poll_interval
            ):
                return Result(
                    Status.Failed,
                    msg=(
                        f"{self.name!r} never went down within {down_timeout}s of the "
                        f"reboot — the reboot likely did not take"
                    ),
                )
            remaining = max(0.0, deadline - loop.time())
            if not await self.wait_until_up(remaining, interval=poll_interval):
                return Result(
                    Status.Failed,
                    msg=f"{self.name!r} did not become reachable within {timeout}s after reboot",
                )
        return result
```

(`asyncio` is already imported in `host.py` — `wait_until_up` uses it.)

- [ ] **Step 3a: Update the two existing wait-path tests**

Both `test_reboot_wait_timeout_downgrades_to_failed` (test_power.py:348) and `test_reboot_wait_success_keeps_status` (test_power.py:362) patch only `wait_until_up`; with the two-phase wait the REAL `wait_until_down` now runs first. In the timeout test it fails the down phase and changes the message (breaking its `"reachable" in result.msg` assertion); in the success test `LocalHost.is_reachable` is always True, so the un-patched down phase would spin the full down-phase budget in wall clock. In BOTH tests, add directly above the `await host.reboot(...)` line:

```python
    async def went_down(self, timeout, interval=2.0):
        return True

    monkeypatch.setattr(type(host), "wait_until_down", went_down)
```

No assertion changes — with the down phase satisfied, the timeout test still fails at the up phase with the "reachable" message, and the success test still succeeds.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/host/test_reboot_recovery.py tests/unit/host/test_power.py -v`
Expected: all PASS. Task 1's `test_reboot_wait_does_not_trust_the_cached_connection` now fails at the DOWN phase (the fresh dial refuses, so the host reads as already down, then never comes up) — still `Status.Failed`, still green.

- [ ] **Step 5: Gate and commit**

Run: `uv run nox -s lint && make typecheck-python && make coverage`
Expected: all green.

```bash
git add src/otto/host/host.py tests/unit/host/test_reboot_recovery.py tests/unit/host/test_power.py
git commit -m "feat(host): two-phase reboot wait — down, then up, distinct failures

reboot(wait=True) went straight to wait_until_up, so a probe landing
before the link dropped saw the OLD OS and declared success as the host
went dark. The wait now demands the down transition first (bounded by
down_timeout — a host that never goes down means the reboot didn't
take), then reachability within the remaining budget, each phase failing
with a message naming what happened.

Assisted-by: Claude (Fable 5)"
```

---

### Task 3: Liveness-gated recovery — "accepts TCP" is not "booted"

`wait_until_up` succeeds on `is_reachable`, i.e. a completed connection attempt — and early-boot sshd (or a socket-activated stub) can accept a connection and then stall immediately after. Add a `_confirm_recovered` hook to the pipeline: the default accepts reachability (families with no stronger probe keep today's semantics — embedded hosts' `exec` speaks RTOS shells where `true` means nothing); `UnixHost` overrides it with a bounded `exec("true")` retry loop on the fresh post-rebuild connection.

**Files:**
- Modify: `src/otto/host/host.py` (`_confirm_recovered` default next to `wait_until_down`; call it from `reboot`'s wait branch)
- Modify: `src/otto/host/unix_host.py` (override + module constant)
- Test: `tests/unit/host/test_reboot_recovery.py` (append)

**Interfaces:**
- Consumes: Tasks 1-2's `reboot` shape; `BaseHost.exec(cmd, timeout, log)` (`host.py:746`).
- Produces: `BaseHost._confirm_recovered(self, deadline: float, poll_interval: float) -> bool` (async; *deadline* is a loop-clock instant, not a duration); `UnixHost` override; new failure message `"accepts connections but its shell never answered"`; `_RECOVERY_PROBE_TIMEOUT = 10.0` module constant in `unix_host.py`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/host/test_reboot_recovery.py`:

```python
@pytest.mark.asyncio
async def test_unix_recovery_survives_accept_then_stall(monkeypatch):
    """The false start: sshd accepts (reachable=True) but the shell stalls.
    Recovery must retry the command probe until it answers — two stalled
    probes then a clean round-trip is a recovery, not a failure."""
    h = _unix_host()
    monkeypatch.setattr(h, "_soft_reboot", AsyncMock(return_value=Result(Status.Success)))
    _scripted_reachable(monkeypatch, h, [True, False, True])
    monkeypatch.setattr(
        h, "exec", AsyncMock(side_effect=[_timed_out(), _timed_out(), _ok()])
    )
    result = await h.reboot(wait=True, timeout=5.0, down_timeout=5.0, poll_interval=0)
    assert result.status == Status.Success
    assert h.exec.await_count == 3


@pytest.mark.asyncio
async def test_unix_recovery_fails_when_shell_never_answers(monkeypatch):
    """Reachable forever, shell never answers: the failure must say so —
    this is the accept-then-stall pitfall made loud."""
    h = _unix_host()
    monkeypatch.setattr(h, "_soft_reboot", AsyncMock(return_value=Result(Status.Success)))
    _scripted_reachable(monkeypatch, h, [True, False, True])
    monkeypatch.setattr(h, "exec", AsyncMock(return_value=_timed_out()))
    result = await h.reboot(wait=True, timeout=0.05, down_timeout=0.05, poll_interval=0)
    assert result.status == Status.Failed
    assert "shell never answered" in (result.msg or "")


@pytest.mark.asyncio
async def test_unix_recovery_probe_tolerates_raising_exec(monkeypatch):
    """A probe that RAISES (connection reset mid-handshake) is 'not booted
    yet', never an error: the loop retries and later succeeds."""
    h = _unix_host()
    monkeypatch.setattr(h, "_soft_reboot", AsyncMock(return_value=Result(Status.Success)))
    _scripted_reachable(monkeypatch, h, [True, False, True])
    monkeypatch.setattr(
        h, "exec", AsyncMock(side_effect=[ConnectionResetError("reset"), _ok()])
    )
    result = await h.reboot(wait=True, timeout=5.0, down_timeout=5.0, poll_interval=0)
    assert result.status == Status.Success


@pytest.mark.asyncio
async def test_default_recovery_gate_keeps_reachability_semantics(monkeypatch):
    """Families without a shell probe (LocalHost stands in) recover on
    reachability alone — the base hook must not demand an exec round-trip."""
    h = _reboot_ready_local(monkeypatch)
    _scripted_reachable(monkeypatch, h, [True, False, True])
    result = await h.reboot(wait=True, timeout=5.0, down_timeout=5.0, poll_interval=0)
    assert result.status == Status.Success
```

- [ ] **Step 2: Run to verify the split**

Run: `uv run pytest tests/unit/host/test_reboot_recovery.py -v`
Expected: `test_unix_recovery_survives_accept_then_stall` FAILS (`exec.await_count == 0` — nothing calls a probe today); `test_unix_recovery_fails_when_shell_never_answers` FAILS (result is Success — reachable is treated as recovered); `test_unix_recovery_probe_tolerates_raising_exec` FAILS (no probe); `test_default_recovery_gate_keeps_reachability_semantics` PASSES already (regression guard for exotic families).

- [ ] **Step 3: Implement**

In `src/otto/host/host.py`, add after `wait_until_down`:

```python
    async def _confirm_recovered(self, deadline: float, poll_interval: float) -> bool:
        """Post-reboot recovery gate: is the host USABLE, not merely reachable?

        The default accepts reachability as recovery — the right call for
        families with no stronger probe (an RTOS shell has no ``true``).
        :class:`~otto.host.unix_host.UnixHost` overrides this with a real
        command round-trip: early-boot sshd can accept a TCP connection and
        then stall, so "accepts a connection" must never be the recovery
        criterion where a shell probe exists. *deadline* is an asyncio
        loop-clock instant (``loop.time()`` scale), not a duration.
        """
        return True
```

In `reboot`'s wait branch, after the `wait_until_up` check and before `return result`:

```python
            if not await self._confirm_recovered(deadline, poll_interval):
                return Result(
                    Status.Failed,
                    msg=(
                        f"{self.name!r} accepts connections but its shell never answered "
                        f"within {timeout}s of the reboot — likely still booting"
                    ),
                )
```

In `src/otto/host/unix_host.py`, add near the other module-level constants:

```python
_RECOVERY_PROBE_TIMEOUT = 10.0
"""Per-attempt bound for the post-reboot shell probe (`exec \"true\"`)."""
```

and add the override in the Power / reboot section (after `_soft_reboot`):

```python
    @override
    async def _confirm_recovered(self, deadline: float, poll_interval: float) -> bool:
        # "Accepts a connection" is not "booted": early-boot sshd (or a
        # socket-activated stub) can accept and then stall immediately after.
        # Recovery = one clean command round-trip on the fresh post-rebuild
        # connection, retried until the deadline; a raising probe (refused,
        # reset mid-handshake) is just "not yet", never an error.
        loop = asyncio.get_running_loop()
        while loop.time() < deadline:
            try:
                result = await self.exec(
                    "true", timeout=_RECOVERY_PROBE_TIMEOUT, log=LogMode.QUIET
                )
            except Exception:  # noqa: BLE001 — probe failure means "not booted yet"; the deadline is the arbiter
                result = None
            if result is not None and result.status.is_ok:
                return True
            await asyncio.sleep(poll_interval)
        return False
```

`unix_host.py` does NOT currently import `asyncio` — add `import asyncio` to its stdlib imports (next to `import re` / `import socket`). `LogMode` is already imported there (`from ..logger.mode import LogMode`).

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/host/test_reboot_recovery.py tests/unit/host/test_power.py -v`
Expected: all PASS.

- [ ] **Step 5: Gate and commit**

Run: `uv run nox -s lint && make typecheck-python && make coverage`
Expected: all green.

```bash
git add src/otto/host/host.py src/otto/host/unix_host.py tests/unit/host/test_reboot_recovery.py
git commit -m "feat(host): liveness-gated reboot recovery — accepts-TCP is not booted

wait_until_up succeeds on a completed connection attempt, and early-boot
sshd can accept then stall. reboot(wait=True) now finishes through a
_confirm_recovered gate: UnixHost retries an exec(true) round-trip on
the fresh post-rebuild connection until the deadline; families with no
shell probe keep reachability semantics via the permissive default.

Assisted-by: Claude (Fable 5)"
```

---

## Out of scope (rides other plans)

- Bed reboot scenarios (happy path, phase-marker mid-command reboots, reboot × tunnel, reboot × link) — Plan 4's scenario set, per the spec amendment above; they need Plan 3's phase markers and Plan 4's BedHygiene.
- Docker-restart analog scenarios — Plan 5's docker set.
- `EmbeddedHost` recovery semantics (serial-console boot banners, `kernel reboot cold`) — the permissive `_confirm_recovered` default deliberately leaves them unchanged; a Zephyr-aware probe is future work if Chris wants it.
- `DockerContainerHost.reboot` (a container "reboot" = `docker restart` via the parent) — not defined today, unchanged here; the Plan 5 scenarios drive restarts through the parent host directly.
