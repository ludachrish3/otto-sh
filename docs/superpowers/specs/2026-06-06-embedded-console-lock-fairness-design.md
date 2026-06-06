# Embedded console serialization: writer-fair lock + timeout-safe teardown

**Date:** 2026-06-06
**Status:** Design approved; ready for implementation plan.
**Fixes:** Issue 1 (the `_console_access_lock` setup-timeout) from the
2026-06-01 nox-all failure triage (since removed — the issue is resolved),
root cause now confirmed live.
**Related:** the cross-worker serialization introduced in `2d4bd1a` and the
reactive wedge gate in `5b75244` (both 2026-05-31); the parked async
ResourceWarning leak (Issue 2) is **separate and out of scope**.

---

## 1. Problem & confirmed root cause

The embedded contract suite serializes single-client Zephyr consoles across
xdist workers with a `flock`-based reader/writer lock
([`tests/integration/host/conftest.py`](../../../tests/integration/host/conftest.py),
`_console_access_lock`): per-device tests take a SHARED lock, the fan-out /
contention tests take an EXCLUSIVE lock. `flock` is **reader-preferring on
Linux**, so a continuously-busy set of SHARED holders starves the EXCLUSIVE
waiter. The existing code comment acknowledged the risk but deemed it
"bounded by timeout in practice."

It is not. A two-stage repro on a **free** lab (2026-06-06) confirmed it:

- **Stage A — isolation** (`-n0 --count 10`, just the exclusive test): **10/10
  passed**. The test itself is sound; the lock acquires instantly with no
  competing holders.
- **Stage B — realistic concurrency** (`-n auto --dist loadgroup --count 10`,
  full embedded set): the exclusive `test_concurrent_clients_to_one_console_
  contend_and_recover` **starved on 9 of 10 reps** with the exact triage
  signature (`Failed: Timeout (>30.0s)` on the lock setup). Total: **386 errors,
  9 failed, 365 passed** in 383 s.

