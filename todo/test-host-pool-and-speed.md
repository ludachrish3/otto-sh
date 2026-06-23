# Test host-pool & speed strategies (follow-up brainstorm)

> Deferred from the 2026-06-22 test-restructure brainstorm (which scoped itself
> to restructure + dedup). This captures the **speed half** of that request so a
> follow-up brainstorm → spec → plan can start warm. Gate this AFTER the
> restructure lands (the restructure moves `_console_lock.py` into
> `tests/_fixtures/`, which the pool lease would build on).

## The problem

Integration/e2e tests hardcode a "favorite VM" instead of pulling an available
host from a pool, which both concentrates load and forces a test to wait on a
specific host even when an equivalent one is free.

- `host1`, `transfer_host`, and the hop fixtures all funnel onto **`carrot`**;
  `host2`→`tomato`, `host3`→`pepper`. **`basil`** sits nearly idle (only the
  embedded SSH hop). (See `tests/conftest.py`.)
- The suite keeps growing; parallelization is uneven.

## Baseline measurements (captured 2026-06-23, post-restructure `main` @ c9e76d8)

Authoritative starting point for the speedup. Per-tier figures are parsed from
the post-restructure validation JUnit (`reports/junit/`); `coverage-unit` was
re-measured fresh on 2026-06-23.

### Gate wall-clock + coverage (current `main`)

| gate | scope | wall | tests | coverage |
|------|-------|------|-------|----------|
| `make coverage-unit` | unit tier, **no VMs** (`-m "not integration and not embedded"`) | **~28 s** † | 1910 | 87.04 % |
| `make coverage` | all tiers **minus** `stability` (90 % gate) | **~119 s** | 2193 | 91.85–91.87 % |
| `make nox` | full incl. `stability`, ×5 Pythons (85 % gate each) | **~804 s** (~160 s/Python) | 2234/Python | 91.87 % |

> † `coverage-unit` wall was re-measured 2026-06-23 **during a concurrent release
> build** and is likely inflated by CPU contention — treat as an upper bound and
> re-measure on an idle box. Pass-count and coverage % are unaffected. Every
> other figure here comes from the clean post-restructure validation JUnit (a
> solo run with nothing else competing for the box or the bed).

### Where the time goes — aggregate test-time per tier

From the `make coverage` run (excl. `stability`). *Aggregate* = sum of per-test
durations (total work); wall-clock is lower because xdist runs them in parallel.

| tier | tests | aggregate test-time | % of agg |
|------|-------|---------------------|----------|
| integration | 220 | 158.8 s | 57.6 % |
| e2e | 77 | 88.0 s | 32.0 % |
| unit | 1898 | 28.7 s | 10.4 % |
| **total** | **2195** | **275.5 s** | 100 % |

With `stability` included (`nox tests_all-3.12`): integration **427.6 s (74.3 %)**,
e2e 119.5 s (20.7 %), unit 28.8 s (5.0 %); total 575.9 s.

### Key facts for the speedup

- **~90 % of test-time lives in ~13 % of the tests.** The 297 bed tests
  (integration + e2e) are 89.6 % of aggregate time; the 1898 unit tests are 86 %
  of the *count* but only 10 % of the time. → The unit tier is already
  negligible; every real win is in the bed tiers.
- **Coverage is the guardrail, not the bed's purpose.** Integration/e2e exist
  to confirm specific *scenario behaviors* — telnet bad-creds fails fast, a
  second console session contends and recovers, SIGWINCH propagates to the
  remote, a put/get round-trips — **not** to chase a coverage number. For
  reference, unit-only happens to reach 87.04 % and the full bed 91.85 %, but
  that ~4.8-pt delta is *incidental*. The hard constraint is **preserve every
  scenario**; coverage equivalence is merely how we *detect* that the speedup
  didn't silently drop one.
- **xdist already gives ~2.3× parallelism** (275.5 s aggregate → ~119 s wall).
  The remaining ceiling is the **longest serial chain** — the single-client,
  console-locked embedded tests, which trail as a serial tail at end of run.
- **`make nox` re-runs the entire bed suite 5×** (once per Python). Bed
  behavior is Python-version-independent, so most of the ~804 s is the same bed
  work repeated — a large lever *separate* from the local-gate interleave.

#### Slowest individual tests (top of the `make coverage` run)

| time | tier | test |
|------|------|------|
| 20.5 s | e2e | `test_embedded_coverage_e2e::…cli_e2e@sprout_cov` |
| 8.1 s | integration | `test_unix_host_integration::test_telnet_bad_credentials_fails_fast` |
| 7.8 s | integration | `test_embedded_host_integration::…survives_second_open[4.4-lfs]` |
| 6.5 s | integration | `test_host_contract::…returns_status_success[zephyr_lfs]` |
| 6.5 s | e2e | `test_interact_e2e::…resize_triggers_remote_side_update[ssh/telnet]` |
| 5–6 s ×10 | integration | `test_embedded_host_integration` single-console (serial, console-locked) |
| 4–5 s ×3 | e2e | `test_docker_e2e_cli` put/get + idempotency |

## Empirical findings (2026-06-23 measurement campaign, clean idle box)

A focused campaign on the pinned Python, gate scope (`-m "not stability"`), to
**quantify the win before building** (per the open questions below). Single-shot
wall-clock; see the variance caveat.

