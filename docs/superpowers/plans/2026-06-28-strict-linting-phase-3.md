# Strict-Linting Phase 3 — Modernize (UP / RUF / PERF) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clear the `UP` (minus the permanently-denied UP037), `RUF`, and `PERF` ratchet debt — adopting the rules that find real problems, denying/exempting the ones that fight otto's deliberate patterns, and fixing the rest.

**Architecture:** Every Phase-3 code is FIXED (not denied/exempted) — Chris's call is to keep each rule enforced and fix sites properly, reserving the per-site `# noqa` escape hatch only where the rule is a genuine false positive (PERF203 per-item resilience, UP045 runtime construction). Each task fixes its sites, then removes the now-clean codes from the `# ===== TEMP (ratchet) =====` block. No deny-list or per-file-ignore changes this phase. Every task lands the whole gate green. STAGE-ONLY — never commit (Chris commits).

**Tech Stack:** ruff 0.15.x (lint+format), ty 0.0.55 (`all = "error"`), pydantic v2 models, pytest+xdist, nox (py3.10–3.14).

## Global Constraints

- **STAGE-ONLY.** Agents never `git commit` (prepare-commit-msg needs /dev/tty). Stage with `git add`; Chris commits.
- **`ruff check .` is authoritative** — it covers `scripts/` and `docs/` too; the scoped `--select X src tests` form MISSES them. Stage `.ruff.toml src tests scripts docs` as touched.
- **The whole gate is the oracle, incl. `make typecheck`.** `ruff format`/autofix is NOT type-checker-neutral. After any code change run `make typecheck` AND `ruff format --check .` AND `uv run ruff check .`. For behavior-touching src edits also run `make coverage-unit` (85% floor, no-VM).
- **Never run blanket `--fix --unsafe-fixes`.** All remaining Phase-3 fixes are unsafe-fix or manual — apply per-site with judgment, matching surrounding code. (A blanket UP `--fix` would unquote 167 UP037 annotations and break 3.10.)
- **Python 3.10 floor + Sphinx-nitpicky (`-W`) docs gate are HARD.** Never introduce `from __future__ import annotations`, `TYPE_CHECKING`-hidden imports, or unquoted forward refs.
- **Ratchet rule:** `select = ["ALL"]` is permanent. Remove a code from the TEMP block ONLY after its sites are clean (or it's moved to the permanent deny-list / per-file-ignores). Verify `uv run ruff check . --select <CODE> --config 'lint.ignore=[]'` reflects the intended end-state before deleting from TEMP.

**Phase-3 codes leaving TEMP (all 12):** `RUF012`, `PERF203`, `RUF006`, `RUF059`, `RUF005`, `RUF015`, `PERF102`, `PERF401`, `RUF002`, `RUF003`, `RUF043`, `UP045`. End-state: none of these remain in TEMP. (`UP037` stays in the permanent deny-list — untouched.)

---

## Task 1: RUF012 — pydantic `default_factory` + test ClassVar fixes

**Why:** Keep RUF012 (mutable-class-default) ENFORCED everywhere (no exemption). On pydantic v2 `BaseModel` subclasses, the idiomatic fix for a mutable-literal field default is `Field(default_factory=...)` — semantically identical (pydantic already per-instance-copies), satisfies the rule cleanly, and keeps the models package checked for any future genuinely-shared ClassVar mistake. The 4 non-model hits are read-only test-class constants → genuine `ClassVar`.

**Context:** All 28 flagged model classes subclass `OttoModel(BaseModel)` (verified — pure BaseModel, NOT frozen dataclasses), so `Field(default_factory=...)` has no positional-arg concerns. `Field` is imported from `pydantic`.

**Files:**
- Modify: `.ruff.toml` (remove `RUF012` from TEMP — no per-file-ignore)
- Modify: `src/otto/models/settings.py` (12 sites), `src/otto/models/options.py` (8 sites), `src/otto/models/host.py` (8 sites)
- Modify: `tests/unit/host/test_power.py:24` (`_FakeLab.hosts`)
- Modify: `tests/unit/cli/test_dynamic_host_commands.py:646,653` (`_CompletionCtx.params`, `_DispatchCtx.params`)
- Modify: `tests/e2e/cov/test_coverage_e2e.py:278` (`MATH_OPS_EXPECTED`)

**Interfaces:** Model public API unchanged — defaults are semantically identical (empty container per instance). `to_runtime()` outputs unchanged.

- [ ] **Step 1: Convert pydantic mutable-literal field defaults to `Field(default_factory=...)`.** At each of the 28 model sites: `= []` → `= Field(default_factory=list)`; `= {}` → `= Field(default_factory=dict)`; `= set()` → `= Field(default_factory=set)`. For a NON-empty literal default (e.g. `= {"a": 1}`), use `= Field(default_factory=lambda: {"a": 1})`. Leave tuple defaults (`= ()`) alone — they're immutable and not flagged. Ensure `from pydantic import Field` is present in each file (add to the existing pydantic import). Do NOT touch `model_config` (it's a `ConfigDict(...)` call, not a literal — not flagged).

