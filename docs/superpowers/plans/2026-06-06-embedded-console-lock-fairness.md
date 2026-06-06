# Embedded console lock fairness + timeout-safe teardown — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the starvation-prone `flock` console lock with a writer-fair turnstile lock, and add a synchronous abort net so a timed-out test can't leave a half-open single-client console.

**Architecture:** A turnstile-gated reader/writer `flock` (writers hold a gate while waiting, so readers can't starve them) plus a per-process registry of live single-client console transports that the embedded-only test teardown force-aborts. Both are embedded-scoped; unix telnet is untouched.

**Tech Stack:** Python `fcntl.flock`, asyncio transports (`transport.abort()`), pytest / pytest-asyncio / pytest-xdist (`--dist loadgroup`), pytest-timeout (signal method).

**No-self-commit:** do NOT `git commit` in this repo — the prepare-commit-msg hook needs `/dev/tty` and agent commits mistag the AI-assist field. Each task's final step **stages** the listed files (`git add`) and hands off a paste-able commit message for Chris to run.

**Spec:** [`docs/superpowers/specs/2026-06-06-embedded-console-lock-fairness-design.md`](../specs/2026-06-06-embedded-console-lock-fairness-design.md)

---

## File structure

| File | Responsibility |
|------|----------------|
| `src/otto/host/options.py` | add `TelnetOptions.single_client_console` marker field |
| `src/otto/host/telnet.py` | per-process console-transport registry; register in `connect()`, discard in `close()`; `abort_console_transports()` |
| `src/otto/host/embeddedHost.py` | set `single_client_console=True` on the embedded console's telnet options |
| `tests/integration/host/_console_lock.py` | **new** — `console_access()` turnstile fair lock |
| `tests/integration/host/conftest.py` | rewire `_console_access_lock` to the primitive + call the abort net in teardown |
| `tests/unit/host/test_options.py` | flag default/override test |
| `tests/unit/host/test_telnet_client.py` | registry + connect/close wiring tests |
| `tests/unit/host/test_console_lock.py` | **new** — fairness + reader-parallelism tests |
| `todo/nox-all-failure-triage-2026-06-01.md` | record Issue 1 confirmed + fixed |

---

## Task 1: `TelnetOptions.single_client_console` flag

**Files:**
- Modify: `src/otto/host/options.py` (the `TelnetOptions` dataclass, after the `login` field)
- Test: `tests/unit/host/test_options.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/host/test_options.py`:

```python
def test_single_client_console_defaults_false():
    from otto.host.options import TelnetOptions
    assert TelnetOptions().single_client_console is False


def test_single_client_console_can_be_set():
    from otto.host.options import TelnetOptions
    assert TelnetOptions(single_client_console=True).single_client_console is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest -o addopts="" tests/unit/host/test_options.py::test_single_client_console_defaults_false -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument` / `AttributeError`.

- [ ] **Step 3: Add the field**

In `src/otto/host/options.py`, inside `class TelnetOptions`, immediately after the `login: bool = True` field and its docstring, add:

```python
    single_client_console: bool = False
    """When True, this connection targets a single-client console — an RTOS
    telnet shell that serves one client at a time (e.g. Zephyr ``shell_telnet``
    reached over a ``-serial telnet:`` bridge). The transport is registered in a
    process-local set so the embedded test teardown can force-release the slot if
    a timed-out test left it half-open (see
    :func:`otto.host.telnet.abort_console_transports`). Unix telnet (multi-session
    telnetd) leaves this False, so it is never registered or aborted."""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -o addopts="" tests/unit/host/test_options.py -v -k single_client_console`
Expected: 2 passed.

- [ ] **Step 5: Stage + hand off commit**

```bash
git add src/otto/host/options.py tests/unit/host/test_options.py
```
Commit message for Chris:
```
feat(host): add TelnetOptions.single_client_console marker

Marks a telnet connection as a single-client RTOS console (Zephyr shell_telnet)
so the embedded test harness can track + force-release its slot. Defaults False;
unix telnet leaves it unset.
```

---

## Task 2: Console-transport registry + `abort_console_transports()`

**Files:**
- Modify: `src/otto/host/telnet.py` (module-level, near the existing `_naws_subscribers` global at line 40)
- Test: `tests/unit/host/test_telnet_client.py` (new test class)

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/host/test_telnet_client.py` (note the existing imports already include `from unittest.mock import MagicMock` and `from otto.host.telnet import TelnetClient`):

```python
from otto.host import telnet as telnet_mod


class TestConsoleTransportRegistry:
    @pytest.fixture(autouse=True)
    def _clear_registry(self):
        telnet_mod._live_console_transports.clear()
        yield
        telnet_mod._live_console_transports.clear()

    def test_abort_aborts_registered_transport_and_clears(self):
        t = MagicMock()
        telnet_mod._register_console_transport(t)
        n = telnet_mod.abort_console_transports()
        t.abort.assert_called_once_with()
        assert n == 1
        assert len(telnet_mod._live_console_transports) == 0

    def test_register_none_is_noop(self):
        telnet_mod._register_console_transport(None)
        assert len(telnet_mod._live_console_transports) == 0

    def test_unregister_removes_transport(self):
        t = MagicMock()
        telnet_mod._register_console_transport(t)
        telnet_mod._unregister_console_transport(t)
        assert telnet_mod.abort_console_transports() == 0
        t.abort.assert_not_called()

    def test_abort_is_defensive_against_one_bad_transport(self):
        good, bad = MagicMock(), MagicMock()
        bad.abort.side_effect = RuntimeError("already closed")
        telnet_mod._register_console_transport(bad)
        telnet_mod._register_console_transport(good)
        telnet_mod.abort_console_transports()  # must not raise
        good.abort.assert_called_once_with()
        assert len(telnet_mod._live_console_transports) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest -o addopts="" tests/unit/host/test_telnet_client.py::TestConsoleTransportRegistry -v`
Expected: FAIL — `AttributeError: module 'otto.host.telnet' has no attribute '_live_console_transports'`.

- [ ] **Step 3: Add the registry**

In `src/otto/host/telnet.py`, after the `_naws_subscribers` global (line 40), add:

```python
# Live single-client console transports — populated when TelnetOptions.
# single_client_console is True. Strong refs (we keep them reachable so the
# embedded test teardown can force-release a console slot a timed-out test left
# half-open) and per-process (each xdist worker owns its own). See
# abort_console_transports().
_live_console_transports: set = set()


def _register_console_transport(transport: Any) -> None:
    """Track a live single-client console transport (no-op if None)."""
    if transport is not None:
        _live_console_transports.add(transport)


def _unregister_console_transport(transport: Any) -> None:
    """Stop tracking a transport (no-op if absent)."""
    _live_console_transports.discard(transport)


def abort_console_transports() -> int:
    """Synchronously abort every tracked single-client console transport.

    Releases each FD (and the server-side single-client slot) via the
    transport's own synchronous ``abort()`` — no event loop required, so this
    works even after a pytest-timeout signal aborts a test before its async
    ``close()`` could run. Best-effort and idempotent; returns the count
    aborted. Per-process.
    """
    count = 0
    for transport in list(_live_console_transports):
        try:
            transport.abort()
            count += 1
        except Exception:  # noqa: BLE001 — best-effort cleanup; one bad transport must not block the rest
            pass
    _live_console_transports.clear()
    return count
```

(`Any` is already imported in `telnet.py` — `from typing import (Any, Optional)` at line 20 — so no import change is needed.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -o addopts="" tests/unit/host/test_telnet_client.py::TestConsoleTransportRegistry -v`
Expected: 4 passed.

- [ ] **Step 5: Stage + hand off commit**

```bash
git add src/otto/host/telnet.py tests/unit/host/test_telnet_client.py
```
Commit message for Chris:
```
feat(host): per-process registry of live single-client console transports

Adds _live_console_transports + abort_console_transports(): a synchronous,
idempotent sweep that aborts tracked console transports (releasing the FD and
the server-side single-client slot) without needing a live event loop. Backs
the embedded test harness's timeout-safe teardown.
```

---

## Task 3: Wire registration into `connect()`/`close()` + set the flag for embedded consoles

**Files:**
- Modify: `src/otto/host/telnet.py` (`connect()` ~line 125; `close()` ~line 234)
- Modify: `src/otto/host/embeddedHost.py:238`
- Test: `tests/unit/host/test_telnet_client.py` (extend `TestConsoleTransportRegistry`)

- [ ] **Step 1: Write the failing tests**

Add to `TestConsoleTransportRegistry` in `tests/unit/host/test_telnet_client.py`:

```python
    @pytest.mark.asyncio
    async def test_connect_registers_when_single_client_console(self, monkeypatch):
        from otto.host.options import TelnetOptions
        fake_writer = MagicMock()
        fake_writer.transport = MagicMock()

        async def fake_open(host, **kwargs):
            return (MagicMock(), fake_writer)

        monkeypatch.setattr(telnet_mod, "open_telnet_connection", fake_open)
        c = TelnetClient(
            host="h", user="u", password="p",
            options=TelnetOptions(login=False, single_client_console=True),
        )
        await c.connect(interactive=True)  # interactive=True skips ECHO negotiation
        assert fake_writer.transport in telnet_mod._live_console_transports

    @pytest.mark.asyncio
    async def test_connect_does_not_register_plain_telnet(self, monkeypatch):
        from otto.host.options import TelnetOptions
        fake_writer = MagicMock()
        fake_writer.transport = MagicMock()

        async def fake_open(host, **kwargs):
            return (MagicMock(), fake_writer)

        monkeypatch.setattr(telnet_mod, "open_telnet_connection", fake_open)
        c = TelnetClient(
            host="h", user="u", password="p",
            options=TelnetOptions(login=False, single_client_console=False),
        )
        await c.connect(interactive=True)
        assert fake_writer.transport not in telnet_mod._live_console_transports

    @pytest.mark.asyncio
    async def test_close_deregisters(self, monkeypatch):
        from otto.host.options import TelnetOptions
        fake_writer = MagicMock()
        fake_writer.transport = MagicMock()

        async def fake_open(host, **kwargs):
            return (MagicMock(), fake_writer)

        monkeypatch.setattr(telnet_mod, "open_telnet_connection", fake_open)
        c = TelnetClient(
            host="h", user="u", password="p",
            options=TelnetOptions(login=False, single_client_console=True),
        )
        await c.connect(interactive=True)
        await c.close()
        assert fake_writer.transport not in telnet_mod._live_console_transports
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest -o addopts="" "tests/unit/host/test_telnet_client.py::TestConsoleTransportRegistry::test_connect_registers_when_single_client_console" -v`
Expected: FAIL — the transport is not registered (assert fails).

- [ ] **Step 3a: Register in `connect()`**

In `src/otto/host/telnet.py`, in `connect()`, immediately after the writer is established:

```python
        self.reader, self.writer = await open_telnet_connection(
            self.host,
            **open_kwargs,  # type: ignore[arg-type]
        )
        if self.options.single_client_console:
            _register_console_transport(getattr(self.writer, 'transport', None))
```

- [ ] **Step 3b: Deregister in `close()`**

In `close()`, inside the `if self.writer:` block, add the discard right after `transport` is read:

```python
            transport = getattr(self.writer, 'transport', None)
            _unregister_console_transport(transport)
            self.writer.close()
            if transport is not None:
                transport.abort()
```

- [ ] **Step 3c: Set the flag for embedded consoles**

In `src/otto/host/embeddedHost.py:238`, change:

```python
            telnet_options=replace(self.telnet_options, login=False),
```
to:
```python
            telnet_options=replace(self.telnet_options, login=False, single_client_console=True),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -o addopts="" tests/unit/host/test_telnet_client.py::TestConsoleTransportRegistry -v`
Expected: 7 passed (4 from Task 2 + 3 new).

Also confirm no telnet regressions: `uv run pytest -o addopts="" tests/unit/host/test_telnet_client.py -v`
Expected: all pass.

- [ ] **Step 5: Stage + hand off commit**

```bash
git add src/otto/host/telnet.py src/otto/host/embeddedHost.py tests/unit/host/test_telnet_client.py
```
Commit message for Chris:
```
feat(host): register/deregister embedded console transports on connect/close

TelnetClient.connect() registers its transport when single_client_console is
set; close() deregisters it. EmbeddedHost marks its console telnet options
single_client_console=True (alongside login=False). close() stays async and
otherwise unchanged. Unix telnet never sets the flag, so it is never tracked.
```

---

## Task 4: `console_access()` turnstile fair lock primitive

**Files:**
- Create: `tests/integration/host/_console_lock.py`
- Test: `tests/unit/host/test_console_lock.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/host/test_console_lock.py`:

```python
"""Unit tests for the writer-fair console lock (lab-free, multiprocessing)."""
from __future__ import annotations

import multiprocessing as mp
import time
from pathlib import Path

from tests.integration.host._console_lock import console_access


def _reader_hold_then_barrier(lock_dir: str, barrier) -> None:
    # Hold a SHARED lock and wait for the peer reader to also be inside it.
    with console_access(Path(lock_dir), exclusive=False):
        barrier.wait(timeout=5)


def _reader_churn(lock_dir: str, stop) -> None:
    # Continuously take/release SHARED locks to pressure an EXCLUSIVE waiter.
    while not stop.is_set():
        with console_access(Path(lock_dir), exclusive=False):
            time.sleep(0.02)
        time.sleep(0.005)


def test_two_readers_hold_shared_concurrently(tmp_path):
    # If the lock wrongly serialized readers, the Barrier(2) would time out and
    # the children would exit non-zero.
    barrier = mp.Barrier(2)
    ps = [
        mp.Process(target=_reader_hold_then_barrier, args=(str(tmp_path), barrier))
        for _ in range(2)
    ]
    for p in ps:
        p.start()
    for p in ps:
        p.join(timeout=15)
    assert all(p.exitcode == 0 for p in ps), "readers did not hold SHARED concurrently"


def test_writer_not_starved_by_reader_churn(tmp_path):
    stop = mp.Event()
    readers = [
        mp.Process(target=_reader_churn, args=(str(tmp_path), stop))
        for _ in range(4)
    ]
    for r in readers:
        r.start()
    try:
        time.sleep(0.3)  # let the churn ramp up
        start = time.monotonic()
        with console_access(tmp_path, exclusive=True):
            waited = time.monotonic() - start
        assert waited < 5.0, f"exclusive waiter starved: waited {waited:.2f}s"
    finally:
        stop.set()
        for r in readers:
            r.join(timeout=5)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest -o addopts="" tests/unit/host/test_console_lock.py -v`
Expected: FAIL at collection — `ModuleNotFoundError: No module named 'tests.integration.host._console_lock'`.

- [ ] **Step 3: Implement the primitive**

Create `tests/integration/host/_console_lock.py`:

```python
"""Writer-fair cross-worker lock serializing access to single-client consoles.

Per-device embedded tests take a SHARED lock (they touch only their own console;
different devices parallelize across xdist workers); the fan-out / contention
tests take an EXCLUSIVE lock (they open every console, or two clients to one).
Plain ``flock`` is reader-preferring on Linux, so a steady stream of SHARED
holders starves the EXCLUSIVE waiter (confirmed live — see
docs/superpowers/specs/2026-06-06-embedded-console-lock-fairness-design.md).

This is a turnstile-gated reader/writer lock: every caller passes through a
*gate* mutex before taking the *resource* lock. A waiting writer holds the gate,
so new readers block at the gate while in-flight readers drain — the writer
can't be starved. Readers drop the gate the instant they hold the SHARED
resource lock, so they still run concurrently.
"""
from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

GATE_NAME = "zephyr_console.gate"
RESOURCE_NAME = "zephyr_console.resource"


@contextmanager
def console_access(lock_dir: Path, *, exclusive: bool) -> Iterator[None]:
    """Acquire the fair console lock — SHARED (``exclusive=False``) or EXCLUSIVE.

    ``lock_dir`` must be common to every xdist worker (use
    ``tmp_path_factory.getbasetemp().parent``). Closing the fds in ``finally``
    releases the locks even if an explicit unlock is skipped — e.g. a
    pytest-timeout signal interrupts the holder.
    """
    gate_fd = os.open(str(lock_dir / GATE_NAME), os.O_RDWR | os.O_CREAT, 0o644)
    resource_fd = os.open(str(lock_dir / RESOURCE_NAME), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(gate_fd, fcntl.LOCK_EX)  # enter the turnstile
        if exclusive:
            # Hold the gate while waiting for the resource: new readers block at
            # the gate, in-flight readers drain, then the writer acquires. The
            # gate is released by the outer finally (after the resource).
            fcntl.flock(resource_fd, fcntl.LOCK_EX)
        else:
            fcntl.flock(resource_fd, fcntl.LOCK_SH)
            fcntl.flock(gate_fd, fcntl.LOCK_UN)  # let the next caller enter
        yield
    finally:
        # Closing each fd releases any lock held on it (the flock is tied to the
        # open file description), so this is correct even if a flock above was
        # interrupted. Resource first, then gate.
        os.close(resource_fd)
        os.close(gate_fd)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -o addopts="" tests/unit/host/test_console_lock.py -v`
Expected: 2 passed (each within a few seconds).

- [ ] **Step 5: Stage + hand off commit**

```bash
git add tests/integration/host/_console_lock.py tests/unit/host/test_console_lock.py
```
Commit message for Chris:
```
feat(test): writer-fair turnstile lock for single-client consoles

console_access() gates every reader/writer through a turnstile mutex before the
shared/exclusive resource lock, so a waiting EXCLUSIVE (fan-out) acquirer holds
the gate and drains readers instead of starving behind reader-preferring flock.
Readers still run concurrently. Unit-tested for fairness + reader parallelism.
```

---

## Task 5: Rewire `_console_access_lock` to the primitive + abort net

**Files:**
- Modify: `tests/integration/host/conftest.py` (imports at top; the comment block at lines ~192–218; the fixture at lines ~221–250)

- [ ] **Step 1: Add imports**

In `tests/integration/host/conftest.py`, add to the import block (with the other `from ...` imports):

```python
from otto.host.telnet import abort_console_transports
from tests.integration.host._console_lock import console_access
```

- [ ] **Step 2: Replace the fixture body**

Replace the entire `_console_access_lock` fixture (currently lines ~221–250) with:

```python
@pytest.fixture(autouse=True)
def _console_access_lock(request: pytest.FixtureRequest, tmp_path_factory):
    """Serialize the fan-out console tests against the per-device tests.

    Autouse + function-scoped, and (having no dependency on ``host1``) set up
    before it, so the lock is held across the whole window ``host1`` keeps a
    console open — its setup through its ``close()`` in teardown. Non-embedded
    tests are a no-op.

    Uses the writer-fair :func:`console_access` lock so the EXCLUSIVE fan-out
    waiter can't be starved by SHARED per-device churn. On teardown — which runs
    even after a pytest-timeout signal aborts the test — it force-aborts any
    single-client console transport a timed-out test left half-open, *before*
    releasing the lock, so the next test finds both a free lock and a clean
    console. On a clean test the host already closed + deregistered, so the
    sweep is a no-op.
    """
    if "embedded" not in request.node.keywords:
        yield
        return
    lock_dir = tmp_path_factory.getbasetemp().parent
    # A fan-out test references no single backend (it opens all of them); a
    # per-device test names exactly one — the same signal the wedge gate uses.
    exclusive = not _referenced_backends(request.node)
    with console_access(lock_dir, exclusive=exclusive):
        try:
            yield
        finally:
            abort_console_transports()
```

- [ ] **Step 3: Update the comment block + drop the obsolete rationale**

In the comment block above the fixture (lines ~192–218), delete the paragraph that begins "flock is reader-preferring on Linux, so a continuously-busy reader set could in theory starve the exclusive waiter; in practice ..." (it described the *unfixed* behavior). Replace it with:

```python
# The lock is writer-fair (see tests/integration/host/_console_lock.py): the
# EXCLUSIVE fan-out waiter holds a turnstile gate while waiting, so SHARED
# per-device churn can't starve it. The teardown also force-aborts any console
# transport a pytest-timeout'd test left half-open (abort_console_transports),
# so one timed-out fan-out test can't wedge the bed for the rest of the run.
```

- [ ] **Step 4: Remove now-unused imports**

`fcntl` and `os` were used only by the old fixture body. Remove `import fcntl` and `import os` from the top of `tests/integration/host/conftest.py` (lines 15–16).

- [ ] **Step 5: Verify collection + lint-clean import**

Run: `uv run pytest -o addopts="" tests/integration/host/conftest.py --collect-only -q 2>&1 | tail -5`
Expected: no `ImportError` / `NameError`; collection succeeds (it will report the conftest is not a test file, which is fine — the check is that the import graph loads).

Run: `uv run ruff check tests/integration/host/conftest.py`
Expected: no unused-import (`F401`) errors for `fcntl`/`os`.

- [ ] **Step 6: Stage + hand off commit**

```bash
git add tests/integration/host/conftest.py
```
Commit message for Chris:
```
fix(test): writer-fair console lock + timeout-safe teardown in _console_access_lock

Rewire the embedded console lock onto the writer-fair console_access() turnstile
so the fan-out EXCLUSIVE waiter can't be starved by per-device SHARED churn
(Issue 1 of the nox-all triage, root cause confirmed live). Teardown now
force-aborts any half-open single-client console (abort_console_transports)
before releasing the lock, killing the wedge cascade at its source. Drop the
obsolete "starvation is bounded" comment and the now-unused fcntl/os imports.
```

---

## Task 6: Lab acceptance — re-run the repro + happy path

> Requires the Vagrant lab up. These are verification steps, not code. Per the
> spec's success criteria. **Do not kill a run mid-flight** (it wedges
> single-client consoles); let it finish and `make qemu-restart` after.