Because a free lab reproduced it at the same magnitude, the triage's
lab-contention hypothesis is **refuted** — the contention is internal
(the suite's own SHARED holders) and the root cause is `flock` writer
starvation.

**Secondary cascade.** The 11 flock timeouts came first (log lines 1671–2451);
**5 real console wedges** (`shell never became ready`) appeared only afterward
(lines 4466+). Mechanism: delayed fan-out tests, denied the lock, time out
*mid-operation* (their 120 s budget) and a SIGALRM abort leaves a **half-open
telnet client**. A Zephyr `shell_telnet` console serves one client at a time,
so the leaked client wedges it; the reactive wedge gate then fast-fails every
downstream test on that backend (**375** "embedded bed unhealthy" errors). One
starved test collapses the whole embedded run.

## 2. Goals & success criteria

1. The EXCLUSIVE (fan-out) waiter never starves under sustained SHARED churn —
   addressing the root cause, not the symptom.
2. A test aborted by `pytest-timeout` cannot leave a half-open single-client
   console for the next test (kills the cascade at its source).
3. Reader parallelism is preserved (different devices still run concurrently —
   full serialization measured >450 s, over the Makefile's 240 s cap).
4. **Acceptance:** the Stage B repro runs with the exclusive test 10/10 and no
   wedge cascade; the single-pass happy path and the existing embedded suite
   stay green; new lab-free unit tests prove fairness and abort-net scoping.

## 3. Non-goals / out of scope

- **Auto-recovery of wedges.** Deliberately *not* added: a wedge that still
  occurs must stay **visible** (it can signal a resource/design flaw). The
  reactive wedge gate is unchanged.
- **Issue 2** (the parked unclosed-event-loop + AF_UNIX ResourceWarning leak)
  — related teardown territory but a distinct problem; not addressed here.
- Eliminating *all* possible wedges. A fan-out test that legitimately exceeds
  its timeout can still wedge; the abort net releases the slot afterward, and
  the gate keeps it visible.

## 4. Design

Three small, single-purpose pieces.

### 4.1 Writer-fair lock (A1) — turnstile-gated `flock` RW lock

A new unit-testable context manager `console_access(lock_dir, *, exclusive)` in
`tests/integration/host/_console_lock.py` (test-layer concurrency control, not
production). Two lock files under the xdist-shared dir
(`tmp_path_factory.getbasetemp().parent`): `zephyr_console.gate` and
`zephyr_console.resource`. Protocol:

```
reader (per-device, SHARED):              writer (fan-out/contention, EXCLUSIVE):
  flock(gate, EX)                           flock(gate, EX)        # hold the turnstile
  flock(resource, SH)                       flock(resource, EX)   # drain readers while holding gate
  flock(gate, UN)   ← release immediately   ── yield ──
  ── yield ──                               flock(resource, UN)
  flock(resource, UN)                       flock(gate, UN)       ← release last
```

A waiting writer **holds the gate**, so new readers block at the gate →
in-flight readers drain → the writer acquires `resource` EX. Readers drop the
gate the instant they hold `SH`, so they still share the resource concurrently.
Writers are rare (a handful of fan-out tests), so readers do not starve in the
other direction. `finally` closes both fds, which releases any held lock even if
an explicit unlock is skipped (interrupted by a timeout).

### 4.2 Console-transport registry + abort net (B1)

A per-process registry of live **single-client console** transports in
`src/otto/host/telnet.py`, plus a synchronous sweep:

- **Marker:** add `TelnetOptions.single_client_console: bool = False`
  ([`options.py`](../../../src/otto/host/options.py)). The embedded path sets it
  at [`embeddedHost.py:238`](../../../src/otto/host/embeddedHost.py#L238)
  alongside `login=False`. It rides `TelnetOptions`, so every `TelnetClient`
  built for an embedded console inherits it — via `ConnectionManager.telnet()`
  *and* `SessionManager.open_session`. Unix telnet (`login=True`) never sets it.
- **Registry:** a module-global `set` of transports.
  `TelnetClient.connect()` registers `writer.transport` after the writer is
  established, **iff** `self.options.single_client_console`.
  `TelnetClient.close()` discards it (one added line; `close()` stays fully
  async and otherwise unchanged).
- **Sweep:** `abort_console_transports()` calls `transport.abort()` — already a
  synchronous asyncio method, the same FD-release `close()` uses — on each
  registered transport, each guarded by `try/except`, then clears the set. No
  live event loop required, so it works after a SIGALRM abort. `abort()` is
  idempotent, so racing a clean close is harmless.

### 4.3 Wiring

`_console_access_lock` (the autouse, embedded-only fixture) is rewired:
acquire via `console_access(...)`; in teardown, call
`abort_console_transports()` **before** releasing the lock so the next test
finds both a free lock and a clean console. The fixture's sync teardown runs
even after a signal-method timeout (verified by probe), which is what makes the
net effective. The obsolete "starvation is bounded/acceptable" comment is
removed.

### 4.4 Scope by host type

- **A1 is embedded-only**, by construction (`_console_access_lock` early-returns
  for non-`embedded` tests) and by correctness (real telnetd accepts concurrent
  sessions; there is no single console slot to serialize).
- **B1 touches only single-client consoles.** The registry is keyed on the
  explicit flag, so **unix telnet sessions are never registered and never
  aborted** — even when an embedded test and a unix telnet test share a worker
  process. A leaked *unix* telnet transport self-heals (telnetd accepts a fresh
  connection); only the single-client Zephyr console wedges, so only it needs
  the net.

## 5. Error handling & edge cases

- **SIGALRM during `flock`:** the handler raises (PEP 475 won't retry), so a
  starved waiter propagates the Timeout; `finally` closes both fds → lock
  released, no deadlock. Rare once the lock is fair.
- **Writer interrupted holding the gate:** `finally`/close releases it; others
  proceed.
- **`abort()` on an already-closed transport:** idempotent no-op; still guarded
  per-transport so one bad transport can't break the sweep.
- **Registry hygiene:** module-global → per worker process; mutated only from
  the worker's single asyncio thread (no race). Cleared every embedded
  teardown; clean `close()` discards its own transport — never accumulates.
- **Register only when a transport exists** (`getattr(writer, 'transport',
  None)`), right after the writer is established.
- **Teardown ordering:** `_console_access_lock` tears down after `host1`. Clean
  test → host already closed/discarded → sweep is a no-op. Timed-out test →
  transport still registered → sweep aborts it.
- **`-n0` / serial runs:** one process; the exclusive test acquires instantly
  (matches Stage A).

## 6. Verification

**Unit (lab-free, deterministic):**
1. **Writer-fairness** — subprocesses churn SHARED locks against a temp dir;
   an EXCLUSIVE acquirer must acquire within a bounded time despite the churn
   (the starvation regression guard).
2. **Reader parallelism** — two readers hold SHARED concurrently (no
   over-serialization).
3. **Abort-net scoping** — a `single_client_console=True` client registers and
   is aborted by `abort_console_transports()`; a `login=True` client is **not**
   registered (proves unix non-interference without the lab).

**Lab (acceptance):**
4. **Re-run the Stage B repro** (`-n auto --dist loadgroup --count 10`): the
   exclusive test 10/10, no wedge cascade — vs. the 386-error / 9-failed
   baseline. `make qemu-restart` afterward.
5. **Happy path unchanged** — the single-pass `-m "embedded and not stability"`
   run stays green.

## 7. Files touched

| File | Change |
|------|--------|
| `tests/integration/host/_console_lock.py` | **new** — `console_access()` turnstile CM |
| `tests/unit/host/test_console_lock.py` | **new** — fairness + reader-parallelism unit tests |
| `tests/unit/host/test_telnet_client.py` | **extend** — abort-net registry/scoping test |
| `tests/unit/host/test_options.py` | **extend** — `single_client_console` default/propagation |
| `src/otto/host/options.py` | add `TelnetOptions.single_client_console` |
| `src/otto/host/embeddedHost.py` | set the flag at line 238 (with `login=False`) |
| `src/otto/host/telnet.py` | registry; register in `connect()`, discard in `close()`; `abort_console_transports()` |
| `tests/integration/host/conftest.py` | rewire `_console_access_lock`; call sweep in teardown; drop obsolete comment |
| `todo/nox-all-failure-triage-2026-06-01.md` | record Issue 1 confirmed + fixed (follow-up) |
