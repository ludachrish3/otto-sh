# Test-infrastructure remediation plan — gates first, then burn-down

> **For agentic workers:** execute wave-by-wave via superpowers:subagent-driven-development
> or superpowers:executing-plans. Waves use checkbox syntax. **One wave = one squash
> commit onto main; full gates (`make coverage` + `nox -s lint`) green before every
> squash; opus-max review per wave; Chris pushes. `git diff main <branch-tip>` must be
> empty after each squash.**

**Goal:** Convert the 2026-08-06 testing-infrastructure review
([test-infra-review-2026-08-06.md](test-infra-review-2026-08-06.md)) into (1) codified,
live-testable gates that make each defect class unwritable, and (2) a burn-down of the
existing findings, ordered so the gates land first and drive the fixes.

**Architecture:** Three enforcement mechanisms, all already in the repo: **ast-grep
rules** (`.ast-grep/rules/`, run by `make lint-arch` / `nox -s lint`) for pattern rules;
**ruff config** (`.ruff.toml`, `select=["ALL"]`) for rules ruff can express; **meta-guard
tests** (the `test_tier_marker_invariants.py` / `test_tuple_return_debt.py` /
`import_budget` family) for structural invariants that need an AST walk or a subprocess
probe. The churn review's law — *"every line otto holds by automation has held; every
line held only by prose has decayed"* — is the design brief.

**Tech stack:** ast-grep (sgconfig.yml → `.ast-grep/rules/`), ruff ≥0.16
(flake8-pytest-style), pytest meta-guards under `tests/unit/`, nox lanes, Makefile.

## Global constraints

- **NEVER `tach sync`**; tach.toml is a hand-maintained ratchet.
- New ast-grep rules follow the house format (see `typer-exit-outside-cli.yml`): `id`,
  `severity`, `message`, `note:` with baseline count + date, `files:`/`ignores:` globs.
- **Every new gate must be proven red against its motivating defect** before the fix
  lands (mutate or check out the pre-fix tree and run the gate). A gate that was never
  red is a guard that cannot fail — the review's #1 recurring defect class.
