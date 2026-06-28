# Strict Linting — Phase 2 (Bug/Simplify Batch) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adopt the ruff `B` (bugbear), `C4` (comprehensions), `SIM` (simplify), `PIE`, and `FLY` rule families — clear the 83 remaining violations and remove those families from the TEMP ignore block so the gate enforces them.

**Architecture:** Ratchet step on the strict-linting roadmap ([spec](../specs/2026-06-27-strict-linting-design.md)). Phase 1a already applied every *safe* autofix, so what remains for these families is **45 unsafe-auto-fixable + 38 manual** (26 of which are bug-class judgment). Two tasks split by reviewer boundary: (1) style simplifications (SIM/C4/PIE — ruff `--unsafe-fixes` + manual residual), (2) bug-class `B` rules (per-site judgment, correctness-relevant). Each task fixes its sites, removes its codes from the TEMP ignore, and proves the full gate green. `FLY` has **0** violations and is not in the ignore — nothing to do there.

**Tech Stack:** ruff 0.15.x, ty, nox/make, Python 3.10 floor.

## Global Constraints

- **Stage-only — do NOT commit.** Leave changes UNSTAGED; the maintainer commits. (The "Commit" steps are written for completeness.)
- **`make typecheck` is MANDATORY in every task's verification** (not just ruff + unit + docs). Phase 1 proved formatting/lint changes are *not* type-checker-neutral; a fix can move a `# ty: ignore` or re-expose a ty error.
- **Behavior-preserving is NOT free here.** `B006` and `B905` fixes can change runtime behavior — each MUST be verified by `make coverage-unit` (full unit suite) and judged per-site.
- **Python floor 3.10**; line-length 100; quote-style double; no `from __future__ import annotations`; no `# type: ignore`→ keep using `# ty: ignore[code]` if a suppression is unavoidable.
- **Never use `--unsafe-fixes` blindly** — apply, then read the diff and run the full no-VM gate; an unsafe fix that changes a test's meaning or a runtime path is a finding, not a pass.
- **Each task ends green on the FULL gate:** `ruff check .` (with the family un-ignored), `make typecheck`, `make coverage-unit` (2061+ passed, ≥85%), `make docs` (0 warnings).
- **Scoped measurement command** (to see a family's violations while it's still TEMP-ignored): `uv run --no-sync ruff check src tests --select <CODES> --config 'lint.ignore = []'`.
- **Un-ignore mechanism:** delete the relevant rule codes from the `# ===== TEMP (ratchet) =====` block in `.ruff.toml` (NOT from the permanent deny-list groups above it).

---

### Task 1: Adopt `SIM` + `C4` + `PIE` (style simplifications)

**Files (modify — the 57 sites):** across `src/otto/` (session.py, interact.py, connections.py, completion_cache.py, console.py, collector.py, local_host.py, transfer/console.py, coverage/correlator/paths.py, configmodule/__init__.py, suite/suite.py, host/interact.py) and `tests/` (conftest.py, e2e/host/_pty_driver.py, several unit/integration test files). Plus `.ruff.toml` (remove SIM*/C408/PIE804 from TEMP).

**Interfaces:**
- Consumes: the green strict config from Phase 1.
- Produces: `SIM`, `C4`, `PIE` removed from the TEMP ignore; `ruff check .` green with them enforced.

- [ ] **Step 1: See the current SIM/C4/PIE debt**

Run: `uv run --no-sync ruff check src tests --select SIM,C4,PIE --config 'lint.ignore = []' --statistics`
Expected: ~60 violations — dominated by `SIM105` (33, suppressible-exception), `SIM117` (12, multiple-with), `SIM108` (5), `C408` (2), plus single `SIM102/115/118/222`, `PIE804`.

- [ ] **Step 2: Apply ruff's unsafe fixes for the auto-fixable subset**

Run: `uv run --no-sync ruff check src tests --select SIM,C4,PIE --config 'lint.ignore = []' --fix --unsafe-fixes`
This rewrites the mechanical ones — primarily `SIM105` (`try/except X: pass` → `with contextlib.suppress(X):`, adding `import contextlib`), `SIM117` (nested `with a:`/`with b:` → `with a, b:`), `C408` (`dict()` → `{}`), `SIM118` (`x in d.keys()` → `x in d`), `PIE804`.
Then re-format: `uv run --no-sync ruff format .`

- [ ] **Step 3: Read the diff and sanity-check the unsafe fixes**

Run: `git diff --stat` then spot-read the `SIM105`→`contextlib.suppress` conversions (esp. in `src/otto/host/session.py`, `src/otto/host/interact.py`) and the `SIM117` combined-`with` in `tests/unit/host/test_unix_host.py`.
Expected: each `contextlib.suppress(X)` wraps exactly what the old `try/except X: pass` did (same exception type, same suppressed body); combined `with` statements preserve order and bodies. If any conversion changed the suppressed exception type or merged unrelated bodies, revert that hunk and fix by hand.

