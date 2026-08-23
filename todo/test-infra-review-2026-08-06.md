# Testing-infrastructure deep dive — tests that mask defects, and the code patterns behind them

**Date:** 2026-08-06
**Method:** six parallel read-only audits over the full surface — (1) flake-masking and
timing hacks in tests, (2) weak/vacuous tests, (3) harness internals (conftests, fixtures,
nox, coverage bootstrap), (4) product-code error handling, (5) boilerplate/library gaps,
(6) the web vitest suite. Mechanism claims marked *(verified)* were confirmed with
throwaway pytest probes in a scratchpad, not inferred from reading. Findings only — no
fixes applied. Effort scale: **S** ≤ 1 day, **M** 2–3 days, **L** ≥ 1 week.

---

## 0. Executive summary

All six auditors independently reached the same top-line verdict: **this suite's authored
discipline is well above average** — poll helpers overwhelmingly raise on expiry, host-down
is a loud host-named failure everywhere it matters (zero house-rule violations found),
there is exactly one `xfail` and it is strict, 460 of 716 `pytest.raises` carry `match=`,
the vitest suite has zero skips/retries/snapshots, and 68 of 76 broad product catches carry
a written justification. The defect classes this repo has already been hunting (vacuous
guards, silent skips) are visibly suppressed relative to any comparable codebase.

The residual damage is *concentrated*, not diffuse, in five shapes:

1. **The `@pytest.mark.retry(n)` mechanism is the single worst offender** (§2). It is
   active in every CI gate (not just dev runs), it *erases* retried failures from the
   report and JUnit, retried attempts run with pytest-timeout's alarm already cancelled
   *(verified)*, the shipped `OttoPlugin` twin re-runs the test body one extra decisive
   time after a successful retry *(verified)*, it currently shields a documented, unfixed
   product hang (`todo/hop_nc_transfer_flake.md`), and neither implementation has a test.

2. **Safety nets whose own failure is silent** (§4, §5). The harness re-injects
   `OTTO_SUT_DIRS` process-wide right after its own ambient strip, invisible to the pin
   that guards the allowlist *(verified)*; the registry-isolation guard's cache key
   (`len(sys.modules)`) is invalidated by its own teardown; the #110 CliRunner shield and
   the coverage-schema pre-init both degrade to no-ops without a symptom.

3. **Oracles that read a probe's value without checking its status** (§3). The chaos
   lane's final netem oracle and its hygiene bracket both report "clean bed" whenever the
   probe itself times out — the highest-leverage *small* fix in this report.

4. **Good patterns applied to one copy of a duplicated seam and not the other** (§6).
   The guarded readiness poll, the signature-gated retry, the logged skip, and the
   compensating-teardown helper each exist and are used correctly somewhere — and each has
   a sibling seam that didn't get the fix. This is the product-code mirror of the
   harness's known "enumerations inherit their omissions" failure mode.

5. **Incomplete propagation of helpers** (§7). `run_otto`, `active_context`,
   `make_host`, and `lab_data_path` each have a healthy user population *and* a large
   bypass population. The sharpest case: all ten hand-copies of the `run_otto` env dance
   kept the self-evident keys and dropped `-p no:tach` — the one key that encodes the
   issue-#193 scar; four of those copies spawn `otto test`, the exact trigger. The one
   genuinely *missing* library primitive is `wait_for(predicate, timeout)` — reimplemented
   26 times across product and tests in three mutually incompatible shapes.

One additional finding arrived by an irregular route (§10): the interact-e2e tests drive
`test1` without a pool lease while `test_shell_history_e2e` digests test1's
`~/.bash_history` — a real cross-suite race under `-n auto --dist loadgroup`.

---

## 1. Triage shortlist (by leverage)

| # | Item | Sections | Effort |
|---|---|---|---|
| 1 | Retry mechanism overhaul: record reruns (or gate off in CI), fix the shipped double-run, re-arm per-attempt timeout, re-run fixtures or document why not, add tests for both implementations | §2 | M |
| 2 | ~~Fix the hop-nc hang the retry shields, then drop `retry(3)` per its todo's own exit criterion~~ **DONE (Wave 2)** — hang root-caused live (unbounded tunnel awaits behind the LISTEN-vs-accept window); todo file deleted per its exit criterion | §2 | M |
| 3 | ~~Probe-status honesty in chaos oracles: `_bed.py:140`, hygiene bracket, `snapshot_host`~~ **DONE (Wave 3)** — one `check_probe_result` contract behind `run_probe`/`probe_text`/`snapshot_host`; unit-lane falsifiability pins | §3.1 | S |
| 4 | ~~`OTTO_SUT_DIRS` import-time reinjection: declare or relocate; widen the ambient pin to cover integration conftests~~ **DONE (Wave 4)** — runtime session fixture (the import-time justification was stale); per-tree collection pin + module-scope env-write AST guard | §5.1 | S |
| 5 | The ~13 one-line weak-test fixes: non-empty preconditions (7 sites) + `typer.Exit` exit-code asserts (6 sites) | §4.1–4.3 | S |
| 6 | Consolidate the 10 `run_otto` copies — closes the live #193 exposure | §7.1 | S |
| 7 | `wait_for(predicate, timeout)` in `otto/utils.py` + async twin; migrate 26 sites | §7.3 | M |
| 8 | Guarded readiness poll: fix `suite/suite.py:538` and the 10 test-side raw loops; right shape = `MonitorServer` exposes an awaitable started/raising start | §6.1, §3.5 | S |
| 9 | Interact-e2e ↔ shell-history race: land the host-lease fix as its own reviewed change | §10 | S |
| 10 | Error taxonomy: split `GitUnavailableError`; give `link/` (and tunnel/docker) `OttoError` subclasses so unreachable ≠ command-failed | §6.2–6.3 | M |
| 11 | Registry-isolation guard: replace the `len(sys.modules)` cache key with sound invalidation | §5.2 | S |
| 12 | e2e marker guard: defer the report instead of raising from a collection hook (xdist controller crash path) | §5.3 | S |
| 13 | NC put retry: gate on the named race signature (compose.py idiom), stop re-truncating dst | §6.4 | S |
| 14 | Arm `OTTO_TS_COVERAGE` in the CI dashboard lane (bundle-drift guard currently CI-blind) | §5.4 | S |
| 15 | Web cannot-fail cluster: page-meta bare digits, `status-*` testids, `useNow(null)`, seriestree slots | §8 | S |
| 16 | Shared git-repo test fixture (20 files, hermeticity divergence) | §7.2 | M |

---

## 2. The retry mechanism (deep-dive on the worst offender)

Two independent implementations of `@pytest.mark.retry(n)` exist; both are defective in
different ways, and their defects compound.