### Does the embedded chain trail, or overlap? (the interleave hypothesis)

| run | scope | no-cov wall | with-cov wall |
|-----|-------|-------------|---------------|
| **A** embedded only | `-m "embedded and not stability"` | 42 s | 46 s |
| **B** everything *except* embedded | unit + unix + e2e | 83 s | 62 s |
| **T** full | `-m "not stability"` | 80 s | 85 s |

**T ≈ B, not A + B.** The embedded chain (~42 s) runs *concurrently inside* the
non-embedded window — it is **already largely hidden**, not a serial tail. The
per-device `xdist_group` + the *shared* console lock already parallelise the 5
consoles (the 4 × ~48 s stability large-transfers ran in ~48 s wall = 4-way
parallel). **Interleaving embedded with Unix is therefore a non-lever** — the
hypothesis was tested and does not hold for the integration tests.

### What actually trails (consistent across every run)

The last tests to finish, every time:

1. **Unix `TestFileTransfer`** (scp/sftp/ftp/nc put+get) all funnelling onto
   **`carrot`** (`transfer_host`) — the single biggest tail component.
2. **`test_embedded_coverage_cli_e2e@sprout_cov`** — one ~18.8 s e2e test that
   finishes **dead last** (100 %) on every run; `--dist loadgroup` schedules it
   late.
3. **The e2e single-worker chains** — `docker_e2e`, `coverage_e2e`,
   `interact_e2e` are each ONE `xdist_group`, so each runs serially on one
   worker (the two `interact_e2e` SIGWINCH tests are ~6.5 s each, back to back).

The wall-clock floor is set by the **non-embedded** work — carrot-funnelled
transfers + the trailing `sprout_cov` + the e2e chains — **not** by embedded.

### Other measured facts

- **Coverage overhead is small** in paired clean runs: full no-cov 80 s vs
  with-cov 85 s (~5 s). The captured `make coverage` baseline was 119 s; that
  gap is unexplained (full HTML/combine + bed variance + measurement context),
  so treat the real-gate floor as **~85–120 s and noisy**, not a hard 119 s.
  Coverage is *not* a speed lever.
- **Bed timing is noisy:** B swung 83 s → 62 s between two runs (~±20 s of
  bed-I/O / scheduling jitter). **Validate any speedup with repetition**
  (`pytest-repeat` / several runs), never a single wall-clock.
- **Bed flakiness is real:** one with-cov run saw a lone
  `test_snmp_integration…[embedded-3.7-fat]` failure (SNMP-over-console timing)
  that the clean run passed — reinforcing "validate with repetition."

## Ideas to explore (re-prioritised by the findings above)

- **Spread the Unix transfer/command tests off `carrot` (host-pool lease).**
  *Now the #1 data-justified lever* — the transfer tests are the largest tail
  component, all serialised onto `transfer_host` = `carrot`. A test that just
  needs "a Unix host" leases whichever of `{carrot, tomato, pepper, basil}` is
  free, spreading load ~4×. Cross-worker lease can mirror the writer-fair file
  lock in `tests/_fixtures/_console_lock.py`. Enablers already present: per-host
  `resources`/`labs` tags + `remote_name(worker_id, …)` for per-worker path
  namespacing; lease around the reactive bed-wedge gate
  (`integration/host/conftest.py`) so a sick host is skipped, not waited on.
  Caveat: verify each pool host has the needed transfer backends
  (ftp/nc/scp/sftp) configured before leasing it for a transfer test.
- **Front-load the long-pole singletons.** `sprout_cov` (~18.8 s) finishes last
  only because loadgroup schedules it late; hinting the longest known
  groups/tests to start **first** overlaps them with everything else. Cheap and
  targeted.
- **Split the e2e single-worker chains** (`docker_e2e`/`coverage_e2e`/
  `interact_e2e`) into finer groups *iff* their per-test resources can be
  isolated (distinct compose projects, workdirs, consoles). Higher effort /
  flake risk — gate behind a clear isolation story.
- **`make nox`: run the unit ×5 sessions concurrently** while the bed sessions
  stay sequential — preserves the full 5× matrix (your call) yet removes the
  unit repetition; each bed session then inherits the per-run wins above.
- ~~**Interleave the embedded tail with the Unix tests**~~ — *tested 2026-06-23
  and disproven* (see Empirical findings): embedded already overlaps.

## Hard constraints to carry forward

- **Coverage equivalent; scenarios preserved.** Same as the restructure.
- **Embedded backends are NOT poolable** — each `sprout*` is a distinct
  coverage scenario (fs × os_version × command_frame). Keep them
  scenario-parametrized.
- **Preserve resource-contention groups** — `--dist loadgroup`, the single
  -client console lock, the docker-host serialization.
- **otto stays server-less** (fable review #6): the pool is a *test-harness*
  lease, not a coordinator service.

## Open questions for the brainstorm

- Is the pool worth it given SSH/telnet servers already accept many concurrent
  clients (so the real bottleneck is per-VM CPU/disk under soak, not connection
  count)? Quantify the actual win before building.
- Provision more interchangeable Unix VMs, or just better-utilize the existing
  four?
- Does the lease integrate with otto's own JSON/DB reservation backend, or stay
  a pytest-only concern?