- [ ] **Step 4: Manually fix the remaining SIM residual**

These have no autofix (or were left unfixed). Apply per-rule:

- `SIM115` — `src/otto/configmodule/completion_cache.py:463`: an `open()` not wrapped in `with`. Convert to a context manager:
  ```python
  with open(path, ...) as f:
      ...   # use f
  ```
  If the handle deliberately outlives the block (stored on an object), this is a false positive — add `# noqa: SIM115` with a one-line reason instead.
- `SIM108` (5: `completion_cache.py:676`, `coverage/correlator/paths.py:113`, `host/local_host.py:209`, `transfer/console.py:342`, `tests/unit/host/test_zephyr.py:86`) — collapse `if/else` that only assigns one variable into a ternary:
  ```python
  x = a if cond else b
  ```
  Only when it stays readable at ≤100 cols; if the ternary would be cramped, keep the if/else and `# noqa: SIM108` with a reason.
- `SIM102` (`tests/unit/test_tier_marker_invariants.py:42`) — collapse nested `if a: if b:` → `if a and b:`.
- `SIM222` (`tests/unit/docker/test_compose.py:163`) — an `... or True` that makes the expression constant; read the intent and simplify (often the `or True` is dead — remove it, or fix the real condition).

- [ ] **Step 5: Verify the families are clean, then un-ignore them**

Run: `uv run --no-sync ruff check src tests --select SIM,C4,PIE --config 'lint.ignore = []'`
Expected: `All checks passed!` (0 remaining).

Now remove the now-clean codes from the TEMP block in `.ruff.toml`. Find them:
Run: `grep -nE '"(SIM|C4|C408|PIE)[0-9]*"' .ruff.toml`
Delete every matched line that sits **inside the `# ===== TEMP (ratchet) =====` block** (do not touch the permanent deny-list).

- [ ] **Step 6: Full-gate verification (families now enforced)**

Run each; all must pass:
- `uv run --no-sync ruff check .` → `All checks passed!`
- `uv run --no-sync ruff format --check .` → all formatted
- `make typecheck` → `All checks passed!`
- `make coverage-unit` → 2061+ passed, ≥85%
- `make docs` → 0 warnings

If `make coverage-unit` shows a NEW failure, an unsafe fix changed behavior — bisect to the hunk and fix/revert it.

- [ ] **Step 7: Commit (stage-only — hand message to maintainer)**

```bash
git add .ruff.toml src tests
git commit -m "style(lint): adopt SIM/C4/PIE; remove from TEMP ratchet (Phase 2a)"
```

---

### Task 2: Adopt `B` (bugbear — bug-class, per-site judgment)

**Files (modify — the 26 sites):** `src/otto/cli/cov.py`, `src/otto/cli/main.py`, `src/otto/cli/docker.py`, `src/otto/cli/host.py`, `src/otto/host/transfer/nc.py`, `src/otto/monitor/collector.py`, `src/otto/context.py`, and tests (`tests/unit/cli/test_param_synth.py`, `tests/unit/configmodule/test_completion_cache_unit.py`, `tests/unit/configmodule/test_completion_stubs.py`, `tests/e2e/cov/test_coverage_e2e.py`, `tests/integration/host/test_embedded_host_integration.py`, `tests/integration/host/test_unix_host_integration.py`, `tests/unit/host/test_command_frame.py`, `tests/unit/host/test_shell_command.py`, `tests/e2e/host/_pty_driver.py`, `tests/unit/host/test_unix_host.py`, `tests/unit/monitor/test_monitor_import_export.py`). Plus `.ruff.toml` (remove B-codes from TEMP).

**Interfaces:**
- Consumes: the SIM/C4/PIE-clean tree from Task 1.
- Produces: `B` removed from the TEMP ignore; `ruff check .` green with bugbear enforced.

- [ ] **Step 1: See the current B debt**

Run: `uv run --no-sync ruff check src tests --select B --config 'lint.ignore = []' --statistics`
Expected: ~26 — `B905` (9), `B006` (5), `B904` (5), `B017` (3), `B007` (2), `B018` (2).

- [ ] **Step 2: Fix `B905` zip-without-explicit-strict (9 — correctness judgment)**

Sites: `src/otto/context.py:165`, `src/otto/monitor/collector.py:382,407,431`, `tests/integration/host/test_embedded_host_integration.py:412,451,500`, `tests/unit/monitor/test_monitor_import_export.py:133,145`.

For each `zip(a, b)`, read the operands and add `strict=`:
```python
zip(a, b, strict=True)    # operands are guaranteed equal length (the common case — catches a real bug if they ever differ)
zip(a, b, strict=False)   # truncation to the shorter is INTENTIONAL (rare — only with a comment justifying it)
```
Default to `strict=True`. Use `strict=False` only when truncation is clearly intended; if `strict=True` makes a test fail, that test was relying on silent truncation — investigate before flipping to `False`.