- [ ] **Step 2: Annotate the 4 test constants as `ClassVar`.** Each is read-only fixture data; add `from typing import ClassVar` to the file if missing, then:
  - `test_power.py:24` → `hosts: ClassVar = {"hyp": runner}`
  - `test_dynamic_host_commands.py:646` → `params: ClassVar = {"host_id": "u1"}` (in `_CompletionCtx`)
  - `test_dynamic_host_commands.py:653` → `params: ClassVar = {"host_id": "u1"}` (in `_DispatchCtx`)
  - `test_coverage_e2e.py:278` → `MATH_OPS_EXPECTED: ClassVar = {` (keep the dict body unchanged)

- [ ] **Step 3: Remove `RUF012` from the TEMP block** in `.ruff.toml`.

- [ ] **Step 4: Verify RUF012 is clean, models still validate, gate holds.**
  ```bash
  uv run ruff check . --select RUF012 --config 'lint.ignore=[]'   # expect 0 (enforced everywhere)
  uv run ruff check .                                              # expect green
  uv run ruff format --check .
  make typecheck
  uv run pytest tests/unit/models -q                              # model defaults still behave
  ```
  Expected: all clean. (Models are runtime-touched → run the model unit tests + `make typecheck`.)

- [ ] **Step 5: Stage** `git add .ruff.toml src/otto/models/settings.py src/otto/models/options.py src/otto/models/host.py tests/unit/host/test_power.py tests/unit/cli/test_dynamic_host_commands.py tests/e2e/cov/test_coverage_e2e.py`

---

## Task 2: RUF006 — store + await/cancel the 44 dangling test tasks

**Why:** RUF006 (asyncio-dangling-task) flags `asyncio.create_task(...)` whose result isn't stored — the task can be GC'd mid-flight (Python keeps only a weak ref). Chris's call is to FIX, not exempt: storing the reference and awaiting/cancelling it is more correct and may help the parked "unclosed event loop" async-leak. All 44 are in async test methods spinning up a `simulate()` coroutine that feeds a mock session before an `await session.run_cmd(...)`.

**Files (all tests):**
- `tests/unit/host/test_session.py` (27 sites)
- `tests/unit/host/test_zephyr.py` (10 sites)
- `tests/unit/host/test_session_output_buffering.py` (5 sites)
- `tests/unit/host/test_session_logging.py` (2 sites)
- Modify: `.ruff.toml` (remove `RUF006` from TEMP)

**Interfaces:** none (test-internal).

- [ ] **Step 1: Store and clean up each task.** The canonical pattern is a fire-and-forget feeder consumed by the immediately-following `await`:
  ```python
  _feed = asyncio.create_task(simulate())
  result = await session.run_cmd("echo hello world")
  ```
  Rewrite each site to capture the task and ensure it finishes (it completes during the `await` below it, so awaiting it is safe and non-blocking):
  ```python
  feed_task = asyncio.create_task(simulate())
  result = await session.run_cmd("echo hello world")
  await feed_task   # task already done; surfaces any exception and clears the dangling-task warning
  ```
  If a site's task is NOT guaranteed to complete (e.g. an infinite feeder loop used only as a backdrop), instead store it and cancel at the end of the test with `feed_task.cancel()` + `with contextlib.suppress(asyncio.CancelledError): await feed_task`. Inspect each site; prefer `await` when the coroutine self-terminates (the common case here), `cancel` only for non-terminating backdrops. Use a distinct local name per task if a test creates several.

- [ ] **Step 2: Remove `RUF006` from the TEMP block.**

- [ ] **Step 3: Verify — clean, and the async tests still pass with identical timing/behavior.**
  ```bash
  uv run ruff check . --select RUF006 --config 'lint.ignore=[]'   # expect 0 (enforced everywhere)
  uv run ruff check .
  make typecheck
  uv run pytest tests/unit/host/test_session.py tests/unit/host/test_zephyr.py tests/unit/host/test_session_output_buffering.py tests/unit/host/test_session_logging.py -q
  ```
  Expected: ruff/ty clean; all four test modules green. Watch for new "Task was destroyed but it is pending" warnings or hangs — if `await feed_task` blocks, that site needs `cancel` instead.

- [ ] **Step 4: Stage** `git add .ruff.toml tests/unit/host/test_session.py tests/unit/host/test_zephyr.py tests/unit/host/test_session_output_buffering.py tests/unit/host/test_session_logging.py`