- **Every new meta-guard scanner ships with an embedded positive control**: the scanner
  runs against an inline bad-example source string and must flag it. This is how an
  enumeration-based guard stays falsifiable (counters the "enumerations inherit their
  omissions" failure mode).
- **Fix-with-gate in one squash** (Chris, 2026-08-06, final ruling): each squash commit
  carries a gate *together with* its site fixes — the gate enters at `severity: error`
  and green because its red inventory is cleared in the same commit. Multi-sub-wave
  burn-downs (G6, G10) enter at `severity: warning` with their first sub-wave and are
  promoted to `error` by the sub-wave that clears the last site; no warning-severity
  rule survives the end of the program. G1 is the one error-with-ignore exception (a
  single commented ignore entry deleted by Wave 2).
- **Prove-red protocol, per squash:** before the fixes are applied, run the new gate
  against the pre-fix tree (stash the fixes, or run from the parent commit) and record
  the hit count in the squash's commit message — that number is the gate's t0
  scoreboard entry, and a gate that was never observed red does not land.
- Suppressions must justify themselves inline (house rule: a suppression that drops the
  count is a failure). No `noqa`/`ignores` entry without a comment naming why.
- Commit messages via `git commit -F <file>`, never `-m`.
- Docs: each new gate gets a row/entry on the architecture-gates docs page (added in
  `10ef1866`).

---

## Commit sequence (ruled by Chris, 2026-08-06 — fix-with-gate; supersedes the interim single-squash-Part-A ruling)

1. **Commit 1 — `docs(spec)`:** both planning docs — `test-infra-review-2026-08-06.md`
   (the spec of record; every wave cites its file:line inventories) and this plan (its
   execution schedule). One logical unit; the review is the higher-level document.
2. **Commits 2..N — one squash per Part B wave, in order.** Each squash carries its
   gate(s) *and* their fixes together, per the fix-with-gate constraint above. The
   per-gate "Landing" lines in Part A name which wave carries which gate.
3. **Part C — its own `docs(quality)` commit** (the five principles as the gates-page
   preamble). Can land any time after Wave 0; the principles reference gates by id, not
   by state.

---

## Part A — Codified policies (the gates)

Sixteen gates, G0–G15. Each entry: the policy in one sentence, the mechanism, the red
inventory on today's tree (the burn-down list Chris asked for), and landing mode.

### G0. Gate infrastructure: ast-grep must see the tests

**Policy:** test code is subject to pattern gates, same as product code.
**Mechanism:** widen the scan target in both call sites:
- `Makefile:751`: `ast-grep scan src/otto web/src` → `ast-grep scan src/otto web/src tests`
- `noxfile.py:360`: same change.
Every tests-scoped rule carries `ignores: ["tests/repo1/**", "tests/repo2/**",
"tests/repo3/**", "tests/repo_broken/**", "tests/repo_e2e/**", "tests/firmware/**"]` —
fixture SUT repos are input data, not otto's tests.
**Red today:** none by itself (no tests-scoped rules exist yet).
**Landing:** Wave 0, first — every tests-scoped rule depends on it. Verify:
`uv run --group lint ast-grep scan tests` exits 0 before any tests-scoped rule lands.

### G1. `@pytest.mark.retry` is banned in otto's own suite

**Policy:** otto's own tests never retry; retry is a shipped product feature for otto
*users* (`tests/repo1` demonstrates it), not a flake-management tool for this repo.
**Mechanism:** `.ast-grep/rules/no-retry-marker-in-otto-tests.yml`:

```yaml
id: no-retry-marker-in-otto-tests
language: python
severity: error
message: otto's own tests must not retry — fix the flake or the product. retry(n) is a shipped feature for otto users; tests/repo1 is its only sanctioned home here.
note: >
  Baseline 2 violations as of 2026-08-06, both in
  tests/integration/host/test_hop_integration.py, both shielding the
  documented hop-nc hang (todo/hop_nc_transfer_flake.md). The ignores entry
  below is that file ONLY and is deleted by the wave that fixes the hang.
files:
- tests/**
ignores:
- tests/repo1/**   # user-facing sample; retry demo is the point
- tests/repo2/**
- tests/repo3/**
- tests/repo_broken/**
- tests/repo_e2e/**
- tests/integration/host/test_hop_integration.py  # pending hang fix (Wave 2) — do not add siblings
rule:
  pattern: pytest.mark.retry($$$N)
```

**Red today:** 2 (the hop tests) — held by the temporary ignore, which Wave 2 deletes.
**Landing:** Wave 1 (error severity immediately; the ignore is the ratchet).

### G2. Any retry mechanism must record its reruns and re-arm its timeout

**Policy:** a retry that erases the failed attempts from the report, or runs attempts
without the per-test timeout, is flake *concealment*, not flake management.
**Mechanism:** not lint — pinned meta-tests for the (single, shared) retry
implementation, in `tests/unit/suite/test_retry_semantics.py` (new). Subprocess-pytest
pins (the `test_env_hermeticity.py` idiom):
1. a fail-fail-pass `retry(3)` test run under `--junitxml` must leave rerun evidence
   (user_properties `retry_attempts=2` on the testcase, plus a terminal-summary line);
2. a `retry(2)` test whose body sleeps past `@pytest.mark.timeout(1)` must fail in
   bounded time on *every* attempt (total wall clock < 2 × timeout + slack) — pins the
   re-armed alarm;
3. a `retry(2)` test that passes on attempt 1 runs its body exactly once (pins the
   double-run bug); a body counter written to a tmp file, asserted `== 1`;
4. a retried body calling `pytest.fail()` is retried, not escaped (pins the
   `except Exception` vs `_pytest.outcomes.Failed` bug).
**Red today:** all four fail against both current implementations (that is the point).
**Landing:** Wave 1, with the implementation fix.

### G3. `pytest.raises(typer.Exit)` must bind and assert the exit code

**Policy:** `typer.Exit()` defaults to code 0; a raises-check without an exit-code
assert can pass on "blocked with a success code".
**Mechanism:** `.ast-grep/rules/typer-exit-raises-must-assert-code.yml`:

```yaml
id: typer-exit-raises-must-assert-code
language: python
severity: error
message: bind the excinfo and assert exit_code — typer.Exit() defaults to 0, so this passes on "failed successfully". `with pytest.raises(typer.Exit) as excinfo:` then `assert excinfo.value.exit_code == N`.
note: >
  Baseline 6 violations as of 2026-08-06 (test_bootstrap_gate.py:35,
  test_monitor.py:299, test_leaf_render.py:225/:239,
  test_dynamic_host_commands.py:154, test_monitor_cli.py:172). Fixed in the
  same wave; zero suppressions expected — 15 of 21 sites already did this
  right before the gate existed.
files:
- tests/**
ignores: [tests/repo1/**, tests/repo2/**, tests/repo3/**, tests/repo_broken/**, tests/repo_e2e/**, tests/firmware/**]
rule:
  pattern: |
    with pytest.raises(typer.Exit):
        $$$BODY
```

(The `as $VAR` form has a different AST shape and does not match — exactly the
discrimination wanted.)
**Red today:** 6. **Landing:** Wave 5a, fix-with-gate.

### G4. `pytest.raises(ValidationError)` requires `match=`

**Policy:** with `extra='forbid'` models, *any* validation error satisfies a bare
raises — baseline-dict drift silently retargets the test.
**Mechanism:** ruff, `.ruff.toml` — add (PT011 is already selected via `ALL`):

```toml
[lint.flake8-pytest-style]
# NOTE: this setting REPLACES the default list — the stdlib entries below are
# ruff's defaults, copied verbatim, then extended. Verify against the installed
# ruff's docs when landing.
raises-require-match-for = [
    "BaseException", "Exception", "ValueError", "OSError",
    "IOError", "EnvironmentError", "socket.error",
    "pydantic.ValidationError", "ValidationError",
]
```

**Red today:** 52 sites (inventory in review §4.4).
**Landing:** Wave 5b, fix-with-gate in one mechanical squash (each site gains a
`match=` naming the field/constraint under test — this is what converts them from
"model rejects something" to "model rejects *this*").

### G5. Probe results must be status-checked before their value is read (chaos oracles)

**Policy:** an oracle that reads `.value` from a live-bed probe without checking
`.status` reports "clean" whenever the probe itself fails — make the wrong thing
unwritable at the helper.
**Mechanism (contract, not lint):**
1. `tests/e2e/chaos/_bed.py::run_probe` raises (host-named, status-quoted) on non-ok
   results instead of returning them; callers keep only the happy-path read.
2. `tests/e2e/chaos/bed_hygiene.py::snapshot_host` asserts each probe's `is_ok` and
   raises naming the host + probe on failure.
3. Falsifiability pin (`tests/e2e/chaos/test_bed_oracle_honesty.py`, hostless-runnable
   with a stub host): a probe returning `status=Error, value="Command timed out"` must
   make `run_probe` raise and must make the hygiene bracket FAIL, not report clean.
   (This is the "prove the guard red" requirement made permanent.)
**Red today:** `_bed.py:140` oracle + hygiene bracket (review §3.1) — pin 3 fails
against the current code. **Landing:** Wave 3.

### G6. No hand-rolled deadline polls — `wait_for` is the only spelling

**Policy:** poll-until-deadline has exactly one implementation
(`otto.utils.wait_for` / `wait_for_async`); 26 hand-rolled copies in three incompatible
shapes is how the sleep-first and silent-expiry variants were born.
**Mechanism:** `.ast-grep/rules/no-handrolled-deadline-poll.yml`:

```yaml
id: no-handrolled-deadline-poll
language: python
severity: warning   # promoted to error by Wave 7c after migration
message: hand-rolled deadline poll — use otto.utils.wait_for / wait_for_async (probe_first, on_timeout are parameters, not re-implementations).
note: >
  Baseline 26 sites as of 2026-08-06 across src/otto and tests (review §7.3).
  Promote to error when the count is 0; the helper module itself is the only
  sanctioned ignore.
files: [src/otto/**, tests/**]
ignores: [src/otto/utils.py, tests/repo1/**, tests/repo2/**, tests/repo3/**, tests/repo_broken/**, tests/repo_e2e/**, tests/firmware/**]
rule:
  any:
  - pattern: deadline = time.monotonic() + $$$REST
  - pattern: while time.monotonic() < $DEADLINE
```

**Red today:** 26. **Landing:** Wave 7a lands helper + rule (warning); 7b/7c migrate and
promote.

### G7. No raw `while not X.started` readiness polls

**Policy:** polling a started flag without a `task.done()` check converts a startup
failure into an infinite hang; after Wave 8 the flag is unnecessary everywhere.
**Mechanism:** `.ast-grep/rules/no-raw-started-poll.yml` — `severity: error`,
`files: [src/otto/**, tests/**]` (usual fixture ignores),

```yaml
rule:
  pattern: |
    while not $SERVER.started:
        $$$BODY
```

**Red today:** 11 (suite/suite.py:538 + 10 test sites, review §3.5/§6.1).
**Landing:** Wave 8, fix-with-gate (the fix removes the pattern's reason to exist).

### G8. The otto-subprocess env dance lives in exactly one module

**Policy:** any test env carrying `COVERAGE_PROCESS_START` outside the canonical
helper is a fork of `run_otto` — and forks drop the scar-encoding keys first
(all ten dropped `-p no:tach`, #193).
**Mechanism:** `.ast-grep/rules/otto-subprocess-env-through-helper.yml` —
`severity: error`, `files: [tests/**]`, ignores = fixture repos +
`tests/e2e/_otto_subprocess.py` + `tests/_coverage_bootstrap/**` +
`tests/_fixtures/_coverage_preinit.py`,

```yaml
rule:
  pattern: '"COVERAGE_PROCESS_START": $VALUE'
```

**Red today:** 10 copies (review §7.1). **Landing:** Wave 6, fix-with-gate.

### G9. No `Path(__file__).parents[N]` arithmetic in tests

**Policy:** depth arithmetic breaks silently when a file moves;
`tests/_fixtures/paths.py` exports `TESTS_ROOT` / `PROJECT_ROOT`, `labdata.py` exports
lab-data paths — import them.
**Mechanism:** `.ast-grep/rules/no-parents-arithmetic-in-tests.yml` —
`severity: error`, `files: [tests/**]`, ignores = fixture repos +
`tests/_fixtures/paths.py` + `tests/_fixtures/labdata.py`,

```yaml
rule:
  any:
  - pattern: Path(__file__).parents[$N]
  - pattern: Path(__file__).resolve().parents[$N]
```

**Red today:** ~49 sites incl. 3 direct bypasses of `lab_data_path`.
**Landing:** Wave 6 (with the `TESTS_ROOT`/`PROJECT_ROOT` export; mechanical migration).

### G10. Library modules raise domain errors, not bare `RuntimeError`

**Policy:** a library consumer must be able to `except OttoError`; link/tunnel/docker/
transfer currently signal three different conditions through one stdlib type
(review §6.2).
**Mechanism:** `.ast-grep/rules/no-bare-runtimeerror-in-libraries.yml` —
`severity: warning` until Wave 10 completes, then error; `files:
[src/otto/link/**, src/otto/tunnel/**, src/otto/docker/**, src/otto/host/transfer/**]`,

```yaml
rule:
  pattern: raise RuntimeError($$$ARGS)
```

Companion count-ratchet while warning-severity: extend the
`test_tuple_return_debt.py`-style guard with per-module baselines
(link 10, tunnel 8, transfer 6, docker 12) that may only decrease. Scanner carries an
embedded positive control (inline source with a `raise RuntimeError` must be flagged).
**Red today:** ~36 in scoped dirs. **Landing:** rule Wave 10a; promotion Wave 10c.

### G11. Ambient-env hermeticity pin covers every conftest chain

**Policy:** an `os.environ` write at conftest import time is invisible to monkeypatch
and to a pin that only collects `tests/unit` — the pin must exercise each top-level
tree, and module-scope env writes in test infrastructure are banned outside the root
conftest's sanctioned block.
**Mechanism (two parts):**
1. Widen `test_probe_ambient_otto_env_is_stripped`: parametrize the inner subprocess
   over one collect-only target per tree — `tests/unit/<file>`,
   `tests/integration/<file>`, `tests/e2e/<file>` — so any conftest-chain injection
   (like `ensure_sut_dirs()`) is visible to the probe. *Proven red first*: before the
   Wave-4 fix, the integration-target parametrization must FAIL on `OTTO_SUT_DIRS`.
2. New meta-guard `tests/unit/test_conftest_env_writes.py`: AST-walk every
   `tests/**/conftest.py` + `tests/_fixtures/*.py`; any module-level (non-function)
   `os.environ[...]`/`setdefault`/`putenv` outside the root conftest's registered block
   fails, naming the file. AST, not regex — a quoted `os.environ` in a docstring must
   not count (the quoted-annotation lesson). Embedded positive control included.
**Red today:** `tests/integration/conftest.py:32` (and the `NO_COLOR`/`TERM` block at
root — which is *sanctioned* and allowlisted with a comment, or moved to
`pytest_configure` in the same wave; decide in-wave, default = move it).
**Landing:** Wave 4.

### G12. Skip policy: lanes that must never skip, pinned

**Policy:** chaos and embedded-coverage e2e lanes fail loud or pass — a skip in those
trees is a retired lane hiding behind green (house host-down rule, extended to
build/config absence).
**Mechanism:** meta-test `tests/unit/test_no_skip_lanes.py`: AST-scan
`tests/e2e/chaos/**` and `tests/e2e/cov/**` for `pytest.skip` / `pytest.mark.skip` /
`skipif` — assert zero. (chaos is already zero — the pin preserves it; cov has 2 sites
converted to hard-fail in the same wave.) Embedded positive control included.
**Red today:** 2 (`test_embedded_coverage_e2e.py:58,:168`; plus
`tests/repo3/tests/test_embedded_coverage.py:182` — repo3 is fixture data, out of
scope, but the *runner* of that lane gains a config-presence hard-fail).
**Landing:** Wave 13 (with the skip→fail conversions and the stale
`test_tier_marker_invariants.py:175` dir-check fixed to `assert chaos_dir.is_dir()`).

### G13. Lane truthfulness: addopts overrides keep the tach guard; CI arms TS coverage

**Policy:** an `-o addopts=` override that drops `-p no:tach` re-opens #193; a
browser lane that never sets `OTTO_TS_COVERAGE` runs a drift guard that cannot fire.
**Mechanism** *(dashboard leg revised at implementation time — the Makefile documents
that `OTTO_TS_COVERAGE` is make-only BY DESIGN: ad-hoc/nox runs must not append raw
coverage dumps outside make's rm+stamp protocol, so arming it in the nox session would
contradict a written rationale. The drift check is decoupled from collection instead):*
1. Fix: `noxfile.py:158-167` (`tests_unit_repeat`), `noxfile.py:378` (`docs`),
   `Makefile:902` (`doctest-src`) — every cleared addopts re-includes `-p no:tach`.
2. Drift guard: extract the bundle predicate to `tests/_fixtures/_ts_bundle_filter.py`
   (shared with `collect_ts_coverage`, so guard and filter cannot diverge); both browser
   conftests' `pytest_configure` dist-gates gain a `bundle_filter_drift_reason` check —
   it runs in EVERY lane that collects those suites (make, nox, CI), arms nothing, and
   `pytest.exit`s with a named message on a vite output-layout change. Falsifiability
   pins in `tests/unit/test_ts_bundle_filter.py` (drifted-layout control observed red).
3. Pin: meta-test `tests/unit/test_lane_invariants.py` scans `noxfile.py` + `Makefile`
   text: every occurrence of `addopts=` in an override must contain `-p no:tach`.
   (Text-scan of build files is the established `webassets`-guard idiom; embedded
   positive control included.)
**Red today:** 3 addopts sites + a CI-blind drift guard. **Landing:** Wave 0 (small,
self-contained, immediately testable).

### G14. Web: a queried testid must exist in the source; no bare-digit textContent asserts

**Policy:** an absence assertion against a testid nothing renders is permanently green;
a `toContain("2")` against text containing a timestamp is unconditionally true.
**Mechanism:**
1. New vitest meta-test `web/src/__tests__/testid_integrity.test.ts`: glob all test
   files, extract literals passed to `getByTestId`/`queryByTestId`/`findByTestId`
   (regex over source is fine here — it *should* also see quoted/commented ids? No:
   parse only call-argument literals), assert each appears as `data-testid=` (or
   `data-testid={`…) somewhere under `web/src` excluding test files. Positive control:
   an inline source snippet querying `definitely-not-rendered` must be flagged by the
   extractor.
2. `.ast-grep/rules/no-bare-digit-textcontent.yml` (language: typescript,
   `files: [web/src/**/*.test.ts, web/src/**/*.test.tsx, web/src/__tests__/**]`):

```yaml
rule:
  pattern: expect($EL.textContent).toContain($S)
constraints:
  S: { regex: '^"\d{1,2}"$' }
message: bare digits match the embedded report timestamp — assert the labelled fragment ("2 covered", "4 contexts").
severity: error
```

**Red today:** 2 testids (`status-text`, `status-dot`) + 5 bare-digit sites.
**Landing:** Wave 15, fix-with-gate.

### G15. No awaited remote cleanup in a bare `finally`

**Policy:** cleanup I/O in a bare `finally` replaces the primary exception with
transport noise — use `_teardown_step` / `lifecycle.compensate`.
**Mechanism:** `.ast-grep/rules/no-awaited-exec-in-finally.yml` — `severity: error`,
`files: [src/otto/**]`, ignores `src/otto/host/connections.py` (the helper's home) —
relational rule:

```yaml
rule:
  pattern: await $X.exec($$$ARGS)
  inside:
    kind: finally_clause
    stopBy: end
```

plus a second `any:` arm for `await $X.close($$$)` at `severity: warning` (legitimate
direct closes exist; the warning arm is a review prompt, not a ban — do not promote).
Verify the `_teardown_step`/`compensate` call sites do NOT match (they wrap the await
in a `with`/function call — confirm against `compose.py:542` before landing; if the
grammar match is unstable, narrow to the `.exec` arm only).
**Red today:** 2 error (`docker_host.py:523,:600`) + 1 warning (`unix_host.py:583`).
**Landing:** Wave 12, fix-with-gate for the error arm.

### Deliberately NOT gated (and why)

- **Non-empty preconditions for loop-guarded asserts** (review §4.3): a general gate
  needs whole-function dataflow; an annotation-based gate inherits the quoted-annotation
  blind spot. The 5 at-risk sites get one-line `assert <collection>` fixes (Wave 5a) and
  the pattern goes to the review checklist. Revisit only if it recurs.
- **Premise-establishing sleeps** (§3.3/§3.4): `time.sleep`/`asyncio.sleep` in tests has
  legitimate uses (injected chaos, workload simulation); a ban would breed suppressions
  that stop being read. The 65-site MockSession family gets the `_feed_after_ready()`
  helper (Wave 16); G6 removes the deadline-poll subfamily; the rest is review
  discipline.
- **Weakened floors** (`>= 2` where 4 is knowable, §3.6): not expressible without
  knowing the knowable value. Fixed in Wave 16; checklist item.
- **`extra='forbid'` respelling, help-string triplication, to_runtime mirrors** (§7.6):
  real, but library-shape work — folded into their remediation waves; gates would
  outnumber the sites.

---

## Part B — Remediation waves

Ordering: gates and their fix-with-gate waves first (0–8), then the M-sized library and
taxonomy work (9–12), then the long tail (13–17). Waves 0–8 are each S unless noted.
Every wave: branch → implement → prove new guards red against pre-fix code (checkout or
mutate) → full gates (`make coverage` ~5 min, `nox -s lint`, `make lint-arch`) → opus-max
review → squash to main → empty-diff check → hand to Chris.

### Wave 0 — Gate infrastructure + lane truthfulness (G0 + G13)
**Files:** `Makefile:751,:902`, `noxfile.py:158-167,:360,:378`, new
`tests/unit/test_lane_invariants.py`, `sgconfig.yml` (comment update).
- [x] Widen ast-grep scan targets to include `tests` (both call sites); run scan — must
      stay green (no tests-scoped rules yet).
- [x] Write `test_lane_invariants.py` (with embedded positive control); run it against
      the un-fixed noxfile/Makefile — all four legs must FAIL (record counts for the
      commit message).
- [x] Re-add `-p no:tach` to the three cleared-addopts lanes; guard now green.
- [x] Land the bundle-filter drift twin (G13 mechanism 2): `_ts_bundle_filter.py`,
      both conftest configure checks, falsifiability pins. (Replaces the original
      "set OTTO_TS_COVERAGE in nox" idea — make-only is documented design.)
- [x] Confirm one CI dashboard run trips nothing and the configure log shows the
      suites collecting normally (the drift guard is silent when clean). *(Done via
      the local dashboard lane in the Wave 0 gates — the same configure path, clean;
      the CI leg re-confirms on Chris's push.)*

### Wave 1 — Retry overhaul (item 1; G1 + G2)
**Files:** `src/otto/suite/plugin.py:240-261`, `tests/conftest.py:194-223`, new
`src/otto/suite/_retry.py`, new `tests/unit/suite/test_retry_semantics.py`, new
`.ast-grep/rules/no-retry-marker-in-otto-tests.yml`, `tests/repo1/tests/test_device.py`
(docstring: when retry is legitimate), docs gate page.
**Design:** one shared implementation. `_retry.py` exposes `run_with_retry(item, n)` as
a proper hookwrapper-compatible helper that (a) re-arms a per-attempt `signal.alarm`
from the item's effective `timeout` marker/ini value, (b) appends
`("retry_attempts", k)` to `item.user_properties` (lands in JUnit properties) and
prints a terminal-summary count, (c) catches `BaseException` subtypes that represent
test failure (`Exception` + `_pytest.outcomes.Failed`) while letting
`Skipped`/`KeyboardInterrupt` escape, (d) is invoked from a **wrapper** hook in both
plugin.py and tests/conftest.py so pytest's default `runtest` never double-runs.
- [x] Write the four G2 meta-tests first; run them against the current implementations —
      all four must FAIL (this is the review's verification, reproduced as pins; record
      the failures for the commit message). *(Probe: 4 RED / skip-pin GREEN-by-design.)*
- [x] Implement `_retry.py`; rewire both hook sites to delegate; pins green.
- [x] G2 tests green; run the hop tests once under the new marker on the lab bed to
      confirm rerun evidence appears in JUnit (expected duration: minutes, live bed).
      *(Both passed first-attempt in 2.4s — no natural flake this run, so no rerun
      evidence to observe live; the JUnit/summary evidence path is pinned hermetically
      by `test_retry_semantics.py`, incl. an async-body pin since every real retry
      user is a pytest-asyncio test.)*
- [x] Land G1 rule (error, with the hop-file ignore + note pointing at Wave 2).
- [x] Rider (from Wave 0's review): re-state `-p no:tach` in the two PRODUCT
      `--override-ini addopts=` argv sites — `src/otto/suite/run.py:500` and
      `src/otto/config/repo.py:568` — otto's own in-process pytest sessions are the
      literal #193 trigger and this is a recorded live defect
      (`todo/churn-review-cheap-items-followups.md`, "otto test panics when tach is
      installed"). Extend `test_lane_invariants.py` with a src-side leg when fixing;
      its docstring already bounds today's pin to the two build files and names these
      two sites.

### Wave 2 — Fix the hop-nc hang; drop the retries (item 2)
**Files:** `src/otto/host/transfer/nc.py` (hop path), per
`todo/hop_nc_transfer_flake.md`'s investigation plan;
`tests/integration/host/test_hop_integration.py:357,:390`; delete the todo file; delete
G1's ignore entry. **Effort: M** (root-cause per the todo's plan: bounded `wait_for` on
the asyncssh channel with an asyncio-level deadline — do not paper with a longer
timeout; memory rule: confirm root cause before fixing).
- [ ] Reproduce the hang per the todo file's recipe (live bed; state expected duration
      up front).
- [ ] Fix; soak the two tests ×20 on the bed without the marker (integration lane,
      serial — no heavy parallel load on the dev VM).
- [ ] Remove `@pytest.mark.retry(3)` from both tests, delete
      `todo/hop_nc_transfer_flake.md` (its own exit criterion), delete the G1 ignore —
      gate now fully armed.

### Wave 3 — Probe-status honesty (item 3; G5)
**Files:** `tests/e2e/chaos/_bed.py:140` (+ `:90-91` narrow the except-continue),
`tests/e2e/chaos/bed_hygiene.py:57-59`, consumers
(`test_tunnel_link_chaos.py:351`, `test_reboot_chaos.py:62-73,:223,:230`,
`test_connection_drop.py:258-262`, `test_reboot_chaos.py:151-153`), new
`tests/e2e/chaos/test_bed_oracle_honesty.py`.
- [ ] Write the falsifiability pin (stub host, Error-status probe) — must FAIL against
      current `run_probe`/`snapshot_host` (record for the commit message).
- [ ] Make `run_probe` raise host-named on non-ok; `snapshot_host` asserts per-probe
      `is_ok`; update consumers (they get *simpler* — happy-path reads only); pin green.
- [ ] Hand-probe the chaos bed once (gate blind spot rule: the chaos lane is never in
      default gates — run `nox -s chaos` legs touched, with Chris's bed coordination).

### Wave 4 — Ambient hermeticity (item 4; G11)
**Files:** `tests/integration/conftest.py:32`, `tests/_fixtures/paths.py:33-35`,
`tests/conftest.py:112-134` (colour block + `AMBIENT_OPT_INS`),
`tests/unit/test_env_hermeticity.py`, new `tests/unit/test_conftest_env_writes.py`.
**Decision (default):** move `ensure_sut_dirs()` into a session-scoped autouse fixture
in the integration conftest that sets the var via `os.environ` + registers teardown —
*and* imports config lazily so the import-time singleton constraint is satisfied by
fixture ordering, not import order. If the `_repos` singleton makes that infeasible,
fall back to: keep the import-time write but declare it in `AMBIENT_OPT_INS` with a
comment, so the pin sees it and the two-lane config divergence becomes explicit.
Either way the *pin* is the deliverable; the write becomes declared or scoped.
- [ ] Widen the hermeticity pin (3-tree parametrization); confirm the integration leg
      FAILS pre-fix (proven red; record for the commit message).
- [ ] Apply the chosen fix; pin green in all three trees.
- [ ] Land `test_conftest_env_writes.py` (AST scan + positive control); move or
      allowlist-with-comment the root colour/TERM block.

### Wave 5a — Cheap weak-test fixes (item 5 first half; G3)
**Files:** the 7 precondition sites (`test_lab_health.py:37`, `test_lab_data_hops.py:36`
— skip→fail + non-empty assert, `test_listing.py:415`,
`test_gen_monitor_fixtures.py:121,:128,:246`, `test_tuple_return_debt.py:117`), the 6
typer.Exit sites, `test_cov.py:337,:350` (assert the error message via the mock),
`test_cli_registry.py:243` (FrozenInstanceError, drop noqa), new G3 rule file.
- [ ] One-line `assert <collection>` preconditions; `test_lab_data_hops` becomes
      fail-with-named-error when a tech has zero embedded hosts *and* the fixture is
      expected to have them (tech2's legitimate absence gets an explicit parametrize
      exclusion with a comment — not a skip).
- [ ] typer.Exit sites: bind + `assert excinfo.value.exit_code == <N>` (read each
      command to pin the *right* N — bootstrap gate is 1; usage errors are 2).
- [ ] Land G3 at error in this squash; prove red on the stashed pre-fix tree (6 hits).

### Wave 5b — ValidationError match= burn-down (item 5 second half; G4)
**Files:** `.ruff.toml` + the 52 sites (review §4.4 inventory).
- [ ] Add the `[lint.flake8-pytest-style]` block; run ruff — the emitted PT011 list is
      the burn-down inventory (record the count, expected ~52, for the commit message).
- [ ] Fix mechanically in the same squash, one `match=` per site naming the field/
      constraint under test. No `--fix` exists for this. **Effort: M** (mechanical but
      each match string requires reading the model).

### Wave 6 — Test-boilerplate consolidation, high-leverage pair (item 6; G8 + G9)
**Files:** `tests/e2e/_otto_subprocess.py` (add `extra_argv_prefix`, `cwd` params), the
10 copy sites (delete), `tests/_fixtures/paths.py` (export `TESTS_ROOT`,
`PROJECT_ROOT`), ~49 `parents[N]` sites, two new rule files.
- [ ] Extend `run_otto`; migrate the 10 modules; delete local `_otto_env`/`_run_otto`
      copies (~180 lines). The four `otto test`-spawning modules now carry `-p no:tach`
      for the first time — run `test_stability_e2e.py` once on the bed to confirm no
      #193 panic surfaces (it was latent, not hypothetical).
- [ ] Export roots; migrate `parents[N]` sites (mechanical).
- [ ] Land G8 + G9 at error in this squash; prove red on the stashed pre-fix tree
      (10 and ~49 hits).

### Wave 7 — `wait_for` primitive (item 7; G6) — a/b/c
**Files:** `src/otto/utils.py` (+ unit tests `tests/unit/test_utils_wait_for.py`), then
the 7 product sites, then the ~19 test sites; rule file in 7a.
**Interface (locked here):**
```python
def wait_for(predicate: Callable[[], bool], timeout: float, *, interval: float = 0.1,
             probe_first: bool = True, on_timeout: str | Callable[[], str]) -> None
async def wait_for_async(...)  # same shape; awaits predicate if it returns Awaitable
```
Raises `TimeoutError` with the rendered `on_timeout` message (mandatory — silent expiry
is the defect class; there is no return-False mode). `probe_first=False` covers the
do-while need documented at `unix_host.py:811` — port that comment into the helper's
docstring.
- [ ] 7a: helper + tests (incl. a fake-clock test for probe-first vs sleep-first
      semantics) + G6 lands at warning (prove: 26 hits on the pre-helper tree).
- [ ] 7b: product sites (host.py ×4, unix_host.py, nc.py ×2) — behavior-preserving;
      the `unix_host.py:811` do-while comment moves to the call site's `probe_first`
      argument.
- [ ] 7c: test sites; promote G6 to error; delete the warning note.

### Wave 8 — Readiness as an event (item 8; G7)
**Files:** `src/otto/monitor/server.py` (expose `await server.started_event.wait()` or
`async def start()` that raises on startup failure), `src/otto/suite/suite.py:538`,
`tests/unit/monitor/test_server.py` (10 sites → `_wait_started` or the new API;
`test_server_signals.py:27` fixed to raise), rule file.
- [ ] Add the awaitable-started API with a startup-failure path test (bad TLS pair —
      the exact `server.py:800` translation).
- [ ] Fix `suite.py:538` (deadline + `task.done()` → `task.result()`); fix the 10 test
      loops; land G7 at error in this squash (prove red pre-fix: 11 hits).

### Wave 9 — Interact-e2e residuals (item 9) — **lease already landed in `bef943aa`**
The host-lease fix (class-scoped `leased_carrot` via `lease_unix_host`, `ELEMENT`
single-sourcing) was independently root-caused from the `make release` 3.14 failure by a
concurrent session and is **committed** — see review §10 correction and the
`project_release_314_shell_history_race` memory topic. Remaining work in the same file
(review §3.2), foldable into Wave 16 if preferred:
- [ ] Un-suppress the `stty size` expect at `test_interact_e2e.py:239` — it is the only
      assertion that the resize reached the remote side; on timeout, fail naming the
      backend (ssh vs telnet parametrization).
- [ ] Replace the `time.sleep(0.3)` SIGWINCH settle with a bounded `wait_for` poll of
      the session log once Wave 7 lands (sequence after 7a).
- [ ] Same file `:107-113`: turn the expect-timeout-then-drain into a failure naming
      which of the two token occurrences was missing.

### Wave 10 — Error taxonomy (item 10; G10) — a/b/c, **Effort: M total**
- [ ] 10a: `gitio` split (`GitMissingError`/`NotAGitRepoError`/`GitCommandFailedError`
      under `GitUnavailableError`); convert the 3 string-match sites + `cli/cov.py:540`;
      land G10 rule at warning + count-ratchet guard.
- [ ] 10b: `link/` errors (`LinkHostUnreachableError`/`LinkCommandFailedError`, both
      `(OttoError, RuntimeError)` so existing catches keep working); fix the read path
      (`unreachable=True` only on unreachable; `_cancel_timers` distinguishes;
      `_ensure_not_foreign` → ValueError per the module's own convention). Ratchet:
      link baseline 10 → 0.
- [ ] 10c: shared `HostCommandError` + `run_or_raise(host, cmd)` helper; tunnel/docker/
      transfer adopt; promote G10 to error; retire the count-ratchet.

### Wave 11 — Registry-guard invalidation (item 11)
**Files:** `tests/conftest.py:1283-1312,:1441-1442`,
`tests/e2e/cli/test_registry_isolation_e2e.py`.
Replace the `len(sys.modules)` cache key with an identity-safe one: cache keyed on the
*set* of module names matching the scan filter (cheap frozenset compare), or drop the
cache and re-scan (measure first — if a scan is <5ms the cache is not paying for its
defect). Extend the e2e pin to assert discovery completeness: import a new module
defining a Registry mid-test while evicting another, assert the new registry is
snapshotted (this is the mutation that today's guard misses — proven red first).

### Wave 12 — Collection-crash + finally-await (items 12; G15)
**Files:** `tests/e2e/conftest.py:105-108` (defer: collect offenders in
`pytest_collection_modifyitems`, report + fail via a session-fixture/`pytest_configure`
-safe path per the dashboard conftest's documented pattern),
`src/otto/host/docker_host.py:523,:600`, `src/otto/host/unix_host.py:583` (adopt
`_teardown_step`/`compensate`); land G15 in this squash — `.exec` arm at error (prove
red pre-fix: 2 hits), `.close` arm at warning by design (never promoted).
- [ ] Reproduce the controller crash once with a deliberately mistagged e2e test under
      `-n 2` (scratch clone, not the dev repo) — then verify the deferred report names
      the offender instead.

### Wave 13 — Skip-policy pins (G12) + embedded-coverage hard-fail
Per G12, all in one squash: `test_no_skip_lanes.py` lands (proven red pre-fix on the 2
cov sites), the 2 skip→fail conversions in `test_embedded_coverage_e2e.py`, and the
tier-marker stale-skip fix.

### Wave 14 — Harness silent-degradation sweep (review §5.4–5.6 remainder)
**Files/items, one squash:** CliRunner shield (`tests/conftest.py:1247-1253`) — replace
`except ImportError: yield` with a hard fail naming the pytest version bump; coverage
pre-init (`tests/conftest.py:253-254`) — act on the stashed outcome: session-level
warning-or-fail when preinit reports False under `--cov`; FD-watermark consolidation
into `tests/_fixtures/fd_watermark.py` (gc-collect-before-baseline semantics, the
tunnel_stability variant wins) with the three conftests importing it; tunnel-reap
teardown logs suppressed exceptions (`tunnel_stability/conftest.py:61-68`); the two
default-reset autouse fixtures (`tests/conftest.py:543-580`) become snapshot-restore.

### Wave 15 — Web cannot-fail cluster (item 15; G14)
**Files:** the 5 bare-digit sites, `shell.test.tsx:83-93` (rewrite against rendered
reality or delete with the spec-decision note moved to the component), `clock.test.tsx`
(drive `useNow(null)` directly via a probe component), `seriestree.test.ts:31-37,:79-83`
(use the 3-series `chassis-a` tree), `commands.test.tsx` (assert the *same* rendered
hook re-renders), `linkinspector.test.tsx` (one fixture with `provenance: "measured"`),
`reconnectingbanner.test.tsx` (third arm); G14 lands in this squash — testid-integrity meta-test (proven red on the 2
phantom testids) + bare-digit rule at error (proven red on the 5 sites).
Gate `make lint-ts` + `make coverage-ts` (memory: bare pytest does not build web dist —
run `make web` first if touching components).

### Wave 16 — Timing-test hardening, unit tier (§3.3, §3.4, §3.6 residue)
`_feed_after_ready()` Event helper for the 65-site MockSession family (one helper in
the mock, mechanical migration); `test_session.py:482` gains a positive control
(assert the feed actually happened before asserting the verdict); the
`test_collector_run.py` floors move to exact counts on a virtual clock;
`test_connection_race.py` gains a contention positive-control; `test_console_lock.py`
asserts the readers reached the churn loop. **Effort: M.**

### Wave 17 — Test-fixture library build-out (items 16 + §7.4/§7.5 tail; **Effort: M–L**, divisible)
`tests/_fixtures/gitrepo.py` (hermetic `git_env` + `TmpGitRepo`; migrate 20 files);
`make_sut_repo()` (48 sites); `active_context` migration (28 sites);
`bare_host()` (11 factories); `write_lab()` (7 helpers). Each sub-item is
independently squashable; sequence by annoyance. DispatchRunner migration note from
review §7.4 applies: migrate the 11 bare-CliRunner files *before* (or with) any
cov.py async-leaf conversion.

### Not scheduled (tracked, deliberately deferred)
Review items with real value but lower leverage, left to opportunistic pickup:
`completion_cache` `_mutate_cache` + RMW race (§6.6 — schedule when completion work next
opens that file); CLI error-render consolidation (`usage_error`/`fail_from`, §7.6);
`to_runtime` mixin; TOML `load_toml`; typer `otto_group`; path-verb naming
(`store_key_root`); monitor-e2e silent poll; Playwright 60s-default responsiveness
assertion; `_host_pool` lease deadline; chaos §3.7 low items. Each is one squash when
picked up; none needs a gate beyond those above.

---

## Part C — Policy text (for the gates docs page)

Add these five sentences to the architecture-gates page as the section preamble — they
are the design principles the gates mechanize, quotable in review:

1. **A test may manage flakes only in ways that leave evidence.** Retries record their
   attempts; timeouts survive retries; the report never shows a clean pass for a dirty
   run.
2. **A guard's own failure must be loud.** Any fixture/hook/pin that protects the suite
   must fail the run (or a dedicated pin) when it degrades — `except ImportError: yield`
   around a safety net is a second bug waiting behind the first.
3. **An oracle checks its probe before it checks its property.** Status first, value
   second; a probe that can time out must not be able to satisfy a negative assertion.
4. **The premise of a test gets its own assertion.** If setup stops producing the
   situation under test, the test fails — it does not pass vacuously (non-empty
   preconditions, positive controls, contention checks).
5. **Scar tissue is centralized.** Any dance that encodes a past incident
   (`-p no:tach`, git hermeticity env, probe-first polling) lives in exactly one helper;
   copies are gate violations because copies drop the scar first.

---

## Self-review notes

- Every review triage item 1–16 maps to a wave (1→W1, 2→W2, 3→W3, 4→W4, 5→W5a/5b,
  6→W6, 7→W7, 8→W8, 9→W9 (residuals; lease landed in `bef943aa`), 10→W10, 11→W11,
  12→W12, 13→W13, 14→W0, 15→W15, 16→W17); §5.4–5.6 harness residue →W14; §3
  unit-timing residue →W16; the deferred list is explicit.
- Gates G1–G15 each name their red inventory on today's tree — the live test Chris
  asked for. Landing modes: fix-with-gate in the named wave's squash (G3, G4, G7, G8,
  G9, G12, G13, G14, G15), warning→error across sub-waves (G6, G10),
  error-with-shrinking-ignore (G1), contract+pin (G2, G5, G11).
- Commit packaging per Chris (final): docs commit (both planning docs) → one squash per
  wave, each carrying its gate(s) + fixes → Part C as its own docs commit.
- Two claims to verify at implementation time (flagged in place): ruff's current
  default `raises-require-match-for` list (G4), and ast-grep's `finally_clause` node
  name / relational-match stability for G15 (fall back to the `.exec`-only arm).
