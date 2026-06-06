# `feature/embedded-host` merge-readiness: stability verification

**Date:** 2026-06-06
**Status:** Design — awaiting user review before the implementation plan.
**Branch:** `feature/embedded-host` (65 commits ahead of `main`, +18.3k/-1.6k).
**Goal in one line:** prove the test suite runs reliably with zero issues under
repeated execution, so the branch can be merged with confidence.

---

## 1. Goal & success criteria

Merge `feature/embedded-host` only after **every kind of test suite passes
under graduated repeat execution (`--count` 1 → 3 → 10)** with a genuinely
clean result.

A campaign **stage is GREEN** iff, across every tier it covers:

- **0 failures, 0 errors** in the JUnit reports, and
- no occurrence of the parked async ResourceWarning leak (eliminated by
  Workstream A — it is *fixed*, not tolerated), and
- no embedded console wedge at the **diluted** (`-n auto --dist loadgroup`)
  distribution (see §6 decision rule), and
- any flake that fires is root-caused and fixed (or consciously quarantined
  with sign-off) before the stage is declared green — see §7.

**Definition of done:** Stage 3 (`COUNT=10`) is GREEN across all tiers, the
async leak is fixed, and a per-stage evidence appendix (JUnit summaries) is
attached to the PR.

## 2. Current state (verified 2026-06-06)

A double-check of "the nox-all issues are addressed" against the run artifacts
in `reports/junit/` and the recent commits found **three distinct phenomena**,
not one:

| # | Phenomenon | Status | Evidence |
|---|-----------|--------|----------|
| 1 | `_console_access_lock` flock writer starvation (`Timeout (>30s)`) | **Fixed** | Writer-fair turnstile (`tests/integration/host/_console_lock.py`) + timeout-safe teardown net (`telnet.py`/`options.py`). The `contend_and_recover` test shows **0 failures** in both post-fix runs (was 8/Python). |
| 2 | Async ResourceWarning leak — unclosed `_UnixSelectorEventLoop` + 2 `AF_UNIX` sockets | **Parked → will fix (Workstream A)** | ~1-in-16,470 flake; needs full nox-all breadth to surface; `filterwarnings=["error"]` promotes it to a hard failure on a random test. |
| 3 | x86 in-guest telnet RST-churn wedge (`console wedged` / `shell never became ready`) | **Known bed limit — not fixed** | Only appears under **over-concentrated** `--count` hammering of the embedded integration file: 260+ errors/run, 100% on x86 telnet beds (`4.4-lfs`, `3.7-lfs`, `2.7-fat`, `3.7-fat`), **0 on ARM serial beds**. The representative nox-all run (diluted) had **0**. |

**Bed matrix is deliberately mixed.** The ARM migration was **downscoped**
(`no_fs` 3.7 → ARM `mps2_an385`; the other four contract hosts stay `qemu_x86`
to retain FS/version diversity — see `plans/2026-06-05-embedded-arm-bed-migration.md`).
So the x86 telnet beds are **permanent**; "finish the ARM migration" is **not**
an available remedy for phenomenon 3.

**Test→target coverage audit (no gaps).** Every directly-collected test lives
under `testpaths` (`tests/unit`, `tests/integration`). The five test files
outside `testpaths` (`tests/repo1/tests/*`, `tests/repo3/tests/*`) are
sample-project **fixtures** driven *indirectly* by the integration tier
(`tests/repo3/tests/test_embedded_coverage.py` runs inside
`tests/integration/test_embedded_coverage_e2e.py` via `otto test --cov`;
`repo1/tests/*` back the suite-plugin tests). **One real gap:**
`make stability-embedded` has no `COUNT`/`--count` knob, so it cannot be driven
×10 — closed by Workstream B1.

## 3. Strategy: graduated × tiered campaign

Two axes:

- **Graduated stages (gated):** `COUNT=1` (smoke) → `COUNT=3` → `COUNT=10`.
  Advance only when the prior stage is GREEN. Cheap suites fail fast on obvious
  gotchas before the expensive hammering.
- **Tiers (breadth vs cost):** the full lab suite dominates wall time
  (~22-24 min/Python at `COUNT=1`; ×10 ≈ ~4 h/Python, ~20 h across all five).
  So breadth is reserved for the cheap suites and depth for one pinned Python.

| Tier | Target | Breadth | VMs |
|------|--------|---------|-----|
| T1 unit | `make nox` | all 5 Pythons, every stage | no |
| T2 full lab — breadth | `make nox-all` (all 5 Py) | **Stage 1 only** (cross-version health + surfaces the leak) | yes |
| T2 full lab — deep | `nox -s tests_all-3.10 -- --count=N` | **pinned 3.10**, every stage | yes |
| T3a concurrency | `make stability` | every stage | no |
| T3b real telnet/SSH | `make stability-all` (incl. embedded tier3) | every stage | yes |
| T3c embedded contract | `make stability-embedded COUNT=N` (B1) | every stage | yes |

**Pinned Python = 3.10** (oldest supported floor — maximizes the chance of
catching version-floor regressions under the deep hammering).

Note T3b `stability-all` already chains `stability` (tier1) and
`stability-embedded` (tier3); B1 also threads `COUNT` from `stability-all`
into the embedded tier so the whole chain scales together.

## 4. Workstream A — fix the async ResourceWarning leak (prerequisite)

The chosen path is **fix it first** (not document/tolerate). Approach:

- Reproduce under xdist + embedded with `tracemalloc` + `-W error::ResourceWarning`,
  narrowing to the connection path (memory: connection-specific; suspects are an
  asyncio loop / `AF_UNIX` socketpair — self-pipe or subprocess transport —
  created without `close()`).
- Drive with the `systematic-debugging` skill; confirm the allocation site
  before patching (root-cause-first).
- **Gate:** must be fixed before Stage 3 is declared GREEN. Develop it in
  parallel with the Stage 1/2 smoke runs (those provide the breadth that
  surfaces it).
- **Verify:** the repro that previously surfaced it (full nox-all breadth, or
  the embedded + SNMP integration subset) runs leak-free across repeats with
  `filterwarnings=["error"]` intact and `tracemalloc` off.

## 5. Workstream B — test-infra

- **B1 — `COUNT` knob on `stability-embedded`** (confirmed required): thread
  `--count=$(or $(COUNT),1) -p no:cacheprovider`, mirroring `stability`/`stability-all`;
  and have `stability-all`'s tier-3 call pass `COUNT` through.
- **B2 — campaign runner + aggregator:** a thin script that drives the
  graduated stages across tiers, writes per-session JUnit to `reports/junit/`,
  and aggregates via `scripts/junit_failures.py` into a per-stage pass/flake
  report. The report is reused verbatim as the PR evidence appendix. It encodes
  the stage gating (stop on a dirty stage) and the tier breadth rules from §3.

## 6. Workstream C — run the campaign

Execute the §3 matrix stage by stage; after each stage, the aggregator emits a
GREEN/dirty verdict and the next stage runs only on GREEN.

**Embedded diluted-wedge decision rule** (operationalizes "measure diluted
first"): the full-suite tiers run at the native `-n auto --dist loadgroup`
distribution, which keeps embedded tests interleaved among thousands of others
(the condition under which the representative run had 0 wedges).

- **0 wedges at `COUNT=10` diluted ⇒ goal met** for the embedded tier.
- **Wedges appear at diluted `COUNT=N` ⇒** revisit the embedded reconciliation
  (ARM migration is *off the table*): scope the ×10 stress to wedge-free beds,
  add bed self-recovery (auto `qemu-restart` / reconnect backoff between reps),
  or document as an accepted known-limit. Decide with the user at that point.

## 7. Workstream D — flake watch-items

These known intermittents are in the campaign blast radius and must be resolved
(or quarantined with sign-off) before the relevant stage is GREEN:

- **`test_otto_suite.py::TestOttoTestDir::test_test_dir_created_per_test`** —
  intermittent on Python 3.12 (passes in isolation; ~1-in-3 full runs). The T1
  unit tier runs 3.12 ×10, so it will likely fire. Investigation plan already
  recorded in `todo/test_otto_suite_3_12_flake.md` (prime fix: pin the
  inner-`pytest.main()` tests to one worker via `xdist_group`).

## 8. Workstream E — repository hygiene (largely done this session)

Done: deleted resolved/stale `command_frame_protocol.md`, `embedded_coverage.md`,
the untracked full-transition ARM spec (`2026-06-04-embedded-arm-migration-design.md`),
the untracked completed Track-A plan (`2026-06-04-embedded-toolchain-unification.md`),
and the stale `todo/embedded_cortex_m_migration.md` tracker; removed the dead
`nc_monitor_retirement.md` link from `todo/TODO.md`; repaired the deleted-triage
link in the kept console-lock-fairness spec. Kept (deliberately):
`todo/TODO.md`, `todo/test_otto_suite_3_12_flake.md`, `todo/gcno_mismatch_error.md`,
and the current `plans/2026-06-05-embedded-arm-bed-migration.md`.

## 9. Sequencing

1. **B1** (COUNT knob) + finish **E** (hygiene) — quick, parallel.
2. **B2** (campaign runner) — needed before the campaign.
3. **Stage 1 (`COUNT=1`)** smoke across all tiers — catch gotchas; expect the
   async leak in the nox-all breadth pass.
4. **Workstream A** (leak fix) in parallel, using the Stage-1 breadth runs.
5. Graduate to **Stage 2 (`COUNT=3`)**, then **Stage 3 (`COUNT=10`)** once the
   leak is fixed and the prior stage is GREEN.
6. Assemble the PR with the evidence appendix; hand off to
   `finishing-a-development-branch`.

## 10. Non-goals

- ARM migration Track B / "fixing" the x86 telnet wedge — out unless §6 forces
  the conversation.
- Net-new test coverage beyond closing the B1 gap.
- Changing CI gating (`ci.yml` stays unit-only; the lab campaign is local). A
  follow-up could automate the campaign in a dispatchable nightly, but that is
  not in this scope.

## 11. Environment guardrails & risks

- **Dev VM is the only copy** — run in-place, no destructive probes; confine any
  scratch work to `tmp_path`.
- **Don't kill live-bed runs at tight timeouts** — SIGTERM wedges single-client
  consoles and poisons the next run; let runs finish or `make qemu-restart`.
- **Recover wedged beds** with `make qemu-restart` between stages.
- **No self-commit** — the `prepare-commit-msg` hook needs `/dev/tty`; each step
  yields a paste-able commit message for the user to run.
- **Cost risk:** the full ×10 campaign is many lab-hours; the tiered design
  bounds it to ~one pinned Python deep. Run the deep stages in the background /
  overnight.
- **Lab-contention risk:** another agent's worktree using the lab widens the
  embedded timing windows; confirm the lab is otherwise idle during deep stages.
