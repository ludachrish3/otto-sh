# Strict-Linting Phase 4b — Cleanup Stragglers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clear the remaining cleanup-straggler ratchet debt — the bug-class, style, pytest, pathlib, and structural families left after Phase 4 — leaving only the `D` (docs), `ANN` (annotations), and `PGH003` (suppression-audit) families in TEMP for their dedicated phases.

**Architecture:** Per Chris's standing call (keep rules enforced + fix properly), most codes are FIXED. Deliberate exceptions this phase, all user-approved: (a) **exempt INP001/E402 in `tests/**` and INP001/T201 in `scripts/**`** (pytest namespace packages, fixture path-setup imports, CLI-script `print` are conventions, not bugs); (b) **raise `max-complexity` 10→20** (most flagged functions are inherently complex CLI/transfer/protocol code) — the two functions still over 20 are handled individually (`_run_coverage` refactored, `_build_app` noqa'd); (c) narrow per-site `# noqa` where a rule fights a deliberate pattern. Each task fixes/handles its sites then removes the now-clean codes from the `# ===== TEMP (ratchet) =====` block. STAGE-ONLY — never commit (Chris commits).

**Tech Stack:** ruff 0.15.x, ty 0.0.55 (`all = error`), pytest+xdist, asyncio, Python 3.10 floor.

## Global Constraints

- **STAGE-ONLY.** `git add` only; never `git commit`. Chris commits.
- **`ruff check .` is authoritative** (covers `scripts/`+`docs/`). Stage `.ruff.toml` + every touched file.
- **`make typecheck` in every task's verification; `make docs` in the final gate** (ruff format is NOT type-checker-neutral; implementers tend to leave format drift — run `uv run ruff format .` + re-`make typecheck` before handoff). Behavior-touching tasks also run `make coverage-unit`.
- **Never blanket `--fix --unsafe-fixes`.** Apply per-site with judgment; 31 of the remaining fixes are unsafe-fix only.
- **Python 3.10 floor + Sphinx-nitpicky (`-W`) docs gate are HARD.** No `from __future__ import annotations`, no unquoted forward refs. Per-site noqa format: `# noqa: CODE — <specific reason>`.
- **Ratchet rule:** a code leaves TEMP ONLY when all its sites (src+test+scripts) are resolved. Verify `uv run ruff check . --select <CODE> --config 'lint.ignore=[]'` is at the intended end-state before deleting from TEMP.

**Phase-4b codes leaving TEMP (~35):** `INP001 E402 T201 C901 E501 PT003 PT006 PT011 PT013 PT017 PT018 PTH105 PTH108 PTH116 PTH119 PTH123 PTH208 TRY004 TRY203 TRY300 TRY400 RET504 RSE102 PLW0108 PLW0603 PLW1510 PLW2901 PLR0913 PLR0133 PLC0414 PYI034 E741 E731 ERA001 EXE001 TD004 ARG001 ARG002`. **Left in TEMP for dedicated phases:** `D1xx D2xx D3xx D4xx` (Phase D), `ANN*` (Phase A), `PGH003` (Phase S).

---

## Task 1: Config — exemptions + max-complexity=20

