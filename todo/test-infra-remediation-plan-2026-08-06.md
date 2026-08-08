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
  burn-downs (G10; originally also G6, whose a/b/c collapsed into one squash at
  execution time) enter at `severity: warning` with their first sub-wave and are
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

**Red today:** 0 — the two hop-test markers were removed by Wave 2 (hang root-caused), and the temporary ignore is deleted; the rule is fully armed.
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
  Baseline 7 violations as of 2026-08-06: the review's six
  (test_bootstrap_gate.py:35, test_monitor.py:299,
  test_leaf_render.py:225/:239, test_dynamic_host_commands.py:154,
  test_monitor_cli.py:172) plus test_main.py:592, which its inventory
  missed. Fixed in the same wave; zero suppressions expected — 15 of 22
  sites already did this right before the gate existed.
  [Wave 5a landing note: the statement-shaped pattern below is BLIND to
  parenthesized multi-item `with (...)` blocks — where two of the seven
  live — and a recount taken with that same blind pattern briefly
  "corrected" this baseline to 5. The landed rule matches
  `pytest.raises(typer.Exit)` inside a `with_item` instead (the `as` form
  nests under an as_pattern and falls out), and the rule-verified pre-fix
  count is 7. Never recount a spec with the instrument under test.]
files:
- tests/**
ignores: [tests/repo1/**, tests/repo2/**, tests/repo3/**, tests/repo_broken/**, tests/repo_e2e/**, tests/firmware/**]
rule:
  pattern: pytest.raises(typer.Exit)
  inside:
    kind: with_item
```

(The `as $VAR` form nests the call under an `as_pattern` — outside a bare
`with_item` — and does not match: exactly the discrimination wanted. The plan
originally spelled this as a statement-shaped `with pytest.raises(typer.Exit):`
pattern; see the landing note for why that shape is blind.)
**Red today:** 7 (rule-verified with the with_item form; see note). **Landing:**
Wave 5a, fix-with-gate.

### G4. `pytest.raises(ValidationError)` requires `match=`

**Policy:** with `extra='forbid'` models, *any* validation error satisfies a bare
raises — baseline-dict drift silently retargets the test.
**Mechanism:** ruff, `.ruff.toml` — add (PT011 is already selected via `ALL`).
*(Block below as originally proposed — SUPERSEDED by the landed form; see the
Red-today note: the landed config is the extend- form with a `*ValidationError`
glob, and the bare `"ValidationError"` entry below is documented-dead.)*

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

**Red today:** 71 sites, ruff-verified with the landed config (t0 on `b21c37a4`).
[Wave 5b corrections, in order: the review §4.4 inventory said 52 — a grep
undercount vs ruff's AST (multi-line and qualified forms); the pydantic-only
config measured 66; interim review then showed a bare short-name entry is DEAD
(PT011 compares resolved qualified names) and the landed
`raises-extend-require-match-for = ["*ValidationError"]` glob also catches
jsonschema's ValidationError and otto's own EventValidationError — 5 more bare
sites, 71 total. Heaviest files match the review's list; counts are higher.]
**Stated residual (found during the Wave 5b burn-down, NOT fixed there):** a
pre-existing `match=` that names only a bare field can be BLIND — pydantic's error
rendering echoes the input dict via `input_value=...`, so `match="tls_key"` passes
even when the model rejects for a completely different reason (proven by mutation
at `tests/unit/models/test_settings.py:625`; suspect siblings: `:632` and the
bare-field matches in `test_settings_coverage.py`). PT011 cannot see this (a match
is present). The robust form is `(?m)^<loc>\n\s+<reason>`, pinning both halves.
Goes to the review checklist; sweep the pre-existing bare-field matches in Wave 16
alongside the other semantic-strengthening work. Raises-shape escapes (verified,
latent — zero in-tree uses today): tuple arguments `raises((ValidationError, X))`
and variable indirection `exc = ValidationError; raises(exc)` are invisible to
PT011; alias imports, keyword form, attribute form, and locally-defined
`*ValidationError` classes ARE caught.
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
   ✅ DONE (Wave 3) — plus `probe_text` (checked exec+unwrap in one call), the single
   `check_probe_result` spelling shared with `snapshot_host`.
2. `tests/_fixtures/bed_hygiene.py::snapshot_host` (the plan's
   `tests/e2e/chaos/bed_hygiene.py` path was a drift) asserts each probe's `is_ok` and
   raises naming the host + probe on failure. ✅ DONE (Wave 3).
3. Falsifiability pin — landed as `tests/unit/test_bed_oracle_honesty.py`, NOT the
   planned `tests/e2e/chaos/` location: the chaos lane is opt-in-only, so a pin there
   would itself be a guard that never runs; under tests/unit it runs in every default
   gate. Stub-host probes through the REAL `probe_host` seam; each raising pin has an
   embedded positive control. ✅ DONE (Wave 3).
   (This is the "prove the guard red" requirement made permanent.)
**Red today: 0 — Wave 3 landed the contract.** t0 evidence: 4 defect demos passed
against pre-fix code (run_probe handed back Error results; snapshot_host parsed the
error text into empty/phantom fields; the hygiene bracket read dead probes on BOTH
sides as a clean bed; `veggies_link_id` swallowed arbitrary corruption), and the pin
file was collection-red. 9 targeted mutations each killed — including reintroducing
the wave's own motivating defect at a consumer, which the first cut of the
consumer-drift scan MISSED (named-factory shape) and which its mutation run then
fixed. **Landing:** Wave 3.

### G6. No hand-rolled deadline polls — `wait_for` is the only spelling

**Policy:** poll-until-deadline has exactly one implementation
(`otto.utils.wait_for` / `wait_for_async`); the hand-rolled copies in three incompatible
shapes is how the sleep-first and silent-expiry variants were born.
**Mechanism:** `.ast-grep/rules/no-handrolled-deadline-poll.yml`, landed at
`severity: error` (the a/b/c warning phasing collapsed into one fix-with-gate squash).
*Neither of the plan's proposed arms survived contact:* the bare-assignment arm
(`deadline = time.monotonic() + …`) over-matches host.py's two legitimate shared-budget
splitters (deadline arithmetic with no poll loop), and the bare while-arm does not even
parse (an incomplete statement — the same failure G8's proposed pattern had). The landed
rule has twenty arms in three families — whole-condition `while clock < deadline`
(both `time.monotonic()` and the `$C.time()` receiver shape, which covers `time.time()`,
`loop.time()` and `asyncio.get_running_loop().time()`, in BOTH operand orders — `a < b`
and `b > a` are different ASTs, and the reversed order is the first respelling an
annoyed author reaches for), the same loop in elapsed-vs-budget spelling (zero in-tree
uses; regression pins), and the do-while `if clock >= deadline` exit guard (the
motivating unix_host shape, both orders). The elapsed-**if** form is deliberately not
an arm: it is how cache-TTL freshness checks and work-loop budgets are legitimately
spelled. The note frames the arms as a tripwire for natural spellings, not an
exhaustive enumeration, and lists the known unmatched spellings with their verified
in-tree status (compound conditions, bare-name clock imports, perf_counter,
reversed-elapsed, count-based retries).

**Red today (rule-measured, landed arms):** 21 — not the review's 26. The corrected
migratable inventory is 21 sites (5 product + 16 test); the review's five others are not
predicate polls (host.py's two budget splitters, `_pty_driver.py`'s two
select-with-remaining expect loops, `test_lifecycle_sync_phase.py`'s blocking-readline
loop — the last inline-suppressed with the reason), and the rule's 21 hits are a
different composition: 20 arm-visible migrated sites + the suppressed readline loop,
while the 21st migrated site (`test_replay_soak`'s variable-bound quiescence poll) is
arm-invisible and was found by classifying every clock call in tests.
`src/otto/host/shell_liveness.py` is a sanctioned ignore alongside `utils.py`: its
`confirm_live` is the product's own fused probe-response primitive, paced by the
reply-wait timeout rather than an interval sleep. **Landing:** Wave 7, one squash.

### G7. No raw `while not X.started` readiness polls

**Policy:** polling a started flag without a `task.done()` check converts a startup
failure into an infinite hang; after Wave 8 the flag is unnecessary everywhere.
**Mechanism:** `.ast-grep/rules/no-raw-started-poll.yml` — `severity: error`,
`files: [src/otto/**, tests/**]` (usual fixture ignores), seven arms: the bare
attribute form plus six zero-in-tree respellings (parenthesized negation, call
spelling, `is False` / `== False` / `is not True`, and the drain-loop
`if $S.started: break` that hides the poll from any while-header arm).

**Red today (rule-measured, landed arms):** 14 — not the review's 11 (it missed
server.py's own internal poll, `test_server_signals.py`'s copy, and one
`test_server.py` site). Known unmatched: compound flag+other conditions (zero
in tree) and the dashboard harness's bounded cross-thread `wait_for` flag poll
(awaiting the asyncio event is not thread-safe from outside the loop).
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

**Red today:** 14 copy files, rule-measured on the pre-fix tree (review §7.1's 10 was
an undercount — it predated two files and missed the unit-lane python-probe and the
chaos driver). [Wave 6 note: the pattern as proposed below does not PARSE — a dict
pair is not a standalone Python expression; the landed rule uses the
context/selector form (`context: '{"COVERAGE_PROCESS_START": $VALUE}'`,
`selector: pair`).] **Landing:** Wave 6, fix-with-gate.

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

**Red today:** 67 sites across 58 files, rule-measured with the landed 8-arm rule
(the review estimated ~49; a grep said 47; the plan's two patterns measured 51; the
qualified `pathlib.` spelling added 1; `.parent.parent` chain arms added 15 — most
invisible to grep because ruff formats long chains across lines). Includes 3 direct
bypasses of `lab_data_path`.
**Landing:** Wave 6 (with the `TESTS_ROOT`/`PROJECT_ROOT` export; mechanical migration).

### G10. Library modules raise domain errors, not bare `RuntimeError`

**Policy:** a library consumer must be able to `except OttoError`; link/tunnel/docker/
transfer currently signal three different conditions through one stdlib type
(review §6.2).
**Mechanism:** `.ast-grep/rules/no-bare-runtimeerror-in-libraries.yml` —
`severity: error`; `files:
[src/otto/link/**, src/otto/tunnel/**, src/otto/docker/**, src/otto/host/transfer/**]`,

```yaml
rule:
  pattern: raise RuntimeError($$$ARGS)
```

The plan's staged promotion (warning first, then a per-module count-ratchet, then
error) was DROPPED at landing: the fix-with-gate ruling makes converting every site
part of the same squash, so the baseline is zero on the day the rule lands and a
ratchet has nothing left to ratchet. The positive control is a planted probe under
`src/otto/link/`, scanned and removed — paired with an out-of-scope probe under
`src/otto/host/` (non-transfer) that must NOT fire; both were run.
**Red at t0:** **37**, rule-measured on the pristine tree (link/manage 10,
tunnel/manage 8, tunnel/socat 1, docker/staging 6, docker/compose 6, transfer/nc 6).
The review's ~36 missed `tunnel/socat.py`'s `pick_free_port` — which is the argument
for measuring a baseline WITH the rule rather than by reading.
**Landing:** Wave 10, one squash, straight to error. **Now:** 0.

### G11. Ambient-env hermeticity pin covers every conftest chain

**Policy:** an `os.environ` write at conftest import time is invisible to monkeypatch
and to a pin that only collects `tests/unit` — the pin must exercise each top-level
tree, and module-scope env writes in test infrastructure are banned outside the root
conftest's sanctioned block.
**Mechanism (two parts):**
1. Per-tree collection pin (`test_conftest_chain_writes_no_ambient_env_at_collection`,
   a NEW parametrized pin rather than a widening of the runtime probe): the WHOLE
   TREE ROOT per tree (a conftest *chain* is per-file — the opus review's spy plugin
   showed a one-file target importing 3 of the suite's 14 conftests, and verified an
   escape through a deep conftest; tree-root `--collect-only` imports every module
   and thus every conftest, measured ~3.8s for all three legs), inner run from a
   polluted shell with the `tests._collect_env_probe` plugin asserting at
   `collection_finish` — the moment when every conftest import has happened but no
   session fixture has run, which is exactly the import-time/runtime boundary the
   gate draws. Marker line in stdout = anti-vacuity control; leak (rc 7 — clear of
   pytest's own exit codes; it was 3 at t0, which collides with INTERNAL_ERROR)
   distinguished from a broken inner run (any other rc). ✅ DONE (Wave 4); proven red
   first: the integration leg failed with `['OTTO_SUT_DIRS']`, rc=3, pre-fix.
2. Meta-guard `tests/unit/test_conftest_env_writes.py`: AST scan of every non-fixture
   `tests/**/conftest.py` + `tests/_fixtures/*.py`; module-scope (incl. class-body,
   `if`/`for`-nested) env writes flagged, import aliases resolved, and module-scope
   CALLS resolved one hop into scanned-set helpers (the live offender was exactly
   `ensure_sut_dirs()` — a call whose write lives in another file). Embedded positive
   controls for every banned and sanctioned shape. ✅ DONE (Wave 4); t0 red:
   `['tests/integration/conftest.py:32']` exactly.
**Fix landed:** the integration conftest's import-time `ensure_sut_dirs()` (whose
"config reads OTTO_SUT_DIRS at import time" justification had gone stale — all
readers are lazy) became a session-scoped autouse fixture (`_impl` pattern,
set-with-restore, pinned directly); `ensure_sut_dirs` deleted from
`tests/_fixtures/paths.py`. **In-wave decision:** the root `NO_COLOR`/`TERM` block
STAYS in the root conftest (allowlisted as the one sanctioned block) rather than
moving to `pytest_configure` — the block must precede the root conftest's own otto
imports (module-level rich/click Consoles bake colour at import), which
`pytest_configure` runs after.
**Red today: 0 — Wave 4 landed the contract.** 8 mutation/escape runs, all killed —
three only after forcing detector or pin fixes: an ALIASED reintroduction
(`import os as _os`) walked through the first-cut detector (caught by the collection
pin, then aliases became resolved, not stated); deleting the walker's descent
survived because a redundant inner `ast.walk` hid it (restructured to
single-descent, which also removed a nested-def false positive); and the opus
review's ALIAS-BY-ASSIGNMENT write (`_env = os.environ`) in a DEEP conftest walked
through BOTH first-cut gates (one-file pin targets never imported 11 of 14
conftests; the detector didn't track assignment aliases) — both fixed, the exact
escape now killed by both gates. Detector also gained `environb`/`__setitem__`/
annotated-assign/bare-`putenv` shapes with controls, and states its two
over-approximations. Coverage note: `make coverage` total dropped 95.89% → 95.69%
across Waves 3-4 — partly THIS wave de-faking coverage (unit tests no longer see
ambient `repo1` for part of the session, so incidental `otto/config` discovery
branches stopped executing); expected, not to be chased. **Landing:** Wave 4.

### G12. Skip policy: lanes that must never skip, pinned — LANDED (Wave 13)

**Policy:** chaos and embedded-coverage e2e lanes fail loud or pass — a skip in those
trees is a retired lane hiding behind green (house host-down rule, extended to
build/config absence).
**Mechanism, as landed:** meta-test `tests/unit/test_no_skip_lanes.py`: AST scan of
`tests/e2e/chaos/**` and `tests/e2e/cov/**` (rglob, subdirs included) for
`pytest.skip` / `pytest.importorskip` / `pytest.mark.skip` / `pytest.mark.skipif`
plus the bare `mark.skip` / `mark.skipif` usage-site arms (fable's condition:
they catch `from pytest import mark` and `mark = pytest.mark` rebindings), plus a
ban on `from pytest import skip|skipif|importorskip` (the alias alley that would
make bare `skip(...)` invisible to a dotted scan) — assert zero. Tripwire, not
proof system: five stated blind spots in docstring + gates row, grepped zero at
landing. Anti-vacuity:
an empty or moved lane FAILS the scan (`assert files`) — the same stale-skip shape
the tier-marker fix below retires. Embedded positive control asserts the detector
sees every spelling. repo3's own skip stays: fixture SUT data, the standard
tests-scoped carve-out; the *runner* is what hard-fails.
**Red measured:** 2 (`test_embedded_coverage_e2e.py:52,:152` — the plan's draft line
numbers 58/168 had drifted), both converted to `pytest.fail` naming the missing
config key / unbuilt artifact and where to fix it; chaos was already zero (the pin
preserves it). The runner PASSED live in the same day's gate run, so the fail
branches fire only where the lane is hollow.
**Also landed:** `test_tier_marker_invariants.py`'s stale `pytest.skip("tests/e2e/
chaos not created yet")` dir-guard → `assert chaos_dir.is_dir()` (the tree has
existed since chaos-hardening; the skip would have hidden a moved lane forever).
**Mutations:** planted chaos-lane skip red; conversion revert red; detector blinded
to mark forms → positive control red; lane path broken → vacuity assert red.

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

### G14. Web: a queried testid must exist in the source; no bare-digit textContent asserts — LANDED (Wave 15)

**Policy:** an absence assertion against a testid nothing renders is permanently green;
a `toContain("2")` against text containing a timestamp is unconditionally true.
**Mechanism, as landed:**
1. Vitest meta-test `web/src/__tests__/testid_integrity.test.ts`: extracts literals
   passed to `get/query/find(All)ByTestId` across all vitest files, and accepts an id
   if shipped source renders it — verbatim `data-testid`, ANY forwarded prop ending in
   `testId` (ui/Disclosure has both `testId` and `toggleTestId`), or a template prefix
   (`code-row-${...}` legitimizes `code-row-1`) — or if the referencing test file
   renders it ITSELF (harness probes; own file only, so one file's harness cannot
   legitimize another's phantom). Extraction is comment-stripped (a
   `data-testid="x"` in a comment must not legitimize `x` — two comment-sourced
   statics existed at adoption), the `querySelector('[data-testid="x"]')` form
   counts as a REFERENCE (and never as a render site), test helpers (testUtils,
   `__tests__/_synth`) are excluded from the global acceptance set, and lookalike
   props (`latestId=`) are rejected. Controls: synthetic phantoms through the
   real pipeline (both reference shapes), negative controls for
   comments/selectors/lookalikes, one known site per acceptance path, and corpus
   floors at ~half of measured actuals (77 files / ~900 refs). Accepted blind
   spots stated in the header: Playwright lane, `getAttribute("data-testid")`
   comparisons, same-line trailing comments.
2. `.ast-grep/rules/no-bare-digit-textcontent-{tsx,ts}.yml` (split pair, same reason
   as no-plan-coordinates-*: a `tsx` rule never sees .ts files). The receiver is
   `expect($A)` — ANY expression, not `$EL.textContent` — because 3 of the 17
   offenders bound the text first (`const meta = ...textContent ?? ""`), a form a
   receiver-shaped pattern permanently cannot fire on; the
   anchored 1-2-digit constraint is what keeps that width false-positive-free.

**Red at t0, instrument-measured (the plan's counts below were hand-estimates):**
4 phantom ids (`status-text`, `status-dot`, and — unknown to the review —
`menu-ticket-all`/`menu-ticket-PROJ-1` in AppShell.test.tsx, the 5c menu-removal
tombstone) + 17 bare-digit sites (RunsPage 3, DirectoryPage 2, FilePage 7,
TicketsPage 2, CodeView 3 — the review's list had 12; CodeView and TicketsPage were
outside its file enumeration).
**Landing:** Wave 15, fix-with-gate. Phantom-absence fixes pin behavior, not ids:
AppShell's menu test now asserts no row TEXT mentions a fixture ticket inside the
real open menu (a regrown list under any new testid still fails); shell.test.tsx
dropped the spec-decision-9 tombstone (ReconnectingBanner's header comment is the
surviving render-site note, and its suite covers all three connection arms).

### G15. No awaited remote cleanup in a bare `finally` — LANDED (Wave 12)

**Policy:** cleanup I/O in a bare `finally` replaces the primary exception with
transport noise — wrap it in `teardown_step` (public since this wave,
`otto.host.connections`) or hand the coroutine to `lifecycle.compensate`.
**Mechanism, as landed:** per-arm severity forced two rule files (one severity per
rule doc): `.ast-grep/rules/no-awaited-exec-in-finally.yml` (`severity: error`) and
`no-awaited-close-in-finally.yml` (`severity: warning`, BY DESIGN never promoted —
a close can be the operation's own completion; the warning is the reviewer's cue to
decide once, at the site). Both arms are relational **plus a bare-vs-wrapped
discriminator the plan draft missed**: a `with teardown_step(...):` around the await
still sits INSIDE the `finally_clause`, so without `not: inside: with_statement
(stopBy: finally_clause)` the sanctioned fix would itself match. The exemption is
syntactic (any `with` silences it) — accepted and documented in the rule note.

```yaml
rule:
  pattern: await $X.exec($$$ARGS)   # close arm: await $X.close($$$ARGS)
  inside: {kind: finally_clause, stopBy: end}
  not:
    inside: {kind: with_statement, stopBy: {kind: finally_clause}}
```

No `ignores:`: connections.py needed no carve-out (its close chain is with-wrapped
by construction — rule-verified zero hits). `compensate(...)` call sites don't match
(`compose.py`'s teardown confirmed): the `.exec` lives un-awaited inside the call.
**Red measured (rule, pristine tree):** 2 error (`docker_host.py:524,:601` put/get
staging `rm -rf`) — plan's count held; **9 warning, not the plan's 1** (the
read-count missed cli/expose, cli/monitor, suite/plugin, config/repo,
coverage/produce, coverage/reporter, host/host app-shell, host/remote_host).
Triage: 8 wrapped in-wave; `RemoteHost.close`'s transport close judged the arm's
legitimate case — it is `close()`'s OWN result, its loud-failure contract is pinned
by `test_unix_host.py`'s close-chain sweep (wrapping it turned that sweep red), and
it carries the arm's one site-level `ast-grep-ignore` with that reasoning.
The warning arm is non-blocking by construction — `ast-grep scan` fails only
on errors — so warning-site regressions surface in lint output, not as a red
gate; the per-site pins guard the error-arm sites and the helper's contract.
**Landed:** Wave 12, fix-with-gate; zero unreviewed sites at landing.

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
- [x] Reproduce the hang per the todo file's recipe (live bed; state expected duration
      up front). *(Reproduced DETERMINISTICALLY rather than by load-looping: a remote
      listener holding LISTEN without ever calling accept() — the race window, held
      open. Probe on tomato-via-carrot_seed: connect through the forward succeeded in
      0.01s though the remote never reached accept(). GET's read() sat >20s — the
      CONFIRMED shape of the natural GET hang. Honest split (interim review, finding
      2): the 15-byte PUT payload never reaches a drain wait (all under the
      transport's high-water mark), so the natural PUT hang is ATTRIBUTED — not
      proven — to the previously-unbounded forward setup; the probe's PUT drain
      stall required ~10 MiB buffered and covers the large-send mechanism.)*
- [x] Fix; soak the two tests ×20 on the bed without the marker (integration lane,
      serial — no heavy parallel load on the dev VM). *(Fix: every tunneled data-phase
      step bounded — forward setup 5s; each drain/read a 5s ZERO-PROGRESS window
      (progress re-arms it; a plain wait_for would be a ~100KiB/s throughput floor
      that `link impair --rate` legitimately undercuts — interim review finding 1);
      close handshake 2s then transport.abort() (finding 3: close()+suppress leaked
      the fd + 32MB buffer); GET gains the fresh-port one-shot retry and an
      empty-transfer-vs-known-size check (narrow by design: a non-zero short read is
      NOT failed — the stat is a snapshot and growing files are legitimate, finding
      4; unknown size skips, pinned as a documented residual). Bounds budget-pinned:
      2 attempts of data-path bounds must fit the 30s integration wrapper. 14 new
      unit tests; the original 5 red vs main, the semantics batch mutation-killed
      bound by bound (9 mutations, incl. papering 5.0→1000.0 and the frozen
      zero-progress baseline). Soak: 20/20 pairs passed with
      DEBUG logs captured — ZERO retry-path entries, i.e. the race did not occur
      naturally in 40 executions; the soak certifies the healthy path under the new
      bounds, and the race path is certified by the deterministic probe + pins. This
      is CONTAINMENT, not elimination: the LISTEN-vs-accept window stays open per
      transfer, and the todo's probe-past-accept alternative is inapplicable — nc
      serves exactly one accept, so a readiness connect would consume it.)*
- [x] Remove `@pytest.mark.retry(3)` from both tests, delete
      `todo/hop_nc_transfer_flake.md` (its own exit criterion), delete the G1 ignore —
      gate now fully armed. *(Arming mutation-proved: a marker re-added to the hop
      file trips the scan; clean after revert.)*

### Wave 3 — Probe-status honesty (item 3; G5)
**Files (as actually touched; the plan's `tests/e2e/chaos/bed_hygiene.py` path was a
drift):** `tests/_fixtures/bed_hygiene.py` (contract + `argv_pattern` + nc-listener
probe), `tests/e2e/chaos/_bed.py`, all seven tier-3 consumer modules
(`test_harness.py`, `test_session_chaos.py`, `test_transfer_chaos.py`,
`test_tunnel_link_chaos.py`, `test_connection_drop.py`, `test_reboot_chaos.py`, and
`conftest.py`'s bracket transitively), `tests/integration/chaos/test_signal_run.py`
(hand-rolled bracket-trick deduped), new `tests/unit/test_bed_oracle_honesty.py`,
`docs/architecture/quality-gates.md`.
- [x] Write the falsifiability pin (stub host, Error-status probe) — must FAIL against
      current `run_probe`/`snapshot_host` (record for the commit message). *Done: 17
      pins in `tests/unit/test_bed_oracle_honesty.py` (unit-lane, so they run in every
      default gate); collection-red at t0 + 4 defect demos green against pre-fix code.*
- [x] Make `run_probe` raise host-named on non-ok; `snapshot_host` asserts per-probe
      `is_ok`; update consumers (they get *simpler* — happy-path reads only); pin green.
      *Done: one `check_probe_result` spelling backs both; `probe_text` replaces every
      value-unwrapping factory (session pids, transfer nc-listeners, qdisc reads,
      harness round-trip); `veggies_link_id`'s except narrowed to ValueError. 9
      mutations killed (incl. both consumer-drift scans against the exact
      reintroductions the opus review used to demonstrate blindness — the factory
      scan's first cut MISSED the named-factory shape and was fixed by its own
      mutation run).*
- [x] Opus-review riders, all landed in this squash: consumer-drift AST scans over
      both chaos lanes (factory-`.value` ban incl. named local factories; pattern
      kills must be built by `argv_pattern`) with embedded positive controls;
      `argv_pattern` found live when the contract made session's cleanup
      `pkill -f 'sleep 313'` raise — the pattern matched its own wrapper shell's argv
      and self-killed, silently reported as Failed all along; the scan then caught
      transfer's verbatim `_NC_LISTENER_PROBE` mirror (now imported from the one
      authority, which also dropped the `grep -v "$$"` filter that could hide a real
      listener whose line contained the wrapper's pid); sequential `finally` cleanups
      nested so a raising probe reports without skipping siblings (tunnel_link
      rollback, transfer ×2). Audited-not-changed: `test_privilege_chaos` (all
      `.value` reads — 10 distinct sites — compared against literals/nonces, so a
      dead probe fails loudly) and
      `test_docker_chaos` (35 reads status-asserted inline; its autouse bracket DOES
      ride the changed `snapshot_host`). Siblings OUTSIDE the chaos lanes for Wave
      14's sweep — pattern kills still hand-rolled:
      `tests/e2e/tunnel_stability/_harness.py:167` (result discarded under
      `suppress(Exception)` — silently self-killing today), `test_tunnel_e2e.py:540`,
      `test_link_impair_e2e.py:664,:711`; and verbatim mirrors of the OLD
      `_NC_LISTENER_PROBE` spelling (with the real-listener-hiding `grep -v "$$"`
      filter this wave removed from the authority) at
      `tests/integration/host/test_session_stability_integration.py:275,:421,:453,:470`.
      *All eight retired in Wave 14, which also widened the pattern-kill scan
      from the chaos lanes to the whole tests tree (rule-measured t0 on the
      pre-Wave-14 tree: exactly those 8 sites).*
- [x] Hand-probe the chaos bed once (gate blind spot rule: the chaos lane is never in
      default gates — run `nox -s chaos` legs touched, with Chris's bed coordination).
      *Done for the non-reboot legs (harness, session, transfer, tunnel_link,
      connection_drop), re-run after the opus riders. The reboot module's two
      probe_text conversions ride the same helpers those legs certify; its
      soft-reboot scenarios were NOT hand-run this wave (powering lab VMs stays a
      Chris-coordinated act) — at the next coordinated bed session run `make chaos`
      and watch two NEW red paths that are correct G5 outcomes: a post-reboot tomato
      with eth2 not yet up now RAISES from `_eth2_qdisc` (pre-wave it read "netem
      absent" off "Cannot find device eth2" and PASSED the expected=False assert),
      and `test_docker_chaos`'s bracket now raises on a dead docker-parent probe.*

### Wave 4 — Ambient hermeticity (item 4; G11)
**Files:** `tests/integration/conftest.py:32`, `tests/_fixtures/paths.py:33-35`,
`tests/conftest.py:112-138` (colour block at 112-115 + the `OTTO_*` strip at
137-138 — together the sanctioned block), `tests/unit/test_env_hermeticity.py`, new
`tests/unit/test_conftest_env_writes.py` + `tests/_collect_env_probe.py`.
**Decision (default):** move `ensure_sut_dirs()` into a session-scoped autouse fixture
in the integration conftest that sets the var via `os.environ` + registers teardown —
*and* imports config lazily so the import-time singleton constraint is satisfied by
fixture ordering, not import order. If the `_repos` singleton makes that infeasible,
fall back to: keep the import-time write but declare it in `AMBIENT_OPT_INS` with a
comment, so the pin sees it and the two-lane config divergence becomes explicit.
Either way the *pin* is the deliverable; the write becomes declared or scoped.
- [x] Widen the hermeticity pin (3-tree parametrization); confirm the integration leg
      FAILS pre-fix (proven red; record for the commit message). *Done as a NEW
      collection-time pin (`--collect-only` + `tests/_collect_env_probe.py` plugin at
      `collection_finish`) — the runtime probe can't draw the import-time/runtime
      boundary, a collect-only run can. t0: integration leg red with
      `['OTTO_SUT_DIRS']`, rc=3; unit/e2e legs green.*
- [x] Apply the chosen fix; pin green in all three trees. *Default option taken: the
      "import-time singleton" justification was STALE (all readers lazy), so a plain
      session-scoped autouse fixture suffices — `_impl` pattern, set-with-restore,
      teardown pinned directly (`test_integration_sut_dirs_fixture_sets_scoped_and_
      restores`); `ensure_sut_dirs` deleted from paths.py.*
- [x] Land `test_conftest_env_writes.py` (AST scan + positive control); move or
      allowlist-with-comment the root colour/TERM block. *Guard t0-red at exactly
      `['tests/integration/conftest.py:32']` via one-hop call resolution; aliases
      resolved after a mutation escaped through them; single-descent walker after a
      redundant ast.walk hid a dead branch AND false-flagged nested defs. Colour/TERM
      block: ALLOWLISTED in place (root conftest = the one sanctioned block) — it
      must precede the root conftest's own otto imports, which pytest_configure runs
      after; move rejected.*

### Wave 5a — Cheap weak-test fixes (item 5 first half; G3)
**Files:** the 7 precondition sites (`test_lab_health.py:37`, `test_lab_data_hops.py:36`
— skip→fail + non-empty assert, `test_listing.py:415`,
`test_gen_monitor_fixtures.py:121,:128,:246`, `test_tuple_return_debt.py:117`, plus
§4.3's `test_webassets_guard.py:61`, which was in the review's at-risk list but had
dropped off this wave's — 8 precondition sites in all), the 7 typer.Exit sites (see
G3's baseline note), `test_cov.py:337,:350` (assert the error message via the mock),
`test_cli_registry.py:243` (FrozenInstanceError, drop noqa), new G3 rule file.
- [x] One-line `assert <collection>` preconditions; `test_lab_data_hops` landed
      stronger than planned: both techs stay parametrized under a two-sided assert
      (`bool(embedded) == (tech in _EMBEDDED_TECHS)`), so tech1 losing its embedded
      hosts fails loudly (the sweep accident) AND tech2 gaining embedded hosts
      without joining the tuple fails loudly — no skip, no silent exclusion; every
      surviving embedded host must also retain its hop (partial sweep = same
      accident).
- [x] typer.Exit sites: bind + `assert excinfo.value.exit_code == <N>` (read each
      command to pin the *right* N — bootstrap gate is 1; usage errors are 2).
- [x] Land G3 at error in this squash; prove red on the stashed pre-fix tree (7 hits —
      see G3's baseline note; an earlier statement-shaped pattern counted 5, blind to
      the two parenthesized-`with` sites).

### Wave 5b — ValidationError match= burn-down (item 5 second half; G4)
**Files:** `.ruff.toml` + the 71 sites (ruff-verified; review §4.4's 52 was a grep
undercount — see G4's Red-today note).
- [x] Add the `[lint.flake8-pytest-style]` block; run ruff — the emitted PT011 list is
      the burn-down inventory (final measured t0 = 71, not the review's ~52; see G4's
      Red-today note for the 52 → 66 → 71 evolution). Landed as the extend- form with
      a `*ValidationError` glob — no hand-copied stdlib mirror to drift on upgrades.
- [x] Fix mechanically in the same squash, one `match=` per site naming the field/
      constraint under test. No `--fix` exists for this. Dominant landed form:
      `(?m)^<full.dotted.loc>\n\s+<constraint message>` — pinning WHICH field and WHY
      (pydantic's `input_value=` echo makes bare field-name matches blind; see the
      stated residual).

### Wave 6 — Test-boilerplate consolidation, high-leverage pair (item 6; G8 + G9)
**Files:** `tests/e2e/_otto_subprocess.py` (add `extra_argv_prefix`, `cwd` params), the
14 copy sites (delete — see G8's corrected baseline), `tests/_fixtures/paths.py`
(export `TESTS_ROOT`, `PROJECT_ROOT`), 67 `parents[N]`-class sites (see G9's corrected
baseline), two new rule files.
- [x] Extend `run_otto`; migrate the 14 modules (landed as three layered builders:
      `coverage_subprocess_env` → `otto_subprocess_env` → `run_otto` — the PTY and
      chaos drivers take the env builder, the unit-lane python-probe takes the
      coverage-only one); delete local `_otto_env`/`_run_otto` copies (net -274 LOC
      across the 14). Children in 13 files now carry `-p no:tach` for the first time —
      `test_stability_e2e.py` ran on the bed: 2 passed, no #193 panic. *Old-vs-new env
      reconstructed and diffed at every invocation site (implementers + reviewer,
      independent harnesses): the scar key is the ONLY delta. Accepted argv/text
      deltas, each probed neutral: `-R` precedes `--lab`; `-l`→`--lab`; chaos
      PYTHONPATH trailing-separator normalization (empty-parent case only).*
- [x] Export roots; migrate the 67 sites (mechanical; equivalence proven old==new per
      site with a negative control; 3 `lab_data_path` bypasses routed through
      labdata helpers).
- [x] Land G8 + G9 at error in this squash; proven red on the pre-fix tree: 14 files
      (G8) and 67 hits/58 files (G9). *G8's planned bare-pair pattern does not parse —
      landed as context/selector; G9 landed with 8 arms after two draft residual
      claims (qualified spelling, .parent chains) tested FALSE — grep cannot see
      ruff-wrapped multi-line chains, which is why every count below a rule-measured
      one was an undercount.*

### Wave 7 — `wait_for` primitive (item 7; G6) — landed as ONE squash
**Files:** `src/otto/utils.py` (+ `tests/unit/test_utils_wait_for.py`, 23
tests — 22 fake-clock + 1 real-loop smoke), the 5 product sites, the 16 test sites, the rule file.
**Interface (locked; one recorded extension):**
```python
def wait_for(predicate: Callable[[], bool], timeout: float, *,
             interval: float | Callable[[int], float] = 0.1,
             probe_first: bool = True, on_timeout: str | Callable[[], str]) -> None
async def wait_for_async(...)  # same shape; awaits predicate if it returns Awaitable
```
Raises `WaitTimeoutError` (a `TimeoutError` subclass — the helper's expiry stays
distinguishable from a timeout raised by the predicate, so a wrapping
`except WaitTimeoutError` can never swallow a probe's own timeout) with the rendered
`on_timeout` message (mandatory — silent expiry is the defect class; there is no
return-False mode). NaN timeouts and negative-or-NaN intervals are rejected loudly
(NaN defeats both the expiry comparison and the sleep cap; zero stays legal — sleep(0)
is the tight-poll yield mock-backed callers already use). *Extension over the locked shape:*
`interval` also accepts a `sleep_index -> seconds` callable — nc.py's listener wait has
a deliberate, constant-named fast-poll ramp (`_NC_LISTENER_FAST_POLL_ITERS`), and the
alternatives were deleting a measured optimization or leaving the poll hand-rolled;
preserve the computation, not the observed value. The final sleep is capped to the
remaining budget with one last probe at the deadline edge (total wall time never
overshoots `timeout`), and `probe_first=True` probes once even on an exhausted budget —
the `unix_host.py:811` do-while comment, now the helper's documented contract.
- [x] Helper + fake-clock tests (probe-first vs sleep-first schedules, capped final
      sleep + edge probe, exhausted-budget edges, lazy `on_timeout`, ramp callable,
      predicate-exception propagation; async twin incl. plain-bool predicates).
- [x] Product sites — the review's "host.py ×4" was 2 (the other two are budget
      splitters, not polls): `wait_until_up`/`wait_until_down` keep their bool API via
      `try/except WaitTimeoutError`; `_confirm_recovered` converts its deadline-instant to
      a remaining budget with `probe_first=True`; `_connect_with_retry` keeps its
      per-attempt `min(1.0, max(0.1, remaining))` bound inside the predicate and
      re-raises `ConnectionError from last_err`; `_wait_for_remote_listener` keeps its
      ramp via the interval callable and its `ConnectionError` type.
- [x] Test sites (16): trivial predicates, nonlocal value captures, predicate-raise for
      child-death early failure (`_driver`, tunnel-link-chaos mirror, `_sshd`),
      `probe_first=False` for the two deliberate sleep-first expiry waits
      (link-impair), the one silent-expiry-by-design site wrapped in
      `contextlib.suppress(WaitTimeoutError)` (monitor e2e — later assertions decide), and the
      stateful quiescence closure (replay-soak). Accepted deltas recorded in the squash
      message: AssertionError→WaitTimeoutError expiry types (audited: the one catcher
      of the old type, a best-effort suppress around the chaos driver's stderr wait,
      was widened to suppress both — early-death still raises AssertionError there);
      the link-impair post-wait asserts removed (success implies them; messages moved
      into on_timeout); the tighter final-sleep capping; and wait_until_up/down now
      probing once even on an exhausted budget (the probe_first rationale, commented
      at both call sites — reboot()'s worst case grows by one probe bound at the
      budget edge instead of failing an up host unprobed).
- [x] G6 at error in this squash; t0 = 21 rule-measured (see the gate section for the
      composition and the review-26 correction).

### Wave 8 — Readiness as an event (item 8; G7) — DONE
**Files:** `src/otto/monitor/server.py` (`async def wait_started()` — blocks on an
`asyncio.Event` that `serve()` sets on success AND failure, re-raising the recorded
startup exception, with a recorded `CancelledError` translated to `RuntimeError` so a
never-cancelled waiter's own cancellation state stays clean; `serve()`'s ENTIRE
prologue — config construction, task spawn, startup wait, port extraction — sits
inside the record-and-release guard, so no startup-phase exit can strand a waiter,
and a second `serve()` on the same instance is refused loudly (the latch and the
recorded outcome are single-shot); the internal uvicorn wait runs through
`wait_for_async` with a task-death predicate, since uvicorn itself exposes no event;
a serve task that returns CLEANLY before signalling startup raises rather than
letting the predicate poll a done task forever), `src/otto/suite/suite.py`
(`await wait_started()`, and on failure the dead `_monitor_task` is reaped before
the re-raise — a parked dead task double-raises in `stop_monitor()` and fires
"never retrieved" at GC), the 12 test sites across
`test_server{,_signals,_auth,_tls}.py` (including the local `_wait_started(server,
task)` helper the API absorbed), rule file.
- [x] Awaitable-started API + failure-path tests FROM THE WAITER'S SIDE
      (`TestWaitStarted`): bad-TLS pair re-raises `ssl.SSLError`; an
      already-bound port surfaces the `server.py` SystemExit→RuntimeError
      translation; a clean pre-startup return raises instead of hanging; a
      cancel landing inside the prologue releases the waiter with the
      translated RuntimeError; serve-twice is refused; suite-side, a startup
      failure re-raises out of `start_monitor()` with `_monitor_task` reaped.
      Every `noqa: ASYNC110` in the tree carried the justification "no event
      source available" and died with this wave (zero remain). One documented
      residual: a serve task cancelled before its FIRST step runs no body code
      at all (Python throws into the never-started coroutine), so no
      in-function guard can release waiters — callers must not park a waiter
      that outlives such a cancel (suite.py's waiter lives in the task that
      spawned serve, so it dies with it).
- [x] `suite.py` fixed; 12 test loops fixed; G7 at error in this squash. Prove-red
      t0 rule-measured on the pre-fix tree: 14 hits, not the review's 11 (the
      review missed server.py's own internal poll, `test_server_signals.py`, and
      one `test_server.py` site). Seven arms (bare attr, parenthesized negation,
      call spelling, `is False` / `== False` / `is not True`, drain-loop
      `if started: break`); compound conditions and the dashboard harness's
      bounded cross-thread `wait_for` flag poll are the documented non-arms.

### Wave 9 — Interact-e2e residuals (item 9) — DONE
**Files:** `tests/e2e/host/test_interact_e2e.py`, `tests/e2e/host/_pty_driver.py`.
The host-lease fix (class-scoped `leased_carrot` via `lease_unix_host`, `ELEMENT`
single-sourcing) was independently root-caused from the `make release` 3.14 failure by a
concurrent session and landed earlier in `bef943aa` — see review §10 correction and the
`project_release_314_shell_history_race` memory topic. The three residuals (review
§3.2) landed together, and un-suppressing exposed a fourth, deeper defect:
- [x] **The suppressed `stty size` expect was hiding a test that covered nothing.**
      Un-suppressed, it failed on BOTH backends: three probes over 15s all reported
      the stale `24 80`. Root cause is the PTY driver, not the product:
      `start_new_session=True` with an inherited slave fd gives the child NO
      controlling terminal, and the kernel delivers resize SIGWINCH only to the
      controlling terminal's foreground process group — i.e. to nobody, ever. The
      SIGWINCH-forwarding branches this test exists to cover had never executed
      under test (proven with a standalone handler-printing probe: zero signals
      without a ctty, correct `50 132` with one). Fix in `_pty_driver.py`: a
      post-exec `python -S -c` shim acquires the slave as controlling terminal
      (`TIOCSCTTY` on fd 0) then `execvp`s otto in place — same pid, so
      wait()/killpg semantics are unchanged, and thread-safe where a `preexec_fn`
      is not (PLW1509 stays enforced). All driver consumers re-run green:
      interact e2e (10), login-proxy e2e (7), chaos signal-login (2, re-run by
      hand as well; that file is tier-2 and IS inside `make coverage` — the
      `not chaos` exclusion is tier-3 `tests/e2e/chaos/` only).
- [x] The `time.sleep(0.3)` settle + suppressed expect became one construct: the
      SIGWINCH forwarders leave no local artifact on success (debug-log only on
      failure), so there is nothing for a `wait_for` predicate to poll — the plan's
      sketch was wrong on that point. The remote's own report is the one
      observable: `stty size` probed up to three times, each attempt's `expect`
      timeout owning the pacing (a fused probe-response loop per the Wave 7
      pacing-owner classification, not an interval poll). On exhaustion it FAILS
      naming the backend. Mutation-proven: dropping `sess.resize()` trips it.
- [x] Round-trip token: both occurrences now fail loudly, naming which was
      missing. First expect timeout → "the command echo (first token occurrence)
      never came back". Second → discriminate via the consumed bytes: if the first
      match carried a clean `echo <token>` the response is genuinely missing →
      fail; if not, cursor-repaint mangled the echo and the first expect consumed
      the RESPONSE, so the round trip is proven → drain and continue (the one
      case the old silent path was right about). Mutation-proven both ways:
      `> /dev/null` (echo seen, no response) and `true` (no token at all) each
      trip the intended arm with the intended message.
- [x] Ownership note (recorded at Wave 9's landing): the CHAOS-suite
      suppress-around-expect (review §3.2 bullet 1,
      `tests/e2e/chaos/test_tunnel_link_chaos.py`) is accepted AS-IS — its
      suppress is documented best-effort behind hard rc/survivor asserts, and
      Wave 7 deliberately widened it for the expiry-type migration. No wave
      owns converting it; revisit only if Wave 16's timing hardening gives a
      reason.

### Wave 10 — Error taxonomy (item 10; G10) — DONE, landed as ONE squash
**Files:** `src/otto/coverage/capture/gitio.py`, `src/otto/coverage/anchor.py`,
`src/otto/cli/cov.py`, NEW `src/otto/host/errors.py` (+ `docs/api/host/errors.rst`),
`src/otto/link/manage.py`, `src/otto/cli/link.py`, `src/otto/tunnel/manage.py`,
`src/otto/tunnel/socat.py`, `src/otto/docker/staging.py`, `src/otto/docker/compose.py`,
`src/otto/host/transfer/nc.py`, `src/otto/errors.py` (census), `tach.toml`
(`otto.link` -> `otto.errors`), the rule file, `docs/architecture/quality-gates.md`.
The plan's a/b/c split was dropped for the same reason the rule's warning phase was:
the fix-with-gate ruling puts the conversion and the gate in one squash, so there is
nothing an intermediate commit could be green against.
- [x] **`gitio` split** — `GitMissingError` / `NotAGitRepoError` /
      `GitCommandFailedError` under `GitUnavailableError`, classified at the one
      chokepoint the two runners share, via a failure-path-only
      `rev-parse --is-inside-work-tree` probe. The three
      `"not a git repository" in str(e)` string matches are gone: git TRANSLATES its
      messages, so that test silently stops discriminating under any non-English
      `LC_MESSAGES` and every caller then takes its command-failed branch. Two masked
      defects fell out and are fixed — `blob_sha`/`blob_exists` folded a MISSING git
      into "absent" (a box without git reported every file in the tree as new, with no
      message anywhere), and `cli/cov.py`'s `_resolve_tester` read the same thing as
      "this tester has no email". Both propagate now; only a genuine git-said-no still
      degrades. `cov report`'s hardcoded "not a git repository" line is finally true by
      construction (it sits on the `NotAGitRepoError` arm), with a new generic arm
      echoing what git actually said. Mutation-proven: reverting the classifier to
      always-GitCommandFailedError fails 5 tests across `tests/unit/cov` and
      `tests/unit/cli/test_cov.py`. The classification probe runs through `_run_raw`
      like every other call, NOT straight to `subprocess`: that is the chokepoint the
      spawn-budget guards monkeypatch, and a failure-path spawn they cannot see is a
      spawn no budget bounds (a new test pins the probe as counted; the recursion
      guard is that the probe never classifies itself). A nonexistent `cwd` raises the
      same `FileNotFoundError` a missing git does, and used to be reported as "git
      executable not found" — harmless while callers swallowed it, a propagating lie
      once `blob_sha` started re-raising; it is a `NotAGitRepoError` now.
- [x] **`link/` read path** — `LinkHostUnreachableError` / `LinkCommandFailedError`,
      both `(OttoError, RuntimeError)`, so the CLI's `except (ValueError,
      RuntimeError)` clauses are untouched — those must stay wide, since typer's
      vendored click makes `typer.Exit` a `RuntimeError` subclass. NEW
      `LinkState.read_error`: a reachable host whose `tc` failed rendered exactly like
      a host that was down (`?` plus "partial scan"), sending the operator to check
      the network for a fault that was the host's own tooling, and discarding tc's
      message; it now renders `!`, its own row, and its own summary line.
      `_cancel_timers` skips only on unreachable — a failed `ps` on a REACHABLE host
      means otto cannot see the timers about to fire against the state it is midway
      through changing, which is not hygiene. `_ensure_not_foreign` refuses with
      `ValueError`, so `repair --all` now SKIPS a foreign qdisc (never otto's
      impairment) instead of collecting it as a failure; `repair <id>` still refuses
      loudly. Mutation-proven: folding the read failure back into `unreachable` fails
      the new test. The read arms end at bare `RuntimeError`, NOT at the two new
      classes, and that width is load-bearing: the host stack below raises unnamed
      RuntimeErrors no rule scopes (`host/session.py`'s dead session,
      `host/docker_host.py`'s not-running container, `host/remote_host.py`'s
      unresolvable hop), and narrowing let them propagate out of `read_link_states`,
      which promises never to raise per link — `otto link list` has no `try` of its
      own. `read_errors` is a per-DIRECTION dict, not one link-wide string: the two
      directions land on different hosts, so one endpoint down and the other's `tc`
      broken is a real shape that a single field had to pick one story for.
      `repair_all` returns a `RepairAllReport` dataclass with a third `skipped`
      bucket that the CLI names — a foreign qdisc used to be a bare `continue`, so
      the sweep printed "repaired 0 link(s)" and exited 0 saying nothing about the
      link it declined. Widening the old 2-tuple instead would have been the exact
      defect `no-tuple-return` was written for; converting it retires a DEBT entry.
- [x] **Shared host pair + the rest** — NEW `otto.host.errors` with
      `HostUnreachableError` / `HostCommandError` as PEERS (neither implies the other)
      plus `exec_or_raise`, which is link `_exec`'s proven order (transport,
      timed-out, not-ok) — and which link `_exec` now IS, passing its own two classes
      in as the `unreachable`/`failed` arguments. The classes are parameters because
      the sequence is what repeats while the taxonomy belongs to the caller. No
      tunnel/docker/transfer site adopts it: each owns a message the helper's pattern
      cannot produce (`_require_tools` says "timed out checking for socat", and tests
      output text rather than `is_ok`), and the brief was to change the TYPE, not the
      text. The timed-out split is a real fix in transfer — nc's port strategies
      read `retcode`, which cannot tell a scan that SAID no from one killed by its
      timeout (`-1` also means "never ran"), so every site holding a `CommandResult`
      now asks `timed_out`; `put` keeps one arm because a bare `Result` has no such
      field. `socat.pick_free_port` gets `NoFreePortError` (pure-local range
      exhaustion, no host involved). `compose`'s no-services check became a
      `ValueError` — nothing on the parent failed.
- [x] **Census re-measured, not adjusted** (`src/otto/errors.py`). The counting method
      was RECOVERED by reproducing the original numbers at the commit that wrote them
      (`1393b087`): a raise site is a `raise` of a BUILTIN exception name, and it is
      covered when that builtin is `ValueError`/`RuntimeError`-rooted (which is why the
      42 `NotImplementedError` raises count) — that method reproduces 330/284 there
      exactly. Now: 301 sites, 254 covered, 34 named classes, 23 catchable, 7 rootless,
      4 under `OSError` (23+7+4=34). Two claims were already stale and are corrected
      rather than carried forward: the OSError list omitted `RetryAttemptTimeoutError`
      (a `TimeoutError`), and "all but five raises" missed `SyncPhaseInterrupt`, a
      `KeyboardInterrupt` that `except Exception` also does not catch — six.

### Wave 11 — Registry-guard invalidation (item 11) — DONE
**Files:** `tests/conftest.py` (`_loaded_registries`),
`tests/e2e/cli/test_registry_isolation_e2e.py`.
- [x] Measured first, as instructed: the full scan is 0.216 ms with all of
      otto imported (121 otto modules of 488 total) — 25x under the 5 ms
      keep-the-cache threshold, so the cache is DROPPED rather than re-keyed
      (the frozenset key would have cost 0.03 ms per call to keep a memo
      worth 0.2 ms; there is nothing to pay for). `_loaded_registries`
      re-scans every call; the docstring records the measurement and the
      identity-blindness of the `len(sys.modules)` key it replaces.
- [x] Completeness pin, proven RED against the cached implementation first:
      fabricate an `otto._registry_probe_w11` module carrying a fresh
      Registry, evict one loaded otto module in the same test (module count
      unchanged — the exact shape the count key was blind to), assert
      discovery sees the new registry. Red pre-fix, green post-fix; whole
      tests/unit green (5129) with no measurable slowdown.

### Wave 12 — Collection-crash + finally-await (items 12; G15) — LANDED
**As landed.** The e2e resource-marker rule no longer raises `pytest.UsageError`
from `pytest_collection_modifyitems` (an xdist CONTROLLER crash under the repo's
`-n auto` — reproduced: `INTERNALERROR`, `xdist/dsession.py:217 assert not
crashitem`, and the crash blames whichever INNOCENT item the dead worker held).
Enforcement is deferred: the collection hook (now a tryfirst *wrapper*) stamps
violations on the offending item (`pytest.StashKey`) and — the interim review's
MAJOR — RE-APPENDS offenders that `-m`/`-k` filtering removed, because a test
mistagged hostless+integration is otherwise deselected into permanent silence on
the hostless lane, the only lane CI runs. A tryfirst `pytest_runtest_setup` hook
(not an autouse fixture — the review showed a higher-scoped fixture failure would
swallow the message, and a mistagged test should not pay session fixtures or
touch a testbed first) fails the item with the rule's own message — xdist-safe by
construction. Decision logic extracted as the pure `_resource_marker_violations`
(the `_browser_group_key` pattern). The deliberate trade, documented at the hook:
`--collect-only` no longer aborts — a run precondition must not fire when nothing
runs (#196 doctrine) — while no marker expression can hide an offender from a
running lane, and skip/xfail marks cannot hide the failure either — the hook
preempts `_pytest.skipping`'s tryfirst setup hook (later conftest registration
runs first), verified empirically at landing (fable's find: the draft claimed
the opposite as a residual).
Pins: `tests/unit/test_resource_marker_policy.py` (truth table);
`tests/e2e/test_marker_rule_deferral.py` (pytester-subprocess nested sessions over a
runtime copy of the REAL conftest — proven red pre-fix under `-n 2` with the exact
controller-crash signature, which satisfies the scratch-repro checkbox without
touching the dev repo; the `--collect-only`-is-clean pin, red pre-fix at
`ExitCode.USAGE_ERROR`, with positive collected-probe asserts; and the
deselection pin, proven red against the fixture-based first draft — offender
deselected, 1 error instead of 2 — before the re-append landed).
Product: `teardown_step` promoted public; 2 error + 8 warning sites wrapped, 1
recorded legitimate — inventory, triage and the remote_host chaos-sweep story in
the G15 section above. Masking pins: docker put/get staging cleanup (3 tests),
unix interactive telnet close (2), `teardown_step`'s own swallow/warn +
cancellation-passthrough contract (2). All guards mutation-proven on the committed
tree (M1 UsageError revert, M2 stamp drop, M3 report-hook blind, M4 count weaken, M5/M6
site unwraps — each also re-fires the rule — M7 BaseException widen, M8 planted
probe shapes incl. with-exemption and out-of-scope silence).
Recorded residuals (same defect family, invisible to the two rules' `.exec`/
`.close` spellings; left for a chain-shaped pass): `cli/monitor.py`'s teardown
finally runs `db.finalize` before the wrapped collector close, so a raising
finalize still skips the close; `suite/plugin.py`'s finally is the identical twin
— `monitor_db.finalize` (and the `output.write_text` branch) run ahead of the
wrapped close, and a raise there also skips clearing the
`OttoSuite._session_monitor_collector` slot; `transfer/nc.py:778,985`
(`_close_writer_bounded`) and `:855` (`server.wait_closed`) are bare finally
awaits under names the rules cannot see. `app_shell.attach`'s `_exit` was the
worst of these — it defeated this wave's own `app_shell` session-close wrap one
frame down — and was fixed in-wave (masking-only: best-effort exit when the body
is already failing, loud exit on a clean body because a wedged REPL is the
caller's business; both pinned).

### Wave 13 — Skip-policy pins (G12) + embedded-coverage hard-fail — LANDED
As landed, one squash: `test_no_skip_lanes.py` (proven red pre-fix on the 2 cov
sites, exact inventory in G12 above), the 2 skip→fail conversions, and the
tier-marker stale-skip fix. Details, measured red, and the four mutation proofs are
recorded in the G12 section.

### Wave 14 — Harness silent-degradation sweep (review §5.4–5.6 remainder) — **LANDED**
One squash; every guard red-proven against the pre-wave tree and mutation-proven
on the committed one. As landed:

- **CliRunner shield fails loud** — `except ImportError: yield` became a
  `pytest.fail` naming the pytest rename and forbidding the yield fallback; the
  guard body split into `_clirunner_guard_impl` so
  `test_clirunner_capture_guard.py` drives the fail arm directly
  (mutation: yield-arm restored → red).
- **Coverage pre-init acts on its outcome** — `force_coverage_schema_init`
  returns the full traceback instead of `False`; the collection hook stashes a
  `PreinitOutcome` dataclass; a new session-scoped autouse
  `_coverage_preinit_failure_is_loud` (in `GLOBAL_GUARDS`) fails the worker's
  tests with the recorded traceback (deferred, xdist-safe, fixture-based so
  `--collect-only` never fires). Truth table + hook-composition pins in
  `test_coverage_schema_preinit.py`; deleting the stash write turns every test
  on the worker red with the named message — proven live in the mutation run.
- **FD-watermark consolidated** — `tests/_fixtures/fd_watermark.py` (baseline
  `gc.collect()` + collect-before-verdict + one retry; the tunnel_stability
  shape won), imported by name in the three lane conftests.
  `test_fd_watermark.py`: behavior pins incl. a hidden-leak control that
  reproduces the drifted copy's exact failure (missing baseline gc → red), and
  a drift guard failing any conftest that re-grows a local copy (t0: all 3
  red). The first-collect-vs-retry split is a proven behavior-preserving
  mutant pair — the pins own the property, not the path.
- **Tunnel reap logs** — `reap_tunnels` wraps each `remove_tunnel` in
  `teardown_step` (the W12 authority): failures log tunnel id + exception and
  reaping continues; the leftover sweep stays the consequence-catcher.
- **Snapshot-restore, not reset** — `_restore_otto_logger_state` and
  `_restore_bootstrap_state` replace the two default-reset fixtures: snapshot
  at setup (state copy + otto-logger handlers/level/propagate; the three
  bootstrap globals), restore at teardown (test-created listeners stopped,
  test-registered atexit hooks unregistered, test-added captures detached).
  `test_guard_snapshot_restore.py` pins it order-independently (module fixture
  primes state, BOTH tests assert it — either order red pre-fix, t0-proven);
  `test_env_hermeticity.py`'s poisoner/victim pair still proves the isolation
  half. Stated limit: a listener live at snapshot that the test stops cannot
  be resurrected (no current venue has one).
- **Pattern-kill class closed tree-wide** — the 8 recorded out-of-lane sites
  (`cancel_auto_cont`'s self-killing pkill-under-suppress, now logged not
  swallowed; the tunnel/link socat cleanups, the untagged-socat one now
  asserted; the four `grep -v "$$"` nc-probe mirrors, now
  `_NC_LISTENER_PROBE`) fixed, and `test_bed_oracle_honesty.py`'s scan widened
  from the chaos lanes to all of `tests/` (t0 = exactly those 8; new stated
  blind spots: variable-bound patterns — inline the `argv_pattern` call — and
  exec-style list argv, which has no wrapper shell to self-match).
- **Bounded child reader** — `test_lifecycle_sync_phase.py`'s blocking
  `readline()` (and `_finish`'s unbounded `read()`) became a shared
  `_StdoutReader` over the raw fd (`select()` with remaining budget — the G6
  documented non-arm; the inline ast-grep suppression is retired and the G6
  rule note updated). A silent child is now a named failure at the reader
  budget instead of a 180s pytest-timeout wedge; pinned by a real
  silent-child leg. The budget is 60s, NOT the 20s first cut: it must
  outlast the child's own 30s graceful-teardown deadline net plus a
  heavy-load stall margin — a fully-loaded gate run caught a child stalled
  past 20s with an empty buffer (signals not yet handled), which the old
  unbounded reader absorbed invisibly; the buffered-output diagnostic now
  classifies any recurrence (a W16 follow-up: make the mixed-signal pin
  distinguish second-signal force from deadline force by elapsed time).

**The Wave-5a isolation defect, root-caused:** NOT an otto cache — a pytest 9
collection bug. Since pytest 8.4/9 conftest fixtures bind to the `Directory`
collector *node object*; `Session.collect` re-collects a parent dir with
`handle_dupes=False` when an argument's remaining parts are one file path,
replacing the cached report's child `Directory` nodes on EVERY level down to
a later argument's target — that argument then descends onto nodes the
fixture manager never saw, and each conftest on a rebuilt level silently
vanishes (autouse fixtures simply don't run; with the bare sibling in the
repo root, even `tests/conftest.py`'s process-global guards vanish). The
triple's middle file (bare sibling under `tests/unit`) is what forces the
rebuild; every pair passes. Fix at the source:
`tests/_fixtures/_conftest_rebind.py` — `pytest_collectstart` re-runs
`parsefactories` for a `Directory` node whose conftest is loaded, no longer
pending, and not bound to that node object — REGISTERED AS A PLUGIN from
`tests/conftest.py`'s `pytest_configure`, never re-exported as a conftest
hook (the opus interim review's MAJOR: `pytest_collectstart` is dispatched
through a path-filtered `ihook` proxy that strips conftest hookimpls for
non-anchor directories, so a conftest-hosted copy repaired anchor levels
while intermediate and root conftests stayed dropped). Pinned by
`tests/unit/test_conftest_directory_rebind.py` — a two-conftest-level
(intermediate + anchor) pytester-subprocess probe owning the argument shape,
whose no-plugin leg is the standing reproduction FOR BOTH LEVELS (if it ever
goes green, pytest fixed it upstream: delete the workaround). Real-tree spot
checks post-fix: the triple 129 passed; the intermediate-level shape keeps
all 330 `_no_ambient_webassets` setup-plan entries; the root-sibling shape
keeps both root guards on all 110 items.
**#108 lane answer: it could never have caught this** — `tests_unit_repeat`
passes a single directory argument (the one shape that can't trigger the
re-collect), and `--count` repeats items without re-collecting; a collection-
shape defect needs an in-suite pin over multi-file argument shapes, which the
rebind pin now is.

### Wave 15 — Web cannot-fail cluster (item 15; G14) — DONE
As landed (see G14 for the instruments and their measured t0 = 4 phantom ids +
17 bare-digit sites):
- All 17 bare-digit sites → labelled fragments ("4 contexts", "2 covered files",
  "5 lines · 2 covered", "Runs & captures (2)") or positional exact cells
  (`row.children[n]` `toBe`, the DirectoryPage house pattern) — the fixed shapes the
  rule deliberately does not match.
- `shell.test.tsx` tombstone deleted; `AppShell.test.tsx` menu test rewritten to a
  text-level absence pin inside the real open menu (`findByRole("menu")` proves it
  opened; `/PROJ-/` catches a regrown ticket row under ANY testid).
- `clock.test.tsx`: `NullTile` probe drives `useNow(null)`; a scheduled null interval
  now floods `nullRenders`. (No local `cleanup()` — vitest.setup.ts's global
  afterEach already unmounts, and its comment names this file's counters as the
  reason; a local registration was added and then reverted on opus review.)
- `seriestree.test.ts`: slot loop anti-vacuity via the 3-series element-tree cpu
  chart ([0,1,2]); no-repaint proven on a surviving NONZERO slot (search "sup" keeps
  only chassis-a_sup, slot 2).
- `commands.test.tsx`: both remount sites now assert the SAME rendered hook —
  the shape that catches a non-subscribing/stale-memo regression a fresh mount hides.
- `linkinspector.test.tsx`: fixture provenance "implicit" (the union has no
  "measured"; "declared" was indistinguishable from the `?? "declared"` fallback),
  which also discriminates the LINK read from `edge.provenance`; new test pins the
  fallback arm via a provenance-omitted link (rest-destructure —
  exactOptionalPropertyTypes rejects `provenance: undefined`).
- `reconnectingbanner.test.tsx`: third arm — live + "connecting" (store default and
  reconnect-in-progress) must show the banner.
Mutation battery: 14/14 killed, each by its named test (null-interval schedules;
non-subscribing theme read; provenance read dropped / fallback dropped;
banner only-on-disconnected; slots always-0; filterTree repaint; zero contexts;
CodeView cells reversed; covered/uncovered swapped; HitCell doubled; zero covered
files; disclosure zero; menu regrows a ticket row WITHOUT the old testid — the
differential proof that the phantom-id form could not see it).
Gates: `make lint-ts` + `make coverage-ts` + full gates before squash.

### Wave 16 — Timing-test hardening, unit tier (§3.3, §3.4, §3.6 residue)
`_feed_after_ready()` Event helper for the 65-site MockSession family (one helper in
the mock, mechanical migration); `test_session.py:482` gains a positive control
(assert the feed actually happened before asserting the verdict); the
`test_collector_run.py` floors move to exact counts on a virtual clock;
`test_connection_race.py` gains a contention positive-control; `test_console_lock.py`
asserts the readers reached the churn loop. **Added during Wave 5a (review §4.1's
first bullet, which no wave had claimed):** `tests/unit/cov/test_anchor.py:193,197,
202,207,212` — the five parity-only tests (`lazy == batched` is their only assertion,
so a resolver returning `unverifiable` for everything passes all five) each gain the
semantic outcome assert for their named scenario alongside parity. **Added during
Wave 5b:** sweep the pre-existing bare-field `match=` sites that pydantic's
`input_value=` echo renders blind (see G4's stated residual — test_settings.py:625
proven by mutation; :632 and test_settings_coverage.py's bare-field matches suspect)
to the two-halves `(?m)^<loc>\n\s+<reason>` form. **Effort: M.**

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
  G15 verified at landing: `finally_clause` + rule-object `stopBy` both stable; the
  draft's real gap was different — the fix idiom itself matches without a
  bare-vs-wrapped `not: inside: with_statement` arm (see G15).
