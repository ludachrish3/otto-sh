# `make nox-all` failure triage — 2026-06-01

Source: `reports/junit/tests_all-3.{10,11,12,13,14}.xml`
Tool: `scripts/junit_failures.py [--full]`

## Suite totals (per Python version)

| Python | tests | failures | errors | skipped | wall time |
|--------|------:|---------:|-------:|--------:|----------:|
| 3.10   | 16470 | 1 | 8 | 140 | 1349 s |
| 3.11   | 16470 | 1 | 8 | 140 | 1349 s |
| 3.12   | 16470 | 1 | 8 | 140 | 1452 s |
| 3.13   | 16470 | 1 | 8 | 140 | 1355 s |
| 3.14   | 16470 | 0 | 8 | 140 | 1405 s |

`skipped: 140` = **130 conditional `pytest.skip` + 10 `pytest.xfail`** (see the xfail section — those are by-design, not problems).

---

## Unique failures

There are only **two distinct failure signatures** across all five Python versions.

### 1. `_console_access_lock` setup timeout — 8 errors/version, deterministic (the real failure)

> **RESOLVED 2026-06-06.** Root cause **confirmed** (the hypothesis below was right) and the lab-contention caveat **refuted**: a two-stage repro on a *free* lab reproduced the starvation at the same magnitude (Stage A `-n0` isolation: 10/10 pass; Stage B `-n auto --dist loadgroup --count 10`: the exclusive `contend_and_recover` test starved 9/10 reps with the identical `Timeout (>30s)` signature). So it is internal `flock` writer starvation, not the external worktree. **Fixed** by a writer-fair turnstile lock (`tests/integration/host/_console_lock.py`) — the exclusive fan-out waiter holds a gate so SHARED per-device churn can't starve it — plus a sync console-transport abort net in the embedded teardown. Post-fix the contend test passes **10/10** under the same stress. See `docs/superpowers/{specs,plans}/2026-06-06-embedded-console-lock-fairness*`.
>
> **Bonus finding (corrects a separate, pre-existing comment):** under the same extreme `--count` churn the x86 net beds also throw "shell never became ready" wedges that the wedge gate cascades. A controlled experiment showed this is **NOT** "e1000 net-buffer exhaustion" (the conftest comment's claim): `ping` works while wedged, `NET_BUF=128` is plentiful, and it self-recovers in ~2s (matching `NET_TCP_TIME_WAIT_DELAY=1500ms`). It is **abrupt RST close + rapid reconnect** against small connection-slot pools (`NET_MAX_CONTEXTS=6`/`NET_MAX_CONN=8`) on the in-guest telnet path (the ARM serial beds never wedge). It appears **only under the over-concentrated repro — the actual nox-all run had 0 of these wedges** — so it was left as a known, gate-surfaced bed limit (no fix), not part of the Issue-1 fix. Detail: `~/wiki/inbox/2026-06-06-zephyr-telnet-wedge-is-rst-churn-not-netbuf.md`.

- **Test:** `tests.integration.host.test_embedded_host_integration::test_concurrent_clients_to_one_console_contend_and_recover`
- **Variants:** `[3-10]` through `[10-10]` `@zephyr_fat` — 8 errors, **identical on every Python version** (3.10–3.14).
- **Signature:** `failed on setup with "Failed: Timeout (>30.0s) from pytest-timeout."`
- **Location:** [tests/integration/host/conftest.py:244](tests/integration/host/conftest.py#L244) — blocked on `fcntl.flock(fd, mode)` inside the autouse `_console_access_lock` fixture.

**What's happening (observed):**
The `[N-10]` suffix is the `--count` repeat index (`-10` = 10 total reps). Of the 10 repetitions of this one test, iterations **3–10 all time out** acquiring the lock; iterations 1–2 do not appear as errors (they got the lock). The fan-out console test references no single backend, so the fixture takes an **exclusive** lock (`LOCK_EX`); per-device embedded tests on other xdist workers take **shared** locks (`LOCK_SH`) and hold them for the whole console window (setup → `close()` in teardown). The exclusive waiter never wins within the 30 s `pytest-timeout` and the setup is killed.

**Likely root cause (hypothesis — not yet proven):**
`flock` offers no writer fairness. A steady overlap of shared-lock holders (the per-device embedded tests, fanned across xdist workers) can **starve the exclusive waiter indefinitely**. Early reps acquire the lock during a gap; as the count run progresses and the shared-lock tests pile up, the later reps starve out at 30 s. This is consistent with the failure being perfectly deterministic (8/8, all versions) rather than a random flake.

> ⚠️ **Lab-contention caveat:** these are `@zephyr_fat` embedded tests that drive QEMU/Zephyr consoles. You mentioned another agent's worktree was using lab resources. If that overlapped this `nox-all` run, it would have **prolonged the per-device (shared-lock) tests**, widening exactly the window in which the exclusive waiter starves — so it may be a contributor here, not purely an flock-fairness issue. Worth confirming whether the lab was contended during the 12:54–14:50 run window before treating the flock design as the sole cause.

**Suggested next step (do not run against the lab yet):** re-run *just* this test in isolation (`-n0`, no other embedded tests, lab free) to separate flock-fairness starvation from lab contention. Per your root-cause-first preference, I have **not** patched anything.

### 2. "multiple unraisable exception warnings" — 1 failure/version, flaky (the parked leak)

- **Signature:** `exceptiongroup.ExceptionGroup: multiple unraisable exception warnings (3 sub-exceptions)`
- **Where it landed (a *different* test each run — confirms it is not that test's fault):**
  - 3.10: `test_snmp_integration::test_uptime_advances_between_polls[zephyr-3.7-nofs-3-10]`
  - 3.11: `test_host_stability_contract::test_large_file_round_trips_byte_identical[zephyr-4.4-lfs-2-10]`
  - 3.12: `test_snmp_integration::test_collects_sane_metrics_over_snmp[zephyr-4.4-lfs-3-10]`
  - 3.13: `test_host_stability_contract::test_large_file_round_trips_byte_identical[zephyr-4.4-lfs-2-10]`
  - 3.14: **did not occur this run**
- **The 3 sub-exceptions** (from `--full`):
  1. `ResourceWarning: unclosed <socket.socket fd=15, family=AF_UNIX, ...>`
  2. `ResourceWarning: unclosed <socket.socket fd=14, family=AF_UNIX, ...>`
  3. `ResourceWarning: unclosed event loop <_UnixSelectorEventLoop running=False closed=False>`

  All three surface via `_pytest/unraisableexception.py` `collect_unraisable()`, which raises whenever the GC happens to finalize the leaked objects *during* some test — so the failing test name is just whichever test triggered collection, not the leak's origin.

**Root cause:** this is the **already-parked async ResourceWarning leak** (`project_async_resource_warning_leak.md` in memory): an unclosed `_UnixSelectorEventLoop` plus two unclosed `AF_UNIX` sockets. Connection-specific; needs xdist/embedded + `tracemalloc` to pinpoint the allocation site. Not newly actionable from these reports.

---

## Why some tests are `xfail` (10 per version)

There is exactly **one** intentional xfail test, repeated 10× by the `--count` stress run:

- **Test:** [tests/unit/suite/test_timeout_enforcement.py:33](tests/unit/suite/test_timeout_enforcement.py#L33) — `test_marked_timeout_actually_aborts`
- **Marker:** `@pytest.mark.xfail(reason="pytest-timeout must abort this hung test", strict=True)` + `@pytest.mark.timeout(0.25)`; the body does `time.sleep(30)`.

**It is a tripwire, not a defect.** The test sleeps 30 s under a 0.25 s timeout:

- **Timeout fires (correct wiring):** the body never returns → the test "fails" with a Timeout → `xfail(strict=True)` records it as **XFAIL = a pass**. ✅
- **Timeout does *not* fire (regression):** the sleep completes, the body returns, the test "passes" → strict xfail turns that into a hard **XPASS failure**, flagging that suite-level timeout enforcement has silently broken.

Background (from the test's own docstring): otto once enforced `@pytest.mark.timeout` with an in-house autouse `pytest_asyncio` fixture that cancelled its *own* task instead of the test body — a `timeout(2)` test that slept 30 s passed after 30 s and nothing caught it. Enforcement is now `pytest-timeout` (configured in `pyproject.toml`), and this xfail guards the wiring against future pytest/pytest-asyncio/xdist bumps or a dependency drop.

So: **the 10 XFAILs are expected and green.** Nothing to fix.

### (For completeness) the 130 `pytest.skip`

Conditional, capability-based skips — not failures:

| Skip reason | count/version |
|-------------|--------------:|
| `backend has a filesystem — see round-trip test` | 70 |
| `backend has no filesystem — see no-FS error test` | 20 |
| `backend has no filesystem — no progress to report` | 20 |
| `backend has no filesystem — cycle test skipped` | 10 |
| `backend has no filesystem — large transfer skipped` | 10 |

These fire when a parametrized backend lacks (or has) a filesystem and the scenario doesn't apply to it.

---

## Bottom line

| # | Signature | Count | Verdict |
|---|-----------|-------|---------|
| 1 | `_console_access_lock` 30 s timeout, `test_concurrent_clients_…@zephyr_fat` | 8 / version, all 5 versions | **RESOLVED 2026-06-06.** Confirmed internal `flock` writer starvation (lab-contention caveat refuted by a free-lab repro); **fixed** with a writer-fair turnstile lock + sync abort net. Contend test now 10/10 under the same stress. |
| 2 | `ExceptionGroup: multiple unraisable exception warnings` | 1 / version (3.10–3.13), 0 on 3.14 | **Known parked leak** (unclosed event loop + 2 AF_UNIX sockets). Flaky, lands on a random test. |
| — | `xfail` `test_marked_timeout_actually_aborts` | 10 / version | **By design** — strict-xfail timeout-wiring tripwire. Healthy. |