**Why:** Pure `.ruff.toml`. INP001/E402/T201 are test/CLI-script conventions; complexity threshold rises to 20 (Chris's call).

**Files:** Modify `.ruff.toml`; add a one-line note to `docs/superpowers/specs/2026-06-27-strict-linting-design.md`.

- [ ] **Step 1: Raise complexity threshold.** In `[lint.mccabe]`, change `max-complexity = 10` → `max-complexity = 20`.
- [ ] **Step 2: Exempt structural families.** In `[lint.per-file-ignores]`: add `"INP001", "E402"` to the existing `"tests/**"` list; add a new `"scripts/**" = ["INP001", "T201"]` entry (with a comment: `# scripts are CLI tools: namespace packages + print() are by design`).
- [ ] **Step 3: Remove `INP001` from the TEMP block** (now fully covered by the tests/** + scripts/** exemptions; no src INP001 sites exist — confirm with `uv run ruff check src --select INP001 --config 'lint.ignore=[]'` → 0). Leave `E402` and `T201` in TEMP (they have src/test sites handled in Tasks 2/7). Leave `C901` in TEMP (handled in Task 2).
- [ ] **Step 4: Spec note.** Add to the spec's per-file-ignore section: INP001/E402→tests, INP001/T201→scripts (conventions); and that `max-complexity` was raised to 20.
- [ ] **Step 5: Verify.**
  ```bash
  uv run ruff check . --select INP001 --config 'lint.ignore=[]'   # 0 outside tests/scripts (all exempt)
  uv run ruff check .            # green
  make typecheck
  ```
- [ ] **Step 6: Stage** `git add .ruff.toml docs/superpowers/specs/2026-06-27-strict-linting-design.md`

---

## Task 2: C901 — refactor `_run_coverage`, noqa `_build_app`

**Why:** With max-complexity=20, only two functions still fire. Chris's per-function call: refactor one, noqa the other.

**Files:** `src/otto/cli/test.py` (`_run_coverage`), `src/otto/monitor/server.py` (`_build_app`), `.ruff.toml`.

- [ ] **Step 1: Refactor `_run_coverage` (cli/test.py:677, complexity 22).** Extract the metadata-writing tail (building the `toolchains` dict + the JSON write of cov metadata) into a new module-level helper, e.g. `def _write_cov_metadata(cov_repo, unix_hosts, unix_dirs, host_dirs, cov_dir) -> None:` (match the exact locals it needs — read the function). Call it from `_run_coverage`. Behavior must be identical (same file written, same content). This drops complexity below 20.
- [ ] **Step 2: noqa `_build_app` (monitor/server.py:61, complexity 21).** Add `# noqa: C901 — FastAPI route-factory; complexity is route count, not branching` on the `def _build_app(...)` line.
- [ ] **Step 3: Remove `C901` from the TEMP block.**
- [ ] **Step 4: Verify.**
  ```bash
  uv run ruff check . --select C901 --config 'lint.ignore=[]'   # expect 0 (all ≤20 except the noqa'd one)
  uv run ruff check .
  make typecheck
  uv run pytest tests/unit/cli tests/unit/monitor -q
  make coverage-unit
  ```
- [ ] **Step 5: Stage** the two src files + `.ruff.toml`.

---

## Task 3: PT-family — pytest-style fixes (tests)

**Why:** Chris's call: fix (real test-quality improvement). PT rules apply only to test code.

**Files (all tests; get exact lines live per code):** `PT018` (62, composite assert), `PT011` (22, raises-too-broad), `PT006` (5), `PT003` (4), `PT013` (2), `PT017` (2). Modify `.ruff.toml` (remove `PT003 PT006 PT011 PT013 PT017 PT018`).

- [ ] **Step 1: PT018** — `uv run ruff check tests --select PT018 --config 'lint.ignore=[]' --output-format concise`. Each `assert a and b` (composite) → split into `assert a` then `assert b` (preserves the test, gives a precise failure message). Keep any explanatory message on the most relevant sub-assert. Do NOT split an `assert a or b` (that's not a composite-AND).
- [ ] **Step 2: PT011** — each `pytest.raises(SomeBroadError)` without `match=` → add a `match=` that pins the real message (read what the code raises; use `re.escape` for literal metachars). Don't loosen.
- [ ] **Step 3: PT006** — `@pytest.mark.parametrize("a,b", ...)` with a comma-string → use a tuple/list of names `("a", "b")` (ruff's preferred form). PT013 — `import pytest` not `from pytest import x`. PT003 — drop the redundant `scope="function"`. PT017 — replace `assert` inside an `except` with `pytest.raises` (or restructure); if the test genuinely needs to inspect the caught exception, restructure to `pytest.raises(...) as exc_info` and assert on `exc_info`.
- [ ] **Step 4: Remove `PT003 PT006 PT011 PT013 PT017 PT018` from TEMP.**
- [ ] **Step 5: Verify.**
  ```bash
  uv run ruff check . --select PT003,PT006,PT011,PT013,PT017,PT018 --config 'lint.ignore=[]'   # expect 0
  uv run ruff check .
  make typecheck
  make coverage-unit   # ~90 test edits — confirm the suite still passes and assertions still fire
  ```
- [ ] **Step 6: Stage** touched test files + `.ruff.toml`.

---

## Task 4: E501 — line-too-long (fix breakable, noqa the rest)

**Why:** Chris's call: keep enforced; reflow what's cleanly breakable, noqa the unbreakable.

**Files (55 src + 15 tests; get exact lines live):** Modify `.ruff.toml` (remove `E501`).

- [ ] **Step 1: Fix breakable lines.** `uv run ruff check . --select E501 --config 'lint.ignore=[]' --output-format concise`. For each: if it's a long expression/call/string-concatenation that can be wrapped within line-length 100 WITHOUT hurting readability, reflow it (then `uv run ruff format <file>` to normalize). For an over-long string literal, prefer implicit string concatenation across lines (`("...long..." "...rest...")`) only if it stays readable.
- [ ] **Step 2: noqa the unbreakable.** A single long URL, a long error/log message that reads worse when split, a regex, or a line already carrying stacked directives → `# noqa: E501` (append to any existing noqa as `# noqa: <existing>,E501`). Do NOT add E501 to a line the formatter itself would reflow — run `uv run ruff format .` first so you only noqa genuinely-unbreakable lines.
- [ ] **Step 3: Remove `E501` from TEMP.**
- [ ] **Step 4: Verify.**
  ```bash
  uv run ruff check . --select E501 --config 'lint.ignore=[]'   # expect 0
  uv run ruff check .
  uv run ruff format --check .
  make typecheck
  ```
- [ ] **Step 5: Stage** touched files + `.ruff.toml`.

---

## Task 5: PTH — use pathlib

**Why:** `os.path`/`open()` → `pathlib`. Behavior-preserving modernization.

**Files (~28: get exact lines live):** `PTH123` (12, `open()`→`Path.open()`), `PTH208` (10, `os.listdir`→`Path.iterdir()`/`os.scandir`), `PTH116` (2, `os.stat`→`Path.stat()`), `PTH119` (2, `os.path.basename`→`Path.name`), `PTH105` (1, `os.replace`→`Path.replace`), `PTH108` (1, `os.unlink`→`Path.unlink`). Modify `.ruff.toml` (remove `PTH105 PTH108 PTH116 PTH119 PTH123 PTH208`).

- [ ] **Step 1: Convert each site to the pathlib equivalent.** `PTH123`: `open(p)` → `Path(p).open()` (if `p` is already a `Path`, just `p.open()`). `PTH208`: `os.listdir(d)` → `[x.name for x in Path(d).iterdir()]` or `sorted(Path(d).iterdir())` — preserve whether names or full paths were used. `PTH119`: `os.path.basename(p)` → `Path(p).name`. `PTH116`: `os.stat(p)` → `Path(p).stat()`. `PTH105`/`PTH108`: `os.replace`/`os.unlink` → `Path(...).replace`/`.unlink`. Ensure `from pathlib import Path` is imported. If a site genuinely needs the os API (e.g. a raw fd, or `os.listdir` ordering relied upon), `# noqa: PTHxxx — <reason>`. Behavior identical.
- [ ] **Step 2: Remove the six PTH codes from TEMP.**
- [ ] **Step 3: Verify.**
  ```bash
  uv run ruff check . --select PTH105,PTH108,PTH116,PTH119,PTH123,PTH208 --config 'lint.ignore=[]'   # expect 0
  uv run ruff check .
  make typecheck
  make coverage-unit
  ```
- [ ] **Step 4: Stage** touched files + `.ruff.toml`.

---

## Task 6: Bug-class mechanical — TRY / RET / RSE / PLW / PYI / PLR / PLC

**Why:** Mostly mechanical correctness/cleanup. A few deliberate-pattern noqa.

**Files (get exact lines live per code):** `TRY300` (10, move `return` to `else`), `TRY400` (2, `logging.error`→`logging.exception` in except), `TRY203` (2, remove useless try-except), `TRY004` (1, raise `TypeError` for type check), `RET504` (7, inline the returned var), `RSE102` (4, drop parens on argless `raise Foo()`→`raise Foo`), `PLW0108` (13, `lambda x: f(x)`→`f`), `PLW0603` (11, `global` — usually deliberate module-singleton → noqa), `PLW1510` (9, `subprocess.run` → add explicit `check=False`/`check=True`), `PLW2901` (2, rename redefined loop var), `PYI034` (5, `__enter__`/`__aenter__`/`__iadd__` return `Self`), `PLR0913` (6, too-many-args → `# noqa: PLR0913 — <reason>`), `PLR0133` (1, fix constant comparison), `PLC0414` (1, `import x as x` — keep if it's an explicit re-export (`# noqa`/`__all__`), else drop the alias). Modify `.ruff.toml` (remove all these codes).

- [ ] **Step 1: Mechanical fixes** — TRY300 (return→else), RET504 (inline), RSE102 (drop parens), PLW0108 (drop lambda wrapper), PLW2901 (rename), PLR0133, TRY203, TRY004, TRY400. Each is behavior-preserving; verify the transform matches ruff's intent.
- [ ] **Step 2: PYI034** — return type of `__enter__`/`__aenter__`/`__iadd__`/etc. should be `Self`. Import `from typing import Self` (3.11+) — BUT the floor is 3.10, so use `from typing_extensions import Self` (typing_extensions is already a dependency). Update the annotation.
- [ ] **Step 3: PLW1510** — add an explicit `check=` to each `subprocess.run` (`check=False` to preserve current behavior unless the call already handles returncode; `check=True` only where a non-zero exit should raise).
- [ ] **Step 4: PLW0603 + PLR0913 + PLC0414** — `global` statements are usually deliberate module-singleton/cache mutation → `# noqa: PLW0603 — module-level singleton` (judge each; fix if a global is genuinely avoidable). PLR0913 (too-many-args, mostly CLI commands) → `# noqa: PLR0913 — CLI command params` (judge). PLC0414 (`import x as x`) → if it's an intentional explicit re-export keep with `# noqa: PLC0414 — explicit re-export` (or add to `__all__`); else drop the redundant alias.
- [ ] **Step 5: Remove all the Task-6 codes from TEMP.**
- [ ] **Step 6: Verify.**
  ```bash
  uv run ruff check . --select TRY004,TRY203,TRY300,TRY400,RET504,RSE102,PLW0108,PLW0603,PLW1510,PLW2901,PYI034,PLR0913,PLR0133,PLC0414 --config 'lint.ignore=[]'   # expect 0
  uv run ruff check .
  make typecheck
  make coverage-unit
  ```
- [ ] **Step 7: Stage** touched files + `.ruff.toml`.

---

## Task 7: Style/naming + E402/T201 src

**Why:** Naming/style cleanup + the few src/test sites of the families exempted in tests/scripts.

**Files (get exact lines live):** `E741` (18, ambiguous `l`/`I`/`O` → rename), `E731` (3, `x = lambda:` → `def`), `ERA001` (~10, commented-out code → delete if dead; keep with `# noqa: ERA001` only if it's an intentional documented example), `EXE001` (2, shebang on non-executable script → `chmod +x` the file OR remove the shebang), `TD004` (1, add colon to TODO), src `E402` (2: `configmodule/__init__.py:67`, `configmodule/lab.py:72` — deliberate late imports → `# noqa: E402 — import after <setup>`), src `T201` (3: `cli/callbacks.py:11,13,14` — CLI output → convert to `typer.echo(...)` OR `# noqa: T201 — CLI stdout`), test `T201` (2: `tests/conftest.py:374,378` → `# noqa: T201 — test diagnostic output`). Modify `.ruff.toml` (remove `E402 T201 E741 E731 ERA001 EXE001 TD004`).

- [ ] **Step 1: E741** — rename each ambiguous single-char var (`l`→`line`/`item`/`lab` per context, `I`/`O` similarly); update all in-scope references. Where it's a conventional math index that's clearer as-is, `# noqa: E741`.
- [ ] **Step 2: E731** — convert `name = lambda args: expr` → `def name(args): return expr`.
- [ ] **Step 3: ERA001** — delete genuinely dead commented-out code; keep documented examples with `# noqa: ERA001 — illustrative example`.
- [ ] **Step 4: EXE001** — for each flagged script, `chmod +x` it (preferred for runnable scripts) or remove the shebang if it's not meant to be executed directly. (Note: a chmod is a file-mode change `git add` will stage.)
- [ ] **Step 5: TD004** — add the missing colon after `TODO`.
- [ ] **Step 6: E402 src (2) + T201 src/test (5)** — noqa with the specific reasons above (or convert callbacks.py prints to `typer.echo`).
- [ ] **Step 7: Remove `E402 T201 E741 E731 ERA001 EXE001 TD004` from TEMP.**
- [ ] **Step 8: Verify.**
  ```bash
  uv run ruff check . --select E402,T201,E741,E731,ERA001,EXE001,TD004 --config 'lint.ignore=[]'   # expect 0
  uv run ruff check .
  make typecheck
  make coverage-unit
  ```
- [ ] **Step 9: Stage** touched files + `.ruff.toml`.

---

## Task 8: ARG — unused arguments

**Why:** ARG001 (unused function arg) / ARG002 (unused method arg). Many are interface/protocol-required signatures (overrides, callbacks, registry seams) that must keep the param; a few are genuinely dead. Re-apply the ARG002 noqa intent stripped by RUF100 in Phase 1a. Tests are already exempt.

**Files (src + scripts; get exact lines live):** biggest cluster `host/embedded_host.py` (14 — verb methods that accept a uniform signature but ignore some params), plus `cli/main.py` (5), `cli/host.py` (4), `suite/plugin.py` (3), `suite/suite.py` (2), `reservations/{null,json}_backend.py`, `host/remote_host.py`, `host/transfer/{progress,embedded_base}.py`. Modify `.ruff.toml` (remove `ARG001 ARG002`).

- [ ] **Step 1:** `uv run ruff check src scripts --select ARG001,ARG002 --config 'lint.ignore=[]' --output-format concise`. At each: if the parameter is required by an interface/override/callback/registry signature (the method must accept it for polymorphism even if this implementation ignores it — e.g. `embedded_host` verbs mirroring `unix_host`), add `# noqa: ARG00x — required by <interface> signature`. If the argument is genuinely never needed (a real leftover), remove it AND update callers (only if safe — an override cannot drop a param the base defines; those stay noqa). When unsure, prefer noqa (removing a polymorphic param breaks the contract).
- [ ] **Step 2: Remove `ARG001 ARG002` from TEMP.**
- [ ] **Step 3: Verify.**
  ```bash
  uv run ruff check . --select ARG001,ARG002 --config 'lint.ignore=[]'   # expect 0
  uv run ruff check .
  make typecheck
  make coverage-unit
  ```
- [ ] **Step 4: Stage** touched files + `.ruff.toml`.

---

## End-of-Phase verification (after all 8 tasks, before handoff)

1. **TEMP holds only the deferred phases:** only `D1xx/D2xx/D3xx/D4xx` (Phase D), `ANN*` (Phase A), `PGH003` (Phase S) remain — every Phase-4b code is gone.
2. **Whole-tree clean:** `uv run ruff check .` → 0; `uv run ruff format --check .` → clean (run `uv run ruff format .` + re-`make typecheck` if implementers left drift).
3. **Types:** `make typecheck` → clean.
4. **Docs:** `make docs` → clean (E741 renames / E501 reflows / PT fixes may touch doctested examples).
5. **Behavior:** `make coverage` (full bed, 92% floor). Offer `make nox`.
6. **Opus final whole-branch review** — special attention to the `_run_coverage` refactor (behavior-identical metadata write), PT011 match-tightening (no test weakened), E741 rename completeness, and that every `# noqa` is justified.
7. **PAUSE** — give Chris a single paste-able commit message.

## Self-review notes

- Roadmap: Phase 4b = the cleanup stragglers between Phase 4 and the D/A/S phases. All ~35 codes covered; D/ANN/PGH003 explicitly left for their phases. ✅
- Chris's decisions encoded: E501→fix+noqa; C901→threshold 20 + `_run_coverage` refactor + `_build_app` noqa; INP/E402/T201→tests/scripts exempt; PT→fix. ✅
- Config policy changes (the exemptions + max-complexity bump) recorded in the spec (Task 1 Step 4). ✅
- End goal after this phase: TEMP contains only D/ANN/PGH003 → Phases D, A, S finish the ratchet (TEMP empty).