---

## Task 3: Mechanical expression rewrites + PERF203 noqa

**Why:** Behavior-preserving rewrites the formatter/autofix left as unsafe-fix (RUF059/RUF005/RUF015/PERF102/PERF401), plus PERF203 — which Chris's call is to keep enforced and silence per-site (the try/except-in-loop is the deliberate continue-on-per-item-failure pattern; the implied hoist-out-of-loop fix would change semantics). Apply per-site, verifying each diff.

**PERF203 (11 src sites — add `# noqa: PERF203 — per-item resilience` at each):** `src/otto/context.py:170`; `src/otto/host/repeat.py:88`; `src/otto/host/telnet.py:82,94`; `src/otto/host/transfer/nc.py:141,395`; `src/otto/models/host.py:164`; `src/otto/storage/json_repository.py:69,81`; `src/otto/suite/plugin.py:256`; `src/otto/suite/suite.py:379`. (The noqa goes on the line ruff reports — the `try` statement line.)

**Files (exact sites):**
- `RUF059` (rename unused unpack target → `_`): `tests/integration/host/test_unix_host_integration.py:335`; `tests/unit/docker/test_build.py:107`; `tests/unit/host/test_embedded_host.py:513`; `tests/unit/host/test_embedded_transfer.py:261,529`; `tests/unit/host/test_hop.py:604(×2),672(×2)`; `tests/unit/host/test_local_host.py:211,227`; `tests/unit/host/test_product.py:46`; `tests/unit/host/test_transfer_progress.py:188`; `tests/unit/host/test_unix_host.py:681,692,706,719,736,1560`
- `PERF401` (append-loop → comprehension / `.extend`): `src/otto/cli/main.py:91`; `src/otto/coverage/renderer/html_renderer.py:228`; `src/otto/coverage/reporter.py:159`; `src/otto/host/transfer/unix_base.py:70`; `src/otto/monitor/collector.py:782`; `src/otto/reservations/json_backend.py:93`; `tests/unit/test_tier_marker_invariants.py:56`
- `PERF102` (`.items()` → `.values()`/`.keys()`): `src/otto/cli/docker.py:211`; `tests/e2e/cov/test_coverage_e2e.py:422`
- `RUF005` (concat → unpacking): `src/otto/cli/test.py:298`; `src/otto/configmodule/repo.py:300`; `tests/unit/cli/test_main.py:70`
- `RUF015` (`...[0]` → `next(iter(...))`): `tests/integration/host/test_unix_host_integration.py:363`; `tests/unit/cov/test_model.py:214`
- Modify: `.ruff.toml` (remove the 5 codes from TEMP)

- [ ] **Step 1: RUF059** — at each site, replace the unused unpacked name with `_` (or `_name` if `_` collides with another target on the same line). Where two names on one line are both unused (`test_hop.py:604,672`), use `_, _`. Verify intent: the variable is genuinely never read afterward.

- [ ] **Step 2: PERF401** — read each loop; if it builds a fresh list, convert to a list comprehension; if it appends onto an existing list, use `target.extend(<comprehension>)`. Preserve any filtering/conditionals. Do NOT change behavior for loops with side effects beyond the append (if a loop body does more than build the list, leave it and add `# noqa: PERF401 — loop body has side effects`).

- [ ] **Step 3: PERF102** — confirm only values (or only keys) are used in the loop body, then switch `.items()` → `.values()` (or `.keys()`), dropping the unused unpack target.

- [ ] **Step 4: RUF005** — rewrite `a + [x]` → `[*a, x]` and `[x] + a` → `[x, *a]` per the ruff suggestion text.

- [ ] **Step 5: RUF015** — rewrite `list(x)[0]` / `x.keys()[0]`-style single-element slices to `next(iter(x))` (drop redundant `.keys()`).

- [ ] **Step 6: PERF203** — at each of the 11 sites listed above, append `  # noqa: PERF203 — per-item resilience` to the reported `try:` line. Do NOT restructure the loop. (Verify the noqa lands on the exact line ruff reports; the column in the report is the `try`.)

- [ ] **Step 7: Remove `RUF059`, `PERF401`, `PERF102`, `RUF005`, `RUF015`, `PERF203` from the TEMP block.**

- [ ] **Step 8: Verify.**
  ```bash
  uv run ruff check . --select RUF059,PERF401,PERF102,RUF005,RUF015,PERF203 --config 'lint.ignore=[]'  # expect 0
  uv run ruff check .
  uv run ruff format --check .
  make typecheck
  make coverage-unit   # src expressions changed (PERF401/PERF102/RUF005) — verify no regression
  ```

- [ ] **Step 9: Stage** the touched src + test files + `.ruff.toml`. Run `git status --short` and confirm nothing tracked is left unstaged.