- [ ] **Step 1: Re-run the Stage B repro (the regression that exposed Issue 1)**

Run:
```bash
uv run pytest \
  tests/integration/host/test_embedded_host_integration.py \
  tests/integration/host/test_host_contract.py \
  tests/integration/host/test_snmp_integration.py \
  -m "embedded and not stability" \
  -o addopts="-n auto --dist loadgroup" \
  --count 10 -ra \
  --junitxml=reports/junit/issue1-after-fix.xml
```
Expected: **no `Failed: Timeout (>30.0s)` setup errors** on
`test_concurrent_clients_to_one_console_contend_and_recover` (all 10 reps pass),
and **no "embedded bed unhealthy" cascade** — vs. the documented baseline of 386
errors / 9 failed. A small number of unrelated flakes is acceptable; the gate is
"no flock-starvation timeouts and no wedge cascade."

- [ ] **Step 2: Triage the result**

Run: `uv run python scripts/junit_failures.py reports/junit/issue1-after-fix.xml`
Expected: 0 problems (or only unrelated, non-wedge, non-flock-timeout items).

- [ ] **Step 3: Confirm the happy path is unregressed**

Run:
```bash
uv run pytest \
  tests/integration/host/test_embedded_host_integration.py \
  tests/integration/host/test_host_contract.py \
  tests/integration/host/test_snmp_integration.py \
  -m "embedded and not stability" \
  -o addopts="-n auto --dist loadgroup" -ra \
  --junitxml=reports/junit/b4b-after-fix.xml
```
Expected: same as the pre-fix happy path — 72 passed, 8 skipped, 0 failed.