- [ ] **Step 3: Fix `B006` mutable-argument-default (5)**

Sites: `src/otto/cli/cov.py:154`, `src/otto/cli/main.py:153`, `tests/unit/cli/test_param_synth.py:118`, `tests/unit/configmodule/test_completion_cache_unit.py:95`, `tests/unit/configmodule/test_completion_stubs.py:73`.

Replace the mutable default with a `None` sentinel:
```python
def f(items: list[str] | None = None) -> ...:
    if items is None:
        items = []
    ...
```
(For a dict default, `items: dict | None = None` + `if items is None: items = {}`.) This is behavior-preserving for callers that relied on the default being fresh each call.

- [ ] **Step 4: Fix `B904` raise-without-from-inside-except (5)**

Sites: `src/otto/cli/host.py:102`, `src/otto/host/transfer/nc.py:143`, `src/otto/monitor/collector.py:212`, `tests/e2e/host/_pty_driver.py:192`, `tests/unit/host/test_unix_host.py:341`.

Inside an `except E as err:` block, add a cause to the re-raise:
```python
raise SomeError(...) from err     # when the original exception is relevant context
raise SomeError(...) from None     # when deliberately replacing it (hide the original)
```
Pick `from err` by default; `from None` only when the original traceback is noise.

- [ ] **Step 5: Fix `B017` / `B018` / `B007` (test/loop hygiene)**

- `B017` assert-raises-exception (3: `tests/integration/host/test_embedded_host_integration.py:219`, `tests/integration/host/test_unix_host_integration.py:375`, `tests/unit/host/test_command_frame.py:37`) — `pytest.raises(Exception)` is too broad; narrow to the specific exception the code under test raises:
  ```python
  with pytest.raises(TimeoutError):   # or the actual concrete type
      ...
  ```
  If the test genuinely needs to accept any exception, add `# noqa: B017` with a reason.
- `B018` useless-expression (2: `tests/unit/host/test_shell_command.py:57,64`) — a bare expression statement. If it's asserting that an attribute access / property doesn't raise, make the intent explicit (`assert obj.attr is not None` or `_ = obj.attr`); otherwise delete the dead line.
- `B007` unused-loop-control-variable (2: `src/otto/cli/docker.py:211`, `tests/e2e/cov/test_coverage_e2e.py:422`) — rename the unused loop var to `_` (or `_name` if a name aids readability): `for _ in items:`.

- [ ] **Step 6: Verify B is clean, then un-ignore it**

Run: `uv run --no-sync ruff check src tests --select B --config 'lint.ignore = []'`
Expected: `All checks passed!`

Remove the B-codes from the TEMP block:
Run: `grep -nE '"B[0-9]+"' .ruff.toml`
Delete every matched line **inside the `# ===== TEMP (ratchet) =====` block** (leave the permanent deny-list untouched — note `B` has no codes in the permanent groups, so all matches are TEMP).

- [ ] **Step 7: Full-gate verification (bugbear now enforced)**

All must pass:
- `uv run --no-sync ruff check .` → `All checks passed!`
- `uv run --no-sync ruff format --check .` → all formatted
- `make typecheck` → `All checks passed!`
- `make coverage-unit` → 2061+ passed, ≥85% (this is the real safety net for the B006/B905 behavior changes)
- `make docs` → 0 warnings

- [ ] **Step 8: Commit (stage-only — hand message to maintainer)**

```bash
git add .ruff.toml src tests
git commit -m "fix(lint): adopt bugbear B (zip-strict, mutable-defaults, raise-from); remove from TEMP (Phase 2b)"
```

---

## Self-Review notes

- **Spec coverage:** Phase 2 row of the roadmap = "un-ignore `B, C4, SIM, PIE, FLY`". Task 1 covers SIM/C4/PIE; Task 2 covers B; FLY has 0 violations and is already enforced (noted, not a task). ✅
- **No blind autofix:** Step 3 of Task 1 explicitly reads the unsafe-fix diff before accepting it; the bug-class B rules are all hand-fixed with per-site judgment.
- **Behavior-change safety:** B006/B905 are flagged as behavior-affecting; both tasks run `make coverage-unit` as the safety net, and `make typecheck` is mandatory per the Phase-1 lesson.
- **No placeholders:** every rule has its concrete fix pattern + the enumerated sites + a judgment criterion for the false-positive case (`# noqa: <code>` with reason as the escape hatch, used sparingly).
- **Un-ignore hygiene:** both tasks remove only TEMP-block codes, never the permanent deny-list; each ends on the full gate with the family enforced.
- **Ordering:** Task 1 (style) before Task 2 (bug-class) so the bug-class diff reviews against an already-simplified tree; either order is gate-safe since each un-ignores only its own families.