**2a. Harness copy erases failures — `tests/conftest.py:194-223`** *(verified)* — **high**.
`outcome.force_result(None)` at `:222` converts fail-fail-pass into a clean pass: JUnit
gets a bare `<testcase/>` with no `<failure>` and no rerun annotation (pytest-rerunfailures
records these; this doesn't). The docstring's "for dev pytest runs" means "under bare
pytest as opposed to `otto test`" — and every gate (make coverage, all nox sessions, CI)
runs bare pytest, so the hook is fully active in CI. The only trace of a retried failure
is a `logging.warning` in captured output. Flake rate is structurally invisible.

**2b. Retried attempts run with no timeout — `tests/conftest.py:215-221`** *(verified)* —
**high**. The retry loop sits in the post-`yield` half of the hookwrapper, so
`item.runtest()` runs after pytest-timeout's inner wrapper has already cancelled the
SIGALRM. Probe: `timeout=2` killed attempt 1 at 2s; attempts 2–3 ran unbounded. The two
tests that use the marker have a documented failure mode that *is* a hang, so `retry(3)`
converts one bounded 30s failure into up to two unbounded attempts. Only the non-fatal
`faulthandler_timeout=300` remains.

**2c. Retries run against dirty state — both copies** — **high**. `item.runtest()`
re-executes only the call phase; function-scoped fixture setup/teardown do not re-run
between attempts. For `tests/integration/host/test_hop_integration.py:357,:390` (real SSH
hops) the retried attempt inherits a half-torn-down transport. Also: the harness copy's
`except Exception` misses `_pytest.outcomes.Failed` (BaseException-derived), so a retried
body calling `pytest.fail()`/`pytest.skip()` escapes the loop mid-iteration.

**2d. Shipped copy double-runs the body — `src/otto/suite/plugin.py:240-261`**
*(verified)* — **high**. It is a plain hookimpl and `pytest_runtest_call` is not
`firstresult`, so after `OttoPlugin` retries to success, pytest's default runner executes
the body *again*, and that final run is decisive — probe showed `retry(3)` passing on
attempt 3 ran the body 4 times. `tests/conftest.py:203-206` documents this exact trap; the
harness copy works around a bug the shipped, user-facing copy (`plugin.py:21`, advertised
in `tests/repo1`) still has. Under `otto test` on `tests/repo1` both implementations are
on the conftest chain, so attempts multiply.

**2e. Zero test coverage; the sample repo normalizes the pattern** — **medium**. Neither
implementation has a single test (`grep -rn "retry" tests/unit/` → no hits); the only
"exercise" is `tests/repo1`'s `assert True` placeholder. And
`tests/repo1/tests/test_device.py:64-66` — the reference example — teaches otto users
retry-on-flaky-link as normal practice, on the code path that double-runs.

**2f. The retry currently shields a documented product defect** — **high**.
`todo/hop_nc_transfer_flake.md` documents the hop-nc hang (asyncssh silently swallows the
dropped connection; the next `await` hangs with no asyncio-level deadline), has an
investigation plan, and states the exit criterion: "drop `@pytest.mark.retry(3)` … and
remove this file". Combined with 2a/2b: CI stays green while a real "transfer can hang
forever" bug ships.

---

## 3. Flake-masking and timing hacks in tests

Beyond the retry mechanism, the damage clusters into probe-status blindness, premise
sleeps, and floors weakened below knowable values.

### 3.1 Oracles that never check probe status — high

- **`tests/e2e/chaos/_bed.py:140`** (consumers `test_tunnel_link_chaos.py:351`,
  `test_reboot_chaos.py:223,:230,:62-73`): `assert "netem" not in (out.value or "")`
  with `out.status` never checked. An exec timeout returns
  `CommandResult(status=Error, value="Command timed out…")`, which trivially satisfies the
  assertion. This is the **final oracle for `otto link repair --all`** in three chaos
  tests — a real "repair left netem on the bed" bug goes green whenever the probe itself
  is unhappy. `test_docker_chaos.py:450-458` does it right (explicit "Oracle honesty"
  comment).
- **`tests/e2e/chaos/conftest.py:74-78`** hygiene bracket (+ `bed_hygiene.py:57-59`):
  `snapshot_host` never checks `result.status` and the diff is new-only (`after − before`).
  An after-snapshot whose probes all time out — exactly what a wedged/blackholed/rebooting
  host produces — yields empty sets, i.e. "clean" across tunnel daemons, impair timers, nc
  listeners, staging, docker. Saved today only by accident: the qdiscs leg is an equality
  compare, so a blank probe trips it — an undocumented canary one `_QDISC_DEVS` edit from
  vanishing. — medium/high

### 3.2 Chaos/conditional-injection tests that can pass with zero chaos — high

- **`tests/e2e/chaos/test_tunnel_link_chaos.py:291-298`**
  (`test_interrupt_during_rollback_still_reaps`): SIGINT sent only
  `if p.proc.poll() is None`; the only confirmation it was handled is
  `contextlib.suppress(AssertionError)` around `wait_for_stderr(BANNER)`. On any run where
  otto exits first, the test silently degrades to "add fails against a bound port" —
  passing with no chaos injected, nothing logged.
- **`tests/e2e/host/test_interact_e2e.py:239`** (`test_resize_triggers_remote_side_update`):
  `contextlib.suppress(TimeoutError)` around `sess.expect(b"50 132")` — the *only*
  assertion that the resize reached the remote side, and the sole coverage of
  `change_terminal_size`/NAWS ("the unit tests can't reach them"). Plus a hard
  `time.sleep(0.3)` for the SIGWINCH handler. Sibling issue at `:107-113`: expect-timeout
  downgraded to `drain(0.2)` (medium; partially backstopped by a later log assertion).

### 3.3 Premise-establishing sleeps whose failure mode equals the asserted outcome

- **`tests/unit/host/test_session.py:482`** — high. Feeder sleeps 0.15s into a recovery
  window that closes at t≈0.4. If the sleep overruns under xdist load, nothing is fed,
  recovery times out, and the asserted outcome (`not session.alive`) is **identical** —
  the test greens without ever exercising "a literal `$?` echo must not be accepted as
  recovery confirmation". Its sibling at `:589` asserts the same thing from a no-feed
  premise, so the two are observationally indistinguishable; no positive control.
- **`tests/integration/host/test_session_stability_integration.py:202-203`** — medium.
  `sleep(0.5)` to establish "the long exec is pinned"; premise never asserted — vacuous
  pass against an uncontended pool.
- **`tests/integration/host/test_session_stability_integration.py:466-467`** — medium.
  Fixed 2.0s grace before the nc-listener leak check: an orphaned listener dying on its
  own within 2s passes while otto's cleanup never ran; a slow-correct reap false-fails.
- **`tests/unit/host/test_console_lock.py:47`** — medium. `time.sleep(0.3)` "let the churn
  ramp up" with no assertion the reader processes reached the churn loop — writer-fairness
  can go untested while `waited < 5.0` passes trivially.
- **`tests/unit/host/test_connection_race.py:32-36`** (used ×5) — medium. Fixed
  `range(N + 4)` yield-count settle: a refactor adding one await before the lock
  serializes the tasks, `calls == 1` still passes, and all five race tests silently stop
  racing.
- **`tests/unit/host/test_session.py:742,:1387,:1412`** — high. `sleep(0.01)` gating reads
  of background-task state (`len(s.written)`, `built[0]` → IndexError on a starved loop).

### 3.4 The 65-site sleep-sync family — medium (as a family)

`tests/unit/host/test_session.py` (42 sites), `test_zephyr.py` (12),
`test_session_output_buffering.py` (6), `test_session_logging.py` (5): wall-clock sleeps
as the sole ordering between a feeder task and the code under test. They don't flake today
only because `MockSession.feed()` writes into an `asyncio.StreamReader` that buffers an
early feed — an emergent property nothing enforces. One `MockSession` change turns the
family flaky at once; one shared `_feed_after_ready()` Event helper fixes all 65.

### 3.5 Silent or unguarded polls

- **10 raw `while not server.started` loops** — medium:
  `tests/unit/monitor/test_server.py:151,182,209,390,752,788,825,851`,
  `test_server_tls.py:72`, `test_server_auth.py:232`. The correct helper
  (`_wait_started()`, `test_server.py:218` — checks `task.done()`, re-raises) exists 30
  lines away; only 3 sites use it. Startup death ⇒ 180s hang, no traceback. Near-miss at
  `test_server_signals.py:27`. *Mirror of the product finding §6.1.*
- **`tests/e2e/monitor/test_monitor_e2e.py:188`** — medium. Only silent-expiry poll in
  the monitor e2e tree; `rows_found` never asserted.
- **`tests/_fixtures/_host_pool.py:35-52`** — low. Pool lease polls forever; a stale
  flock surfaces as a generic "Timeout >Ns" 5–15 minutes later instead of "no unix
  host free".
- **`tests/_fixtures/_dashboard_harness.py:159`** — low. Transport-reap loop exits
  silently after 10 passes and `loop.close()` runs anyway — producing exactly the
  misattributed unraisable its own docstring describes.

### 3.6 Floors weakened below knowable values — medium

- `tests/unit/monitor/test_collector_run.py:110,111,128,147,242,283-284`: knowable 4
  ticks asserted `>= 2`; ~11-vs-~3 parser calls asserted only as a 2× ratio. A collector
  regression halving tick rate passes. A virtual clock would make these exact.
- `tests/unit/suite/test_plugin.py:382,:388`: fake ticks ~10 appends, asserts `> 0` —
  "ticks zero times" (the guarded bug) vs "ticks once then stalls" only the count
  distinguishes.
- `tests/integration/host/test_snmp_integration.py:95,:104`: `Uptime >= 0.0` can never
  fail for an unsigned counter; the Threads comment names a floor of 3, asserts `>= 1`.
  Also `:120-125`: a dead `finally: pass` leaks the SNMP collector transport.
- `tests/e2e/chaos/test_connection_drop.py:226-227`: documented ~7s keepalive detection
  accepted anywhere within 150s — a total keepalive regression that rides the `--timeout
  120` backstop still passes.
- `tests/e2e/tunnel_stability/test_traffic.py:143-145`: the post-churn probe re-sends 5× —
  a survivor tunnel dropping 4 of 5 datagrams passes, and the streaming leg's 0.95 floor
  explicitly excludes `final_probe`. (`:44,:167`: the 0.95 UDP floor itself hides ≤5%
  forwarding loss; documented as intentional.)
- `tests/e2e/tunnel_stability/_harness.py:43`: for `test_churn`/`test_traffic`/
  `test_adversity` the 420–900s soak ceiling is the *only* hang detector (no inner
  per-operation budget) — `add_tunnel` regressing 2s→60s/cycle passes. — low

### 3.7 Assorted (low)

`test_unix_host_integration.py:390-393` stale comment claims a retry the helper's
docstring explicitly forbids (`_transfer_retry.py:8-11`) — invites "restoring" it;
`:253-256` dead `if len(...) >= 2` under `assert len(...) >= 3`.
`test_docker_run_get_put.py:158` accepts any non-Success where Timeout is knowable.
`tests/e2e/chaos/conftest.py:33-37` FD guard re-measures only when about to fail and uses
the second reading — a steady ≤4 fd/test leak is structurally invisible; plus the
integration/chaos copy of this fixture has drifted (§5.5).
`_bed.py:90-91` unscoped except-continue in lab-record parse degrades a schema regression
to `IndexError` on `links[0]`.
`test_reboot_chaos.py:39,:86,:144,:205` reboot recovery (~17s real) allowed 620s, elapsed
only printed — known-deferred (`todo/chaos-reboot-followups.md` §3/§4).
`test_docker_chaos.py:463-468,:481` cancel-landing coverage only logged — the shielded
`compose_down` path can silently stop being exercised; `:115-119` the chaos bridge
log-and-continues `host.close()` failures (product teardown is what the lane
characterizes).
`test_session_chaos.py:112` force-path test accepts the graceful outcome and records
neither.
`test_monitor_e2e.py:116` `except sqlite3.OperationalError` also absorbs locked/disk-error
— failure stays loud, cause erased.
`dashboard/conftest.py:168` 60s Playwright default timeouts with no responsiveness
assertion outside the soak; `test_replay_soak.py:100` chromium-only skip converts a
measured WebKit main-thread defect into a permanent non-result.
`test_server.py:282` retry catches any `RuntimeError`, including `_wait_started`'s own
"serve() returned before signalling startup" — genuine startup regressions triaged as
port collisions.
`test_scoping.py:68` `suppress(TimeoutError)` around `collector.run(duration=0.2s)` —
a never-honors-duration regression is swallowed.

**Dismissed as fine** (so they aren't re-flagged): pytest-rerunfailures/flaky not
installed; the wedged-bed gate and all live-bed host probes fail loud and host-named
(house-rule compliant); seeded chaos sleeps are injected chaos, not synchronization;
host-contract skips are declared-capability gates; ~20 `asyncio.sleep(0)` yields are
deterministic; `test_heap_watermark`'s exact-equality assertion and
`test_docker_run_get_put.py:166`'s self-calibrating comparison are exemplary.

---

## 4. Weak and vacuous tests

The "guards that cannot fail" hunt. Three shapes: data-dependent guards nothing pins
non-empty, semantics asserted by name but not by code, and un-upgraded tails.

### 4.1 Cannot-fail guards — high

- **`tests/unit/cov/test_anchor.py:193,197,202,207,212`** (helper `:186`): five
  parity-only tests (`…_is_verbatim`, `…_is_unverifiable`) whose *only* assertion is
  `lazy == batched` — a resolver returning `unverifiable` for everything passes all five.
  The file's own comment at `:132` records that under a corrupting config both paths
  degrade together. Gap: the four named scenarios need their semantic outcome asserted
  alongside parity.
- **`tests/unit/scripts/test_lab_health.py:37`**: the 41cf70c routing-crash guard's
  assertions all sit under `if "creds" not in host` over real lab data. Live today (7
  credless hosts), but nothing pins that — one fixture edit makes it a silent no-op.
  Fix: `assert any("creds" not in h for h in hosts)`.
- **`tests/unit/test_lab_data_hops.py:36`**: guard against "an over-eager sweep deleting
  the real test4 hops" *skips* when there are no embedded hosts — i.e. on the strictly
  worse version of the same accident. **Already firing**: `tech2/lab.json` has zero
  embedded hosts, so one of two parametrizations is permanently green-by-skipping.

### 4.2 Exit-code and message-channel weaknesses — medium-high

- **6 `pytest.raises(typer.Exit)` sites with no `exit_code` assert** — and `typer.Exit()`
  defaults to **0**: `tests/unit/cli/test_bootstrap_gate.py:35`
  (`test_gate_still_blocks_on_errors` — "blocking with a success code" is precisely the
  regression it exists to catch), `tests/unit/cli/test_monitor.py:299`,
  `tests/unit/cli/test_leaf_render.py:225,:239`,
  `tests/unit/cli/test_dynamic_host_commands.py:154`,
  `tests/e2e/cli/test_monitor_cli.py:172`. 15 of 21 sites do assert it; these are the
  outliers.
- **`tests/unit/cli/test_cov.py:337,:350`**: two distinct validation failures each
  asserted only as `exit_code == 1`, with `patch.object(cov_module.logger, "error")`
  muting the discriminating channel. Any unrelated exit-1 (even an import error) keeps
  both green.

### 4.3 Loop-guarded assertions with no non-empty precondition — medium

At-risk sites (of 81 loop-guarded tests repo-wide, most iterate provably-non-empty
constants): `tests/unit/cli/test_listing.py:415` (`collect_tests()` returning `[]` —
the worst collection bug — passes cleanly), `tests/unit/scripts/test_gen_monitor_fixtures.py:121`
(`build_all()` → `{}` ⇒ the credential-leak scan asserts nothing), `:128`, `:246`,
`tests/unit/test_tuple_return_debt.py:117`, `tests/unit/test_webassets_guard.py:61`.
Fix: one leading `assert items` each.

### 4.4 Un-upgraded tails — medium/low

- **52 bare `pytest.raises(ValidationError)`** (no `match=`): heaviest in
  `tests/unit/models/test_monitor.py` (×10), `test_settings.py` (×9),
  `test_host_specs.py` (×7), plus `test_option_specs.py`, `test_settings_coverage.py`,
  `test_link_specs.py`, `test_monitor_tunnels.py`, +15 others. With `extra='forbid'`
  models, any validation error satisfies these — baseline-dict drift silently retargets
  the test. 460/716 raises already carry `match=`; this is the tail.
- **Flag-accepted padding**: `tests/unit/cli/test_monitor.py:154-223` (7 sites),
  `test_main.py:539-562` (5 sites) — `--interval 10` and `-i 10` asserted identically as
  exit-0; neither checks the value reached the collector.
- **`tests/unit/cli/test_main.py:128`** self-admitted empty-path test (`--list-labs`
  rendering has zero unit coverage; e2e covers content).
- `tests/unit/cli/test_cli_registry.py:243` `pytest.raises(Exception)` where
  `dataclasses.FrozenInstanceError` is the contract. (The other 3 broad-raises sites carry
  sound written justifications — leave them.)
- `tests/e2e/cov/test_embedded_coverage_e2e.py:64` stale docstring says "Skip unless
  zephyr37_llext answers" while the body correctly hard-fails — invites a house-rule
  regression by a consistency-restoring editor.

### 4.5 Environment-dependent skips that retire a lane — medium

`tests/e2e/cov/test_embedded_coverage_e2e.py:58,:168` and
`tests/repo3/tests/test_embedded_coverage.py:182`: build-artifact / build-config absence
⇒ the embedded-coverage e2e lane vanishes into a green run. Internally inconsistent: the
same file's `clean_zephyr37_llext` (`:113`) hard-fails with "a dead bed can't hide behind a
green run" — the same argument applies to a missing build artifact. Also
`tests/unit/test_tier_marker_invariants.py:175`: skip reason "tests/e2e/chaos not created
yet" is stale (the dir exists) — a rename would silently retire the chaos marker guard.

Full skip/xfail inventory in **Appendix A**. Notably: **zero host-availability skips**
anywhere — every live-host candidate fails loud and host-named.

---

## 5. Harness internals

### 5.1 `OTTO_SUT_DIRS` re-injected process-wide after the strip *(verified)* — high

`tests/integration/conftest.py:32` (`ensure_sut_dirs()` →
`os.environ.setdefault("OTTO_SUT_DIRS", <tests/repo1>)`, at import time, "before any otto
imports" because config reads it at import to build the module-level `_repos` singleton).
Confirmed live: with an ambient `OTTO_SUT_DIRS=/ambient/leak/path` exported, the root
strip removes it and the integration conftest then sets its own, process-wide, in every
xdist worker, before any test runs. Consequence: `make coverage`/`nox -s tests_all` run
the **entire unit tree** with `OTTO_SUT_DIRS` set while `make coverage-unit`/CI
`tests_hostless` run it unset — two lanes, two ambient configurations, invisibly. It is
exactly the variable the root strip's own comment (`tests/conftest.py:117-134`) names as
the historical poison; it is deliberately **not** in `AMBIENT_OPT_INS`, so the pin
`test_probe_ambient_otto_env_is_stripped` would fail on it — except the pin's inner
subprocess collects a single `tests/unit` file, so the integration conftest never imports
and the assertion structurally cannot see it.

### 5.2 Registry-isolation guard degrades silently — high

`tests/conftest.py:1283-1312` caches its Registry scan on `len(sys.modules)` — and its own
teardown (`_restore_registries`, `:1441-1442`) pops modules, as does
`tests/unit/conftest.py:101-107`'s `purge_tmp_imports`. Import N + evict N ⇒ count
unchanged ⇒ stale cache ⇒ the next snapshot omits a now-existing registry and leaked
entries are never rolled back. `test_registry_isolation_e2e.py` pins only that the fixture
is *active*, not that discovery is complete.

### 5.3 e2e marker guard raises from a collection hook — the xdist crash path — high

`tests/e2e/conftest.py:105-108` raises `pytest.UsageError` from
`pytest_collection_modifyitems`. `tests/e2e/monitor/dashboard/conftest.py:120-135`
documents (empirically) that any exception from a post-sessionstart hook crashes the xdist
controller. Repo addopts is `-n auto`, so a mistagged e2e test produces an xdist internal
error instead of the offender list the guard composed. Fix = deferred report (it needs
items, so it can't move to `pytest_configure` as-is).

### 5.4 Guards that no-op silently — medium

- **#110 CliRunner shield** (`tests/conftest.py:1247-1253`): `except ImportError: yield` —
  a pytest rename of `_LiveLoggingStreamHandler` inertly disarms the guard for every
  `CliRunner.invoke` site while `log_cli = true` keeps the hazard live; the two pin tests
  assert reach, not liveness. Cheap fix: hard-fail or version-assert.
- **Coverage-schema pre-init** (`tests/_fixtures/_coverage_preinit.py:103-105`,
  consumed `tests/conftest.py:253-254`): `except Exception: return False`, stash written
  but never acted on in a real run. A coverage.py rename re-opens the `no such table:
  context` release race silently — and the docstring at `:88-92` records this guard has
  already failed silently once.
- **TS-coverage bundle-drift guard is CI-blind** (`tests/_fixtures/_ts_coverage.py:74-81,
  :120-126`): armed only by `make dashboard` (`Makefile:618`); CI's and nightly's
  dashboard jobs never set `OTTO_TS_COVERAGE` (`noxfile.py:330-345`), so the CDP
  collection *and* its zero-match drift assertion are inert in exactly the three-engine
  lane where a bundle-name change would first appear.
- **addopts-clearing lanes drop `-p no:tach`** (`noxfile.py:158-167`, `:378`;
  `Makefile:902`): the conftest's stub is seeded after the pytest11 entry point loads, and
  `make doctest-src` runs in the dev venv which can carry tach — only the addopts entry
  protects plugin load (#193).

### 5.5 Fixture drift and destructive autouse — medium

- **Three copies of the FD-watermark fixture, one drifted**:
  `tests/integration/chaos/conftest.py:24-38` takes its baseline *without* `gc.collect()`
  (unlike `tests/e2e/chaos/conftest.py:26-37` and
  `tests/e2e/tunnel_stability/conftest.py:29-46`), inflating `before` by a load-dependent
  amount — its docstring's "Same shape as…" is the sentence that stops a reader from
  diffing. Shared body belongs in `tests/_fixtures/`.
- **Session-scoped autouse reaper SSHes to a hardcoded bed IP and runs `docker rm -f`
  with every failure suppressed** (`tests/integration/conftest.py:91-116`, IP at `:50`):
  destructive remote work per xdist worker for any session collecting
  `tests/integration`; a half-completed or over-matching reap leaves no trace; the
  `-e2e-`/`-noexist-` name fragments are the only guard.
- **Tunnel reaping swallows every exception**
  (`tests/e2e/tunnel_stability/conftest.py:61-68`): a raising `remove_tunnel` is a product
  defect in the exact path the suite soaks; the leftover sweep catches the consequence but
  mis-attributed, original exception gone. Log it.
- **Two autouse fixtures reset to default instead of restoring a snapshot**
  (`tests/conftest.py:543-551,:554-580`): unlike their snapshot-and-restore siblings, a
  module-scoped fixture's logging/bootstrap state is destroyed after its first test. — low
- **Stale-bundle guards depend on developer-machine mtimes, duplicated**
  (`tests/e2e/monitor/dashboard/conftest.py:50-88`,
  `tests/e2e/cov/report_browser/conftest.py:26-57`): checkout/stash/worktree ops defeat
  mtime comparison in both directions; a content hash of `WEB_SRCS` (already enumerated at
  `Makefile:557-564`) would be correct and shareable. — low

### 5.6 Structural notes — medium/low

- **Timeout architecture** (`Makefile:123-124`; `pyproject.toml:219-234`): two workers ×
  180s call budget on sequential tests can exceed the outer `timeout 360s`, which SIGKILLs
  the gate — destroying JUnit and `.coverage.*` fragments, reporting an unexplained kill
  instead of a named slow test. And `timeout_func_only = true` excludes setup/teardown —
  where this harness does its heaviest work (loop reaper, registry snapshot/restore,
  transport scan, bed probes, FD watermarks); a wedge there is bounded only by
  `faulthandler_timeout`, which dumps but does not kill.
- **Real signal-handler installation is inert in every default lane**
  (`tests/conftest.py:613-700`): well-documented rationale, but coverage of the real
  `install_handlers` path lives only in nightly tier-2 chaos; `real_sync_phase` has an
  opt-out, `_CommandRun` has none.
- **`_BED_HEALTH` is monotone and substring-triggered**
  (`tests/integration/host/conftest.py:159-161,:275-321`): right design (fail loud), but
  no path back for a recovered console, and any future test whose assertion message merely
  *quotes* "shell never became ready" poisons the backend.
- **Colour/TTY env mutated at import, never restored** (`tests/conftest.py:112-115`):
  `TERM=dumb` inherited by every subprocess including PTY e2e — a terminal-capability
  regression in otto's own Rich/PTY output is masked suite-wide.
- **Subprocess coverage bootstrap** (`tests/_coverage_bootstrap/sitecustomize.py:14-16`):
  prepended `PYTHONPATH` silently shadows any real `sitecustomize`; bare `import coverage`
  kills a coverage-less child with ImportError instead of running uninstrumented.

Hook and nox-lane inventories in **Appendices B and C**.

---

## 6. Product code: error handling and control flow

Density map: 324 `except` clauses in `src/otto`, 76 broad, **68 of those with inline
`noqa: BLE001` justifications**. Nesting shallow: 24 depth-2 `try` blocks, zero deeper.
Top files by count: `config/completion_cache.py` (25), `host/session.py` (20),
`host/interact.py` (19), `cli/cov.py` (15), `host/transfer/nc.py` (14). The recurring
defect is **not** broad catches — it is good patterns applied to one copy of a duplicated
seam and not the other.

### 6.1 Unguarded infinite readiness poll — `src/otto/suite/suite.py:538` — high

`while not self._monitor_server.started: await asyncio.sleep(0.05)` with no deadline and
no `task.done()` check. If uvicorn fails startup (port in use, bad TLS →
`RuntimeError` at `monitor/server.py:800`), `_run()` dies, `started` never flips, the loop
spins forever, and the exception sits unretrieved on the task. The identical loop at
`monitor/server.py:811-820` *has* the guard, with a comment explaining exactly this
failure mode. Right shape: `MonitorServer` exposes an awaitable started event (or a
raising `start()`) so the unguarded loop is unwritable — which also fixes the 10 test-side
copies (§3.5).

### 6.2 `link/` signals three conditions through one `RuntimeError` — high

`src/otto/link/manage.py:144-156` raises the same type for transport-dead, timeout, and
command-ran-and-failed; catch sites (`:335`, `:801`, `:810`, `:757-760`,
`cli/link.py:185,:245`) collapse the family into `unreachable=True` — a reachable host
with a broken `tc` binary shows as **unreachable** in `otto link list`, and
`_cancel_timers` reports "0 timers cancelled" for host-down and ps-failed alike. The
module's own convention (`:745-752`: ValueError = structural, RuntimeError = live) is
violated by `_ensure_not_foreign` (`:299-303`). 24 named `OttoError` subclasses exist
in-tree; `link/` defines none. Related repo-wide gap: `raise RuntimeError` ×63,
concentrated in link/manage (10), tunnel/manage (8), nc.py (6), docker/staging (6),
docker/compose (6) — `tunnel/records.py:18` defines `TunnelScanFailedError(OttoError,
RuntimeError)` while its sibling raises bare RuntimeError ×8. A library consumer cannot
`except OttoError` around tunnel/link/docker. Proposed:
`LinkHostUnreachableError`/`LinkCommandFailedError` (+ a shared
`HostCommandError(OttoError, RuntimeError)` and one `run_or_raise(host, cmd)` helper —
only 3 modules ever check `result.timed_out` today; seven exec-heavy modules never do).

### 6.3 `GitUnavailableError` — one class, three conditions — medium

Raised for git-missing, not-a-repo, and nonzero-exit; three call sites recover the
distinction by **string-matching git's stderr** (`coverage/capture/gitio.py:221-224,
:250-253`, `coverage/anchor.py:53-54` — locale/version-fragile), and a fourth
(`cli/cov.py:538-541`) silently conflates "no git" with "user.email unset". Split into
`GitMissingError`/`NotAGitRepoError`/`GitCommandFailedError` and all four become
type-dispatched.

### 6.4 NC put retry retries every failure and re-truncates dst — high

`src/otto/host/transfer/nc.py:1028-1038`: the comment names "the narrow
listener-readiness race" but the guard is `if not result.is_ok` — it retries
permission-denied, disk-full, and size-verify mismatches too; `nc -l … > {dst}`
re-truncates a destination the first attempt already clobbered, and the first failure's
message drops to DEBUG. `docker/compose.py` solves the same shape correctly with
`_is_transient_network_race(output)` (that retry — `compose.py:319-337` — is the
best-written in the repo and even documents its own escalation levers). The `get` path has
no retry, so the two directions of one backend have different failure semantics.

### 6.5 Remote exec in bare `finally` masks the primary exception — high

`src/otto/host/docker_host.py:523`, `:600`, `src/otto/host/unix_host.py:583`: cleanup
`exec`/`close` in a bare `finally` — the situations where the body raises are precisely
those where the cleanup also raises, replacing the real exception with transport noise.
House tools exist and are used elsewhere (`host/connections.py:teardown_step`;
`lifecycle.compensate()` at `docker/compose.py:542-554`, `tunnel/manage.py:486`,
`link/manage.py:669`, `nc.py:1017`).

### 6.6 Medium/low

- **Completion-cache lost-update race** (`config/completion_cache.py:764-789,
  :1571-1591, :1735-1754`): three writers do whole-file read→mutate→`os.replace` with no
  lock over the RMW window (`_atomic_write_json` guarantees reader atomicity only); two
  concurrent TAB completions silently drop a namespace. The `O_EXCL` lock helper exists
  (`:1606`) but guards only the collection subprocess; 7 more sites repeat the read half.
  One `_mutate_cache(namespace, fn)` under the existing lock removes the duplication *and*
  the race. Also `:1616-1619`: the lock-steal gives up on the one `OSError` that means the
  lock just freed.
- **Lab enumeration silently skips malformed lab files** (`labs/json_repository.py:250-251,
  :291-292`): hosts vanish from `otto host list` and TAB completion with no log line while
  direct lookup says "unknown host". The same symptom was fixed at `:188-193` with a
  logged reason; two of three enumeration paths didn't get it.
- **Hardcoded 0.1s settle split across seams** (`host/session.py:654,:943` vs
  `settle=0.0` at `:686`): the tuning knob (`confirm_live(settle=…)`) and the value live
  in different files; the only unnamed timing literals in the module.
- **`do_for_all_hosts` collect mode** (`context.py:302-308`, `config/fleet.py:191`)
  returns `dict[str, T | BaseException]` — the largest remaining hole in the "every verb
  returns a Result" invariant; `Results.collect` already models it. Intentional today.

---

## 7. Boilerplate and library gaps

Overall: top quartile — shared homes exist and are good, and the centralization habit is
real (`_bed.py:66` documents hoisting after byte-identical verification). The dominant
failure mode is **incomplete propagation**: a helper gets written, new code uses it, and
the pre-existing copies are never retired.

### 7.1 `run_otto` re-implemented 10× — and every copy dropped the guard — high

Copies: `tests/e2e/cov/test_coverage_e2e.py:61,:75`,
`tests/e2e/suite/test_stability_e2e.py:39,:53`,
`tests/e2e/config/test_completion_cache.py:39`,
`tests/e2e/cov/test_embedded_coverage_e2e.py:121`,
`tests/e2e/host/test_host_transfer_e2e.py:62`,
`tests/e2e/host/test_host_priv_modules_e2e.py:61`,
`tests/e2e/docker/test_docker_e2e_cli.py:69`, `tests/e2e/run/test_run_exec_e2e.py:49`,
`tests/e2e/host/_pty_driver.py:36`, `tests/integration/chaos/_driver.py:26`. Canonical:
`tests/e2e/_otto_subprocess.py:24` — 9 of 10 copies already import sibling names from that
module, so this is not a discovery failure. **Live defect**: `run_otto` sets
`PYTEST_ADDOPTS="-p no:tach"` (#193 — tach's Rust pytest plugin panics on consecutive
in-process sessions); zero copies carry the key, and four spawn `otto test`
(`test_stability_e2e.py:82` runs `--iterations 3`, the exact trigger). Every copy kept the
self-evident keys (`COVERAGE_PROCESS_START`…) and dropped the one that encodes a scar.
Fix: delete all ten; extend `run_otto` with `extra_argv_prefix` + `cwd` (~180 lines).

### 7.2 Hand-rolled git-repo harness ×20 files, hermeticity diverging — high

`tests/_fixtures/_repo_timeline.py:18` plus 19 test modules re-type `_GIT_ENV` +
`git()`/`_git()` wrappers. Measured: only 6 of 19 env dicts set
`GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM` — the other 13 leave `/etc/gitconfig` live (a
global `commit.gpgsign` or `core.hooksPath` ⇒ opaque `CalledProcessError`;
`test_changelog_rendering.py:86-90` documents the hazard, never propagated). Four sites
use a third idiom, two a fourth; only 2 pin `GIT_AUTHOR_DATE`. Fix:
`tests/_fixtures/gitrepo.py` with `git_env(home)` + `TmpGitRepo` (~250 lines; hermeticity
becomes one decision instead of nineteen).

### 7.3 `wait_for(predicate, timeout)` — the missing primitive — high

26 reimplementations of poll-until-deadline across product **and** tests
(`src/otto/host/host.py:203,:1067,:1116,:1125`, `unix_host.py:811`, `nc.py:138,:500`;
`tests/_fixtures/tunnel_bed.py:231,:261`, `tests/e2e/test_tunnel_e2e.py:388,:410`,
`test_tunnel_link_chaos.py:90,:188`, `test_link_impair_e2e.py:473,:739`,
`test_signal_run.py:41,:50`, `_driver.py:77`, `_sshd.py:87`, `_pty_driver.py:172,:219`,
`test_lifecycle_sync_phase.py:83`, +5) in **three incompatible shapes**: check-first,
sleep-first (predicate never evaluated at t=0 — `test_link_impair_e2e.py:475,:741`), and
do-while (`unix_host.py:811`, which needed a 10-line comment explaining why check-first
was wrong for it). Expiry behavior diverges across raise/return-False/pytest.fail/silent.
No `wait_for`/`poll_until`/`eventually` exists anywhere in `src/otto`. Proposed:
`otto/utils.py::wait_for(predicate, timeout, interval=0.1, *, probe_first=True,
on_timeout=…)` + async twin (~180 lines deleted, three semantics → one parameterized).

### 7.4 Helpers with a bypass population — high/medium

| Helper | Home | Bypasses | Consequence |
|---|---|---|---|
| `active_context` | `tests/conftest.py:508` | 28 sites hand-roll `set_context`/`try/finally`/`reset_context` (59 raw calls; `test_run_api.py:126` etc.) | ~110 lines + 24 function-local imports; leak-prone finally ×28 |
| `make_host` | `tests/_fixtures/labdata.py:40` | 11 local factories / 155 raw `UnixHost(` sites; byte-identical fixtures ×3 | add lab-data-free `bare_host()` |
| `lab_data_path` | `labdata.py:26` | `tests/e2e/_selection_fixtures.py:15`, `tests/e2e/test_repo_wide_conftest.py:14`, `tests/integration/chaos/_target.py:92` | the exact `parents[N]` arithmetic the docstring forbids; 49 `parents[N]` sites total — export `TESTS_ROOT`/`PROJECT_ROOT` from `paths.py` |
| `DispatchRunner` | `tests/_fixtures/dispatch.py:36` | 11 sites use bare `CliRunner` on sub-apps | **latent + coupled**: bare CliRunner skips `wrap_leaf_callbacks`; safe only because every cov/monitor/suite/schema leaf is currently sync — and they're sync because `cli/cov.py:788,:945` still use the retired self-bridging pattern. Converting those to async leaves silently invalidates 7 test files. Migrate DispatchRunner first, or both in one commit. |
| `_render_panels` | `src/otto/cli/test.py:262` (module-private) | byte-identical copies at `cli/run.py:42`, `cli/main.py:101` | promote |
| `OttoModel` | `src/otto/models/base.py:6` | `coverage/capture/model.py:34,:44` re-spell `extra="forbid"` | 2 of ~50 models |

### 7.5 The SUT-repo scaffold — 48 write sites / 27 files — high (volume)

`mkdir(.otto)` → write `settings.toml` literal → `Repo(sut_dir=…)`, everywhere from
`tests/unit/config/test_repo.py:386` to `tests/e2e/chaos/test_console_chaos.py:124`.
Every new config field currently means 27 edits. Proposed
`tests/_fixtures/sutrepo.py::make_sut_repo(tmp_path, **sections)` (~200-260 lines); folds
in the triplicated `_repo(...)` at `test_dependencies_{resolution,ordering}.py` /
`test_repo_deps_panel.py`.

### 7.6 Product-side dances — medium

- **CLI error rendering in four dialects** (sanctioned `fail`/`print_error` ×15; plain
  red print + Exit(2) ×7 in link/tunnel/reservation; `typer.echo(err=True)` ×9 in
  monitor; `logger.error(escape_markup(…))+Exit(1)` ×6 in cov, two byte-identical):
  checked — *not* a live escaping hole (the ast-grep gate's f-string scope is deliberate),
  but four render surfaces and inconsistent exit codes; `invoke.py:184-186` already
  concedes the consolidation is owed. Add `usage_error(msg)` + `fail_from(exc)` — the
  absence of those two variants is why the dialects grew.
- **Path normalization: four verbs for one repo root**: `anchor_path` ("deliberately does
  not resolve()") vs `.absolute()` ("deliberately not resolve") vs three uncommented
  `.resolve()` calls in `coverage/collect.py:343,:393,:411` (one re-resolving an
  already-anchored dir via a private closure `_anchor_build_dir` at `:357`) — plus three
  sites whose comments exist only to keep the invariant hand-synced
  (`reporter.py:749-755`, `ticket_export.py:98`, `capture/model.py:172`). The two
  behaviours are legitimately different (store keys resolve; config anchoring preserves
  symlink identity) — the defect is that nothing names the boundary. Name the second verb
  (`filesystem.py::store_key_root`). Given the settings-path-anchoring history, the
  ratchet matters more than the lines.
- **TOML read+parse+swallow ×7** (`cli/init.py` ×5, `config/repo.py:230`,
  `coverage/overrides.py:185`): three encoding strategies; `init.py`'s `read_text()`
  lets a non-UTF-8 byte under a C locale escape as an uncaught `UnicodeDecodeError`;
  `overrides.py` opens binary with a 10-line comment explaining why — never propagated.
  `load_toml(path)` opening binary.
- **`to_runtime()` field-by-field mirrors ×14** (`models/options.py`, `host.py`,
  `settings.py`): mutable-aliasing policy inconsistent *within one function*
  (`options.py:114` aliases `env` and copies `extra` on the same line) — a future
  in-place mutation corrupts the spec for some backends and not others.
- **Typer sub-app constructor ×10 + no-op callbacks ×5**; help strings written up to 3×.
  `otto_group(name, help)` factory.
- Smaller confirmed dances (3+ sites each): registry-membership validators (unsorted
  join at `host/factory.py:176` ⇒ nondeterministic error text), `^_` comment-key rule ×4
  (one tolerates `$schema`, others don't), `model_validate`→friendly-error ×6 (four
  different surfaces), 17 thin `@field_validator` shells that `Annotated[...,
  AfterValidator]` erases, `_ok()`/`_fail()` CommandResult factories ×13 defs,
  byte-identical `FakeLab` ×3, `_PATH_LIST_SEP` regex ×3 cross-referencing each other,
  lab.json writer helpers ×7 (+43 copy-pasted `"element": "alt1"` host dicts).

---

## 8. Web vitest suite

Cleanest surface of the six: **zero** skips/todo/only/fails/retries/snapshots/real-time
sleeps; `expect.requireAssertions` on; `sequence.shuffle` on (file and within-file);
console.warn/error escalate to failures with an empty allowlist; coverage thresholds
enforced at run and merge time. `perf_budget.test.ts` is the strongest file — reworked
from wall-clock to Proxy-counted sample reads, mutation-verified, not
environment-sensitive. The residual weakness is one shape: **assertions whose expected
value is supplied by something other than the code under test**.

- **`shell.test.tsx:83-93`** — high: absence assertions on `status-text`/`status-dot`
  testids that no production code renders (grep-confirmed) — an entire test with zero
  fail-capability; "in any mode" unexercised (`mode` stays null).
- **Page-meta bare-digit cluster** — high: `FilePage.test.tsx:754` (sole assertion of its
  test), `:606-607`, `DirectoryPage.test.tsx:136`, `RunsPage.test.tsx:136,:164` —
  `toContain("2")` is satisfied by the embedded `generated_at` timestamp
  (`"2026-07-25 14:02 UTC"`), unconditionally. Assert the labelled fragment
  (`"2 covered"`).
- **`clock.test.tsx:55-59`** — high: "does not tick when the interval is unknown" renders
  `HealthTile`, which hard-codes `useNow(5000)` — the null-interval branch three
  production components depend on is never reached anywhere.
- **`seriestree.test.ts:31-37,:79-83`** — medium: slot-renumbering loops reduce to
  `expect(0).toBe(0)` on the one-series-per-chart fixture; the 3-series `chassis-a` tree
  in the same file would make them real.
- **`commands.test.tsx:98-101,:178-182`** — medium: asserts a *remounted* hook, not a
  re-render — deleting deps from the `useMemo` array stays green.
- **`linkinspector.test.tsx:61`** — medium: `provenance ?? "declared"` fallback equals the
  fixture value everywhere — dropping the read entirely stays green.
- **`reconnectingbanner.test.tsx:13-34`** — medium: three-arm connection union tested on
  two arms; the untested arm (`"connecting"`) is the store default and the reconnect
  state.
- **Title/behaviour mismatches ×7** — medium: incl. `shell.test.tsx:81` ("no backend
  fetches" — `App.tsx:38-40` unconditionally fetches `/api/mode`),
  `markcontrol.test.tsx:178` (neither "blank" nor "anchored" asserted),
  `health.test.ts:117` (20× past the boundary it names; real pin at `:210`),
  `CodeView.test.tsx` "in order" via order-blind `toContain`.
- **Loose bounds where the value is a known constant** — medium:
  `AppShell.test.tsx:321-322` (`< 40` where `MAX_OPTIONS = 8` ⇒ exactly 9),
  `seriestree.test.ts:97` (`> 10` where 41 is deterministic),
  `chartoptions.coloring.test.ts:24`.
- Low: fake-timer advances that bound but don't pin (`Toast.test.tsx` — dismiss between
  1200 and 3200ms both pass; `topology.livehealth`, `subjectpage.zoom`); store keys
  mutated without reset under shuffle (latent; `markcontrol`, `subjecthealthbanner`,
  `eventapi`, `eventeditor`); `topolayout.test.ts:122-124,:140-142`
  `expect(undefined).toBe(undefined)` on optional map lookups (the `colOf` fix exists two
  describes down); assorted noise assertions. Per-file coverage floor is 1% — catches only
  a literally-0% file, by the config's own analysis.

---

## 9. Cross-cutting themes

1. **A safety net whose own failure is silent is not a safety net.** The retry hook, the
   ambient-env pin, the registry-guard cache, the CliRunner shield, the coverage pre-init,
   the TS-coverage guard, and the chaos hygiene bracket all share it. Where a guard has a
   degraded mode, the degradation must be loud or pinned — several of these have "pin
   tests" that assert the guard's reach rather than its liveness.
2. **When a seam is duplicated, fixes reach one copy.** Readiness polls (server yes /
   suite no; helper yes / 10 raw loops no), gated retries (compose yes / nc no), logged
   skips (one enumeration path of three), compensating teardown (four sites yes / three
   no), FD watermarks (two correct / one drifted). Follow-up candidate: when fixing any
   such finding, grep for the seam's siblings before closing.
3. **Copied helpers lose the guard first.** All ten `run_otto` clones kept the obvious
   keys and dropped `-p no:tach`. Scar-encoding lines are exactly the ones a copier
   doesn't understand and omits — centralize helpers that carry scars.
4. **A test's premise needs its own assertion.** The premise-sleep family (§3.3), the
   loop-guarded family (§4.3), and the fixture-dependent guards (§4.1) all fail the same
   way: the setup silently stops producing the situation the test exists to check.
5. **Where the discipline is real, say so and stop.** Zero host-down skips, one strict
   xfail, the wedged-bed gate, `perf_budget.test.ts`, the compose retry, and the
   `errors.py` policy document are house-quality reference implementations — several
   findings above are one-line "make it match the good sibling" fixes.

---

## 10. Independently confirmed: interact-e2e ↔ shell-history race — **fixed in `bef943aa`**

*Correction (same day):* an earlier draft of this section attributed the in-tree fix to
an out-of-scope audit-agent edit. That was wrong. A **concurrent session** root-caused a
`make release` 3.14 failure to this exact race and landed the fix while this audit was
running — committed as `bef943aa` ("fix(tests): the interact e2e must lease the host
whose history it dirties"). Two independent routes converged on the same defect within
hours, which is itself evidence of how live it was. Recorded here for the finding's
content and for the residual follow-ups (§3.2's suppressed `stty size` expect and the
SIGWINCH sleep in the same file remain open):

**The race**: `otto host <id> login` is the human-facing bridge and deliberately does not
neutralize `HISTFILE` (a person's own login must keep recording history — see the
shell-history-suppression design). So every interact-e2e session appends what it types
(`echo otto_login_marker`, `stty size`) to test1's `~/.bash_history` at bash exit.
`test_shell_history_e2e` digests that exact file before and after its measurement window
to prove otto stays out of it, and the bed caps the file at `HISTFILESIZE` — one
concurrent append rotates lines and moves the sha256. The history test leases its host;
the interact tests do not, and living in their own `xdist_group` under
`-n auto --dist loadgroup` is precisely what lets the two run at once.

**Fix shape** (from the reverted patch, to be redone as its own reviewed change): a
class-scoped fixture holding `lease_unix_host(lock_dir, ["test1"])` for the interact
module (named-host lease, per the `test_docker_chaos.py` test3 idiom, since
`HOST_ID`/`HOST_NAME` are baked into banner assertions), plus deriving
`HOST_ID`/`HOST_NAME` from a single `ELEMENT` constant. Effort: S.

---

## Appendix A — xfail / skip / skipif inventory (complete)

| file:line | marker | reason | verdict |
|---|---|---|---|
| tests/unit/suite/test_timeout_enforcement.py:31 | xfail(strict=True) | pytest-timeout must abort this hung test | ok — strict; the only xfail in the tree |
| tests/unit/test_env_hermeticity.py:40 | skipif | probe for the subprocess pin below | ok |
| tests/unit/monitor/test_collector_db.py:188 | skipif not /proc | fd accounting needs Linux | ok |
| tests/e2e/monitor/dashboard/test_replay_soak.py:101 | skip | soak is chromium-only | ok (documented) — but see §3.7 on what the reason encodes |
| tests/unit/test_coverage_schema_preinit.py:164 | skip | coverage not active in this process | ok |
| tests/unit/test_tier_marker_invariants.py:175 | skip | "tests/e2e/chaos not created yet" | **suspicious** — stale; dir exists; a rename silently retires the guard |
| tests/unit/test_lab_data_hops.py:37 | skip | no embedded hosts | **suspicious** — §4.1; already firing for tech2 |
| tests/e2e/cov/test_embedded_coverage_e2e.py:58 | skip | build_dir not configured | **suspicious** — config absence retires the lane |
| tests/e2e/cov/test_embedded_coverage_e2e.py:168 | skip | product not built | **suspicious** — §4.5 |
| tests/repo3/tests/test_embedded_coverage.py:182 | skip | no embedded coverage hosts in active lab | **suspicious** |
| tests/integration/host/test_host_contract.py:140,:177,:225,:273,:325 | skip | no-FS / has-FS branches | ok — exhaustive declared-capability pairs |
| tests/integration/host/test_host_stability_contract.py:120,:189 | skip | no-FS branches | ok |
| tests/unit/bootstrap/test_bootstrap.py:142,:143,:271 | skip (in generated fixture source) | simulated repos | ok — input data, not tests |
| tests/unit/config/test_completion_cache_unit.py:1373 | importorskip (fixture text) | optional-dep simulation | ok |
| tests/repo1/tests/test_device.py:75 | skip | opt-out flag | ok — fixture SUT repo |

Web suite: zero skip/todo/fails/only, zero `skipIf`/`runIf`, zero snapshot tests, zero
retries, zero real-time sleeps.

## Appendix B — conftest hooks that can change a test's outcome

| Hook | File:line | Effect |
|---|---|---|
| pytest_runtest_call (wrapper) | tests/conftest.py:194 | `retry(n)`: re-runs body, `force_result(None)` — converts real failure into pass |
| pytest_collection_modifyitems | tests/conftest.py:77 | stable-sorts heavy xdist groups first (dispatch order only) |
| pytest_collection_finish | tests/conftest.py:232 | coverage schema pre-init; failure swallowed and stashed |
| pytest_runtest_teardown (wrapper) | tests/conftest.py:421 | reaps orphaned loops; raises LeakedProductLoopError — can fail a passing test |
| pytest_configure/unconfigure | tests/conftest.py:226/:296 | chained SIGINT faulthandler; permanent stdlib asyncio monkeypatches |
| pytest_collection_modifyitems (tryfirst) | tests/e2e/conftest.py:76 | stamps e2e marker + browser groups; raises UsageError on violation (§5.3 — crash path) |
| pytest_collection_modifyitems | tests/integration/conftest.py:15 | stamps `integration` by path (changes `-m` selection) |
| pytest_collection_modifyitems | tests/integration/host/conftest.py:164 | per-device xdist groups (worker placement) |
| pytest_runtest_setup | tests/integration/host/conftest.py:275 | pytest.fail for backends marked wedged this run |
| pytest_runtest_makereport (wrapper) | tests/integration/host/conftest.py:302 | marks backend wedged on "shell never became ready" substring |
| pytest_configure | tests/e2e/monitor/dashboard/conftest.py:91 | pytest.exit(1) on missing/stale dashboard dist |
| pytest_configure | tests/e2e/cov/report_browser/conftest.py:60 | pytest.exit(1) on missing/stale covapp bundle |

Autouse fixtures that can fail a test at teardown: `_pageerror_guard`
(report_browser/conftest.py:95), `_ts_coverage` zero-match RuntimeError
(_ts_coverage.py:74), `_bed_hygiene_bracket` (e2e/chaos/conftest.py:81), three
`_fd_watermark` copies.

## Appendix C — nox lane coverage map

Defaults: `lint`, `tests_hostless` (the CI gate: unit + e2e, not
integration/embedded/stability/browser, cov-fail-under 90), `typecheck`, `docs`.
Non-default: `tests_unit`, `tests_integration`, `tests_unix`, `tests_embedded`,
`tests_all` (cov 92), `tests_unit_repeat` (CI job; clears addopts — drops `-p no:tach`),
`chaos`, `chaos_embedded` (opt-in, bed-hostile), `dashboard[chromium|firefox|webkit]`
(CI matrix; never sets `OTTO_TS_COVERAGE` — §5.4). Never in any default lane:
integration, embedded, browser, stability, chaos, soak. Real signal-handler installation
is exercised only in nightly tier-2 chaos (§5.6).