- [ ] **Step 4: Recover the bed**

Run: `make qemu-restart`
Expected: all `sprout*` report fresh uptimes, `up`.

- [ ] **Step 5: Run the new unit tests under the full default config (no override)**

Run: `uv run pytest tests/unit/host/test_console_lock.py tests/unit/host/test_telnet_client.py tests/unit/host/test_options.py`
Expected: all pass under the real `addopts` (`-n auto`, coverage, etc.).

*(No commit — verification only. If Step 1 still shows starvation, stop and reopen the design; do not paper over it.)*

---

## Task 7: Record the outcome in the triage doc

**Files:**
- Modify: `todo/nox-all-failure-triage-2026-06-01.md`

- [ ] **Step 1: Update Issue 1's verdict**

In `todo/nox-all-failure-triage-2026-06-01.md`, in the "Bottom line" table row for `#1` and the Issue 1 section, mark it **confirmed + fixed**: root cause was `flock` writer starvation (lab-contention caveat refuted by a free-lab repro); fixed by the writer-fair `console_access()` lock + the `abort_console_transports()` teardown net (see the 2026-06-06 spec/plan). Note the Stage B repro reproduced 386 errors pre-fix and is clean post-fix.

- [ ] **Step 2: Stage + hand off commit**

```bash
git add todo/nox-all-failure-triage-2026-06-01.md
```
Commit message for Chris:
```
docs(triage): Issue 1 confirmed (flock starvation) and fixed

Root cause confirmed live (free-lab repro refuted the lab-contention caveat);
fixed by the writer-fair console lock + timeout-safe teardown net. See
docs/superpowers/{specs,plans}/2026-06-06-embedded-console-lock-fairness*.
```

---

## Verification matrix (acceptance)

| Spec goal | Task / gate |
|-----------|-------------|
| EXCLUSIVE waiter never starves under SHARED churn | Task 4 (unit fairness) + Task 6 Step 1 (lab) |
| Timed-out test can't leave a half-open console | Task 2/3 (registry+wiring) + Task 5 (teardown sweep) + Task 6 Step 1 (no cascade) |
| Reader parallelism preserved (<240s) | Task 4 (reader-parallelism unit test) + Task 6 Step 3 |
| Unix telnet untouched | Task 1/3 (flag-gated registration) + Task 3 unit tests (plain telnet not registered) |
| No auto-recovery (wedges stay visible) | wedge gate unchanged (out of scope) |
