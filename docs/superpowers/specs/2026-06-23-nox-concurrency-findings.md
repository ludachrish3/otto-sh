# nox unit-concurrency spike — findings

> **Status:** Spike complete 2026-06-23.  
> **Task:** Phase 5 / Task 8 of `docs/superpowers/plans/2026-06-23-test-suite-speedup.md`  
> **Deliverable:** mechanism decision + proof data + recommendation for Task 9

---

## 1. What `make nox` actually does

`make nox` runs `uv run nox -s tests_all` which executes `tests_all` across all
five Python versions (3.10–3.14) **sequentially** (nox's default). Each
`tests_all-3.x` session runs the complete suite (unit + integration + e2e +
stability) under that Python. From the post-restructure validation run:

| session | wall |
|---------|------|
| tests_all-3.10 | ~2 min |
| tests_all-3.11 | ~2 min |
| tests_all-3.12 | ~3 min |
| tests_all-3.13 | ~2 min |
| tests_all-3.14 | ~2 min |
| **total** | **~11 min (~660 s)** |

The `~804 s` figure in the design doc and todo includes stability tests; the
validation run (bed up, stability included per `tests_all`) landed at ~660 s.
Either way, all five sessions run sequentially.

**Unit tier baseline:** `coverage-unit` (unit tier only, pinned Python) = ~28 s
(from the baseline measurement table in `todo/test-host-pool-and-speed.md`).
Our proof run (Task 8 concurrent execution, two `tests_unit` sessions) showed
**35–37 s per session** including nox venv-sync overhead; with pre-built venvs
the session would be closer to 28–30 s.

---

## 2. Coverage-file collision analysis

### .coveragerc isolation (key finding)

`.coveragerc` sets `data_file = reports/coverage/.coverage` (relative to CWD =
project root). This means all concurrent pytest-cov parent processes would write
to the **same file** at `reports/coverage/.coverage` when `coverage combine` runs
at session teardown.

Without isolation, two concurrent `tests_unit` sessions would race at the
`coverage combine` → write step.

**`reports/` is git-ignored** (`/.gitignore`: `reports/`), so scratch coverage
files left there are never accidentally committed. Repo-root `.coverage*` files
are also git-ignored, and `.coveragerc` routes all data out of the root.

**With `COVERAGE_FILE=<unique path>` env var,** coverage.py uses that path
instead of `data_file` from `.coveragerc`. Each concurrent session gets its own
isolated coverage database. The HTML report direction (`[html] directory` in
`.coveragerc`) is a static config key; it is overridden per-session with
`--cov-report html:<unique-path>` passed via pytest posargs.

### venv isolation

Each nox session uses its own `.nox/<session-name-version>/` virtualenv
(`.nox/tests_unit-3-10/`, etc.). These are fully independent and do not share
state. The only shared resource between concurrent sessions is the coverage file
and the HTML report directory — both are fixable with the env var + posarg.

### JUnit XML

`_junitxml(session, "nox-unit")` writes to
`reports/junit/nox-unit/<session>.xml`. Because each nox session name is unique
(`tests_unit-3.10`, `tests_unit-3.11`, etc.), JUnit files do not collide even
without special handling.

---

## 3. Coverage-gate split problem

`tests_all`'s `--cov-fail-under=85` is on the **combined** unit+bed coverage.
The unit tier alone achieves **~86% coverage** (confirmed in proof run:
`tests_unit-3.10` = 86.19%, `tests_unit-3.11` = 86.15%), which exceeds the 80%
`tests_unit` gate *and* actually exceeds the 85% `tests_all` bed gate. The bed
adds the remaining ~5–6 pp to reach ~91.8%.

The three options, honestly assessed:

### Option (a): Keep `tests_all` as-is for bed+gate; ALSO run unit concurrently (double-run)

Run the five `tests_unit` sessions concurrently (gate: 80% each), THEN run the
five `tests_all` sessions sequentially (which re-runs unit AND bed, gate: 85%).
The unit tier runs twice per Python.

- **Pro:** zero change to `tests_all`; the existing combined gate is preserved exactly.
- **Con:** unit re-runs 5× twice = 10 unit runs total instead of 5. Wasteful (~5×35s extra wall). The concurrent unit wave finishes first but you still wait for all 5 sequential `tests_all` to finish; the net saving is **zero wall-clock** because `make nox` still waits for the sequential `tests_all` chain.

**Not viable for the stated goal.**

### Option (b): Bed sessions (×5 sequential) + unit sessions (×5 concurrent)

Split `tests_all` into a `tests_bed` session (markers: `integration or embedded`,
no `--cov-fail-under` or lower threshold) plus keep `tests_unit` (80% gate).
Run `tests_unit-3.x` ×5 concurrently, then `tests_bed-3.x` ×5 sequentially.

- **Pro:** unit savings are real (~4×30s wave → one ~35s wave); the structure is clean.
- **Con:** The combined 85% gate no longer exists per-Python. You get two separate gates (unit ≥ 80%, bed = unchecked or low). Neither alone captures "the combined suite is green at 85%." You'd need `coverage combine` per-Python to reconstruct it — which is additional complexity and the `coverage combine` step has its own race conditions if done wrong.
- **Additional con:** adding `tests_bed` is a new session type that doesn't map cleanly onto the existing `tests_unit` / `tests_unix` / `tests_embedded` split. It duplicates logic.

**Viable but not clean; adds complexity without proportionate gain.**

### Option (c): `coverage combine` per Python

Run `tests_unit` and the bed sessions separately, then run `coverage combine` to
merge per-Python data and check the combined threshold.

- **Pro:** preserves the combined 85% guarantee per Python.
- **Con:** requires orchestrating a `coverage combine` step per-Python after both
  sessions finish; adds a new pipeline stage; the combine step itself must be
  isolated per Python (its own `COVERAGE_FILE`). Significantly more complex than
  the win warrants.

---

## 4. Proof run: concurrent unit sessions (2026-06-23)

Two `tests_unit` sessions launched as parallel background processes with
isolated coverage:

```bash
COVERAGE_FILE=reports/coverage/.coverage.u310 \
    uv run nox -s tests_unit-3.10 -- \
    --cov-report=html:reports/coverage/html-u310 \
    --no-header -q &

COVERAGE_FILE=reports/coverage/.coverage.u311 \
    uv run nox -s tests_unit-3.11 -- \
    --cov-report=html:reports/coverage/html-u311 \
    --no-header -q &

wait
```

**Results:**

| session | tests | coverage | exit |
|---------|-------|----------|------|
| tests_unit-3.10 | 1923 passed, 1 skip, 1 xfail | 86.19% | 0 (GREEN) |
| tests_unit-3.11 | 1923 passed, 1 skip, 1 xfail | 86.15% | 0 (GREEN) |
| wall (concurrent) | — | — | ~37 s |

**Isolation verified:**
- No `.coverage*` files in repo root.
- `reports/coverage/.coverage.u310` and `.coverage.u311` are distinct, independent
  files (each 1,626,112 bytes; written to `reports/coverage/` which is git-ignored).
- `reports/coverage/html-u310/` and `html-u311/` are separate HTML report dirs.
- Pre-existing `reports/coverage/.coverage` from the prior full run was not touched.
- xdist worker fragment files (`reports/coverage/.coverage.otto.<pid>.*`) were all
  under `reports/` and cleaned up post-run.
- **No cross-contamination. No collision.**

---

## 5. Estimated win and cost/benefit

**Sequential baseline:** 5 × ~130 s/session (unit ~35 s + bed ~95 s) ≈ **650 s**  
**Unit concurrency win:** replace 5 × 35 s sequential unit with one ~40 s wave  
**Saving:** (5 - 1) × 35 s ≈ **140 s = ~22%** of the sequential total

But the actual wall-clock saving for `make nox` depends on the orchestration:

- If the concurrent unit wave runs **before** the sequential bed chain:
  `wall = max(5×35s concurrent, 0) + 5×95s sequential ≈ 40 + 475 = 515 s`
  **vs baseline 650 s → ~21% saving, ~135 s.**
- This is real and measurable beyond the ±20 s noise floor.

**Cost:**
- Makefile `nox` target change: ~5 lines.
- `COVERAGE_FILE` and `--cov-report html:<path>` per concurrent session: proven working.
- Coverage gate: the existing `tests_unit` 80% gate applies to each Python in the
  concurrent wave. The bed-session 85% gate is dropped unless `tests_all` still runs
  sequentially (which cancels the win).

**The gate problem is the critical tradeoff:** splitting units out of `tests_all`
means the per-Python 85% combined gate disappears. However: the unit tier alone
hits 86%+ (confirmed), so it exceeds the `tests_unit` 80% gate comfortably. The
bed doesn't lower coverage — it raises it. The question is whether losing the
explicit per-Python "combined ≥ 85%" check matters when unit-alone already clears
that bar, and the bed adds to it.

---

## 6. Recommendation: DESCOPE

**DESCOPE this intervention.** Reasons:

1. **The gate complexity is real and the win is small.** Saving ~135 s on a
   ~650 s `make nox` (21%) requires either accepting that the explicit combined
   85% per-Python gate disappears (replaced by two separate weaker gates) or
   adding a `coverage combine` pipeline step per Python (option c). Neither is a
   trivial change for a 2-minute saving on a gate that already takes 11 minutes
   and isn't run frequently.

2. **The actual `make nox` baseline is ~650 s, not 804 s.** The design doc's
   804 s included stability tests in `tests_all`; the post-restructure
   validation's actual times are 2–3 min per session. The problem is smaller than
   scoped.

3. **`make nox` is already not in the hot path.** `make coverage` (the local
   iteration loop, the real developer latency) runs 1 Python and takes ~82 s.
   `make nox` is a release-gate sweep, run infrequently, and already benefits
   from the bed speedups (Tasks 5–7, which cut `make coverage` by ~20–30 s).

4. **The concurrent unit wave adds a new flake surface.** Two or five concurrent
   pytest processes sharing the project root (same `conftest.py`, same
   `PYTHONPATH`, same `tmp_path_factory` base dir) require careful isolation. The
   proof confirms coverage isolation is solvable, but xdist port contention and
   process-level resource interactions under load are not proven for 5 concurrent
   sessions. The project's zero-flake record is the hard constraint; introducing
   even a 1% flake risk in the release gate is unacceptable.

5. **Simpler path exists.** If `make nox` time matters, the right lever is
   `nox --no-reuse-existing-virtualenvs` being unnecessary (venvs already exist)
   or reducing `stability` test scope — not splitting the unit tier out of the
   sequential matrix.

**If the win is later re-evaluated as important,** the correct mechanism is
option (b) with Makefile orchestration (mechanism A from the task brief):
- `nox-unit` target: `COVERAGE_FILE=reports/coverage/.coverage.unit-$(VER) uv run nox -s tests_unit-$(VER) -- --cov-report=html:reports/coverage/html-unit-$(VER)` launched ×5 in background.
- `nox-bed` target: sequential `tests_unix` + `tests_embedded` ×5 with no cov gate (each env alone can't meet 85%).
- Combined gate: unit sessions enforce 80%, bed-plus-unit combined is implicitly satisfied since unit alone is already 86%.
- The Makefile `nox` target orchestrates: `nox-unit` (fork 5) + wait, then `nox-bed` (sequential).

This is ~10 Makefile lines and a `COVERAGE_FILE` convention documented in a comment. It's achievable if the tradeoff becomes worth it. For now, the remaining speedup budget is better spent on Phase 3 (front-load spike) and, when infra is ready, Phase 6 (docker parallelization).