---

## Task 4: Textual & test-pattern fixes (RUF002 / RUF003 / RUF043 / UP045)

**Why:** Ambiguous unicode and pytest-raises pattern hygiene; UP045 is a genuine false positive (runtime construction, not annotation).

**Files (exact sites):**
- `RUF002` (ambiguous unicode in docstrings — replace `×`→`x`, `–`→`-`): `scripts/stability_campaign.py:2`; `src/otto/docker/compose.py:312`; `src/otto/host/unix_host.py:576`; `tests/unit/host/test_session_concurrency.py:138`
- `RUF003` (ambiguous unicode in comments — same replacement): `src/otto/host/session.py:1308`; `src/otto/host/transfer/nc.py:47`; `tests/e2e/cov/test_coverage_e2e.py:279,280,281`; `tests/integration/host/conftest.py:118`; `tests/unit/host/test_transfer_nc_put.py:103`
- `RUF043` (escape regex metachars in `pytest.raises(match=...)`): `tests/unit/cov/test_merger.py:78`; `tests/unit/host/test_privilege.py:231`; `tests/unit/models/test_settings.py:306`
- `UP045` (false positive — `# noqa`): `src/otto/cli/param_synth.py:86,109`
- Modify: `.ruff.toml` (remove the 4 codes from TEMP)

- [ ] **Step 1: RUF002 / RUF003** — replace the ambiguous glyph with its ASCII equivalent (`×` → `x`, `–` → `-`) in each flagged docstring/comment. These are prose (not doctest expressions) — confirm the line is not inside a `>>>` doctest block before editing. `src/otto/host/unix_host.py:576` is in a Sphinx-rendered docstring; the hyphen renders identically.

- [ ] **Step 2: RUF043** — at each `pytest.raises(..., match="...")`, the pattern contains an unescaped regex metacharacter that's meant literally. Wrap the literal portion with `re.escape("...")` (import `re` if needed) or escape the metachar inline (e.g. `\(`). Confirm the test still matches the real message (run that test).

- [ ] **Step 3: UP045** — append `  # noqa: UP045 — runtime Optional[] construction (not an annotation); X | None raises TypeError on special forms` to `param_synth.py:86` and `:109`. Do NOT rewrite to `X | None`.

- [ ] **Step 4: Remove `RUF002`, `RUF003`, `RUF043`, `UP045` from the TEMP block.**

- [ ] **Step 5: Verify.**
  ```bash
  uv run ruff check . --select RUF002,RUF003,RUF043,UP045 --config 'lint.ignore=[]'  # expect 0
  uv run ruff check .
  make typecheck
  uv run pytest tests/unit/cov/test_merger.py tests/unit/host/test_privilege.py tests/unit/models/test_settings.py -q  # RUF043 sites still match
  ```

- [ ] **Step 6: Stage** the touched files + `.ruff.toml`; `git status --short` to confirm nothing unstaged.

---

## End-of-Phase verification (after all 4 tasks, before handoff)

1. **TEMP is 12 codes lighter:** none of `RUF012, PERF203, RUF006, RUF059, RUF005, RUF015, PERF102, PERF401, RUF002, RUF003, RUF043, UP045` remain in the TEMP block; `UP037` still in the permanent deny-list.
2. **Whole-tree clean:** `uv run ruff check .` → 0; `uv run ruff format --check .` → clean.
3. **Types:** `make typecheck` → clean (ty `all = error`, incl. `unused-ignore-comment`).
4. **Behavior (src touched in Tasks 2-config-only/3/4):** `make coverage` (full bed, 92% floor). Offer `make nox` (py3.10–3.14) — the Phase-3 expression rewrites are version-agnostic, so `make coverage` + `make typecheck` is the standard bar; run nox if Chris wants the multi-Python confirmation (as in Phase 2).
5. **Opus final whole-branch review** before handoff.
6. **PAUSE** — give Chris a single paste-able commit message; he commits + pushes, then says move on.

## Self-review notes (spec coverage)

- Spec Phase 3 = "UP (minus UP037), RUF, PERF." Covered: all live UP (UP045), all live RUF, all live PERF. ✅
- RUF012-vs-pydantic watchpoint from the spec/memory: resolved by keeping RUF012 ENFORCED and converting the 28 model field defaults to `Field(default_factory=...)` + 4 genuine ClassVar test fixes — no exemption. ✅
- No deny-list or per-file-ignore changes this phase (Chris's call: keep every rule enforced, fix properly; PERF203 + UP045 silenced per-site as genuine false positives). The spec's permanent deny-list is unchanged, so no spec edit needed. ✅
- All 12 Phase-3 codes are removed from TEMP; `UP037` remains permanently denied. ✅
