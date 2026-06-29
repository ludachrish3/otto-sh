# Strict-Linting Phase A — Annotations (ANN) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clear the `ANN` annotation-completeness debt (ANN001/002/003/201/202/204/206; ANN401 stays permanently denied) across `src/` + `scripts/`, adding real, 3.10-safe, Sphinx-resolvable type annotations.

**Architecture:** Per Chris's standing call (enforce + fix). The ONE exemption this phase: **`docs/conf.py` ANN is exempted** (Sphinx build-config glue, never autodoc'd — annotating it adds nothing to the rendered docs). All 62 src/scripts sites are FIXED with real annotations following otto's existing idiom. STAGE-ONLY — never commit (Chris commits).

**Tech Stack:** ruff 0.15.x, ty 0.0.55 (`all = error`), pydantic v2, asyncio, Python 3.10 floor, Sphinx-nitpicky (`-W`) docs gate, `typing_extensions` (dep).

## Global Constraints

- **STAGE-ONLY.** Stage ONLY files you touch with explicit `git add <path>` — **NEVER `git add -u`/`git add .`** (it sweeps in Chris's unrelated `todo/TODO.md` edit). After staging, `git status --short` must show only your files as `M ` (no ` M`/`MM` divergence; only `todo/TODO.md` may be ` M`). Chris commits.
- **`make docs` in EVERY annotation task's verification.** Annotations on public functions render in autodoc; the Sphinx-nitpicky (`-W`) gate fails on any unresolved type xref. This is the primary risk of this phase.
- **`make typecheck` in every task** (annotations are exactly what ty reads — a wrong annotation is a type error). `make coverage-unit` too (annotations are runtime-evaluated in some contexts).
- **If you run `uv run ruff format`, run it only on files you edited, then re-`git add` them.** After ANY format, re-run `uv run ruff check .` (format reflows can orphan noqa).
- **Annotation form — otto's idiom (HARD rules):**
  - **NEVER** add `from __future__ import annotations`, **NEVER** use a `TYPE_CHECKING` block, **NEVER** unquote an existing quoted forward-ref. (FA/TC/UP037 are permanently denied to protect the 3.10 floor + the Sphinx docs gate; otto already carries 167 deliberately-quoted annotations.)
  - For a type already imported at module top, use it directly.
  - For a type NOT yet imported: if a real top-level `import` is safe (no import cycle — otto uses lazy/local imports precisely to avoid cycles), add it at module top (sorted). If a top-level import would risk a cycle, use a **quoted forward-ref** string annotation (e.g. `host: "UnixHost"`) — a string is not evaluated at runtime (no cycle, 3.10-safe), matching otto's existing pattern. Prefer the quoted form when unsure; it's the established otto idiom.
  - Quoted forward-refs must still be **Sphinx-resolvable** — use the type's importable name (the same names the existing quoted annotations use). `make docs` is the check.
- **Never annotate as `Any`** (that trips ANN401, permanently denied). If an argument is genuinely untyped, find its real type from usage, or use a precise `object`/protocol/union — not `Any`.
- **Ratchet rule:** the ANN codes leave the TEMP block only in the final task, after the whole tree (minus the conf.py exemption) is ANN-clean. Verify `uv run ruff check . --select <ANN codes> --config 'lint.ignore=[]'` → 0 before removing.

**Special-method return types (ANN204) — the known-answer cheatsheet:** `__init__`/`__post_init__`/`__exit__`/`__aexit__` → `-> None`; `__repr__`/`__str__` → `-> str`; `__eq__`/`__ne__` → `-> bool`; `__hash__` → `-> int`; `__enter__`/`__aenter__` → `-> Self` (`from typing_extensions import Self`, 3.10-safe — NOT `typing.Self`), unless the method clearly returns a different concrete type (then use that, quoted if needed).

---

## Task 1: Config — exempt ANN in docs/conf.py

**Why:** Pure `.ruff.toml`. conf.py is Sphinx build glue, not library API (same rationale as its INP001 exemption).

**Files:** Modify `.ruff.toml`; one-line note in `docs/superpowers/specs/2026-06-27-strict-linting-design.md`.

- [ ] **Step 1:** In `[lint.per-file-ignores]`, extend the existing `"docs/conf.py"` entry (currently `["INP001"]`) to `["INP001", "ANN"]` (the `ANN` selector covers all ANN0xx/2xx codes). Add/extend the comment: `# Sphinx build config — not autodoc'd; annotations add nothing to rendered docs`.
- [ ] **Step 2:** Do NOT remove any ANN code from the TEMP block yet (all have src sites, handled in Tasks 2-4; final removal in Task 4).
- [ ] **Step 3:** Spec note — add to the per-file-ignore section: `docs/conf.py` ANN exemption (build glue, not rendered).
- [ ] **Step 4: Verify.**
  ```bash
  uv run ruff check docs/conf.py --select ANN --config 'lint.ignore=[]'   # expect 0 (exempt)
  uv run ruff check .            # green
  make typecheck
  ```
- [ ] **Step 5: Stage** `git add .ruff.toml docs/superpowers/specs/2026-06-27-strict-linting-design.md`

---

## Task 2: Annotate the `host/` cluster (~24 sites)

**Why:** The biggest cluster, and the one with the most cross-type annotations (Host subclasses, sessions, connections) — highest import-cycle + Sphinx-resolution risk, so do it deliberately.

**Files (get exact lines+codes live per file):** `src/otto/host/local_host.py` (8), `src/otto/host/connections.py` (5), `src/otto/host/remote_host.py` (4), `src/otto/host/host.py` (4), `src/otto/host/telnet.py` (2), `src/otto/host/unix_host.py` (1). Get sites: `uv run ruff check src/otto/host --select ANN001,ANN002,ANN003,ANN201,ANN202,ANN204,ANN206 --config 'lint.ignore=[]' --output-format concise`.

- [ ] **Step 1: Annotate every flagged def fully** (args + return together). For each:
  - Infer the real type from the body/usage and surrounding code. Args named `host` → the appropriate `Host` subclass; `port` → `int`; `path`/`dest_dir` → `Path` (or `"str | Path"` if both are accepted — check usage); `*args`/`**kwargs` → annotate the element type (e.g. `*args: object`, `**kwargs: object`, or a precise type if known).
  - Special methods: use the ANN204 cheatsheet (`__enter__`/`__aenter__` → `Self` from `typing_extensions`).
  - Return types: read what the function returns. `-> None` for no-return; otherwise the concrete/quoted type.
  - **Imports:** prefer an existing top-level import; if absent and a top-level import is cycle-safe, add it (sorted); otherwise use a quoted forward-ref. Match how the existing quoted annotations in these files spell their types.
- [ ] **Step 2: Verify (run make docs — these are host-API methods that autodoc renders).**
  ```bash
  uv run ruff check src/otto/host --select ANN001,ANN002,ANN003,ANN201,ANN202,ANN204,ANN206 --config 'lint.ignore=[]'   # expect 0
  uv run ruff check .
  uv run ruff format --check .
  make typecheck
  make docs                 # MUST be 0-warning — catches any unresolved type xref from a new annotation
  make coverage-unit
  ```
  If `make docs` reports an unresolved-reference warning for a type you added, fix it: use the type's fully-resolvable name, or (if it's a legitimately-unrenderable type) add it to `nitpick_ignore` in `docs/conf.py` with a comment. Do NOT silence the whole docs gate.
- [ ] **Step 3: Stage** the touched `host/` files (explicit paths).

---

## Task 3: Annotate the `cli/` + `suite/` + `scripts/` cluster (~20 sites)

**Why:** CLI commands, suite lifecycle, and a script. Typer-decorated commands have their own annotation conventions (params are often already typed for Typer; the unannotated ones are usually helpers/callbacks).

**Files (get exact lines+codes live):** `src/otto/suite/suite.py` (6), `src/otto/cli/main.py` (4), `src/otto/cli/docker.py` (3), `src/otto/cli/run.py` (2), `src/otto/cli/test.py` (1), `src/otto/cli/monitor.py` (1), `src/otto/cli/host.py` (1), `src/otto/suite/register.py` (1), `scripts/junit_failures.py` (1). Get sites: `uv run ruff check src/otto/cli src/otto/suite scripts --select ANN001,ANN002,ANN003,ANN201,ANN202,ANN204,ANN206 --config 'lint.ignore=[]' --output-format concise`.

- [ ] **Step 1: Annotate every flagged def fully** (same approach as Task 2). For Typer command callbacks, match the existing param-annotation style in the file. For `cli/docker.py` args `repo`/`lab`, read the command to find the real types (a `Repo`/lab-id). Use quoted forward-refs for otto types that would cycle.
- [ ] **Step 2: Verify.**
  ```bash
  uv run ruff check src/otto/cli src/otto/suite scripts --select ANN001,ANN002,ANN003,ANN201,ANN202,ANN204,ANN206 --config 'lint.ignore=[]'   # expect 0
  uv run ruff check .
  uv run ruff format --check .
  make typecheck
  make docs
  make coverage-unit
  ```
- [ ] **Step 3: Stage** the touched files (explicit paths).

---

## Task 4: Annotate the `configmodule/` + `logger/` + `monitor/` + `utils`/`version` cluster (~18 sites) + remove ANN from TEMP

**Why:** The remaining cluster, then close out the ratchet (all src ANN now clean).

**Files (get exact lines+codes live):** `src/otto/configmodule/repo.py` (6), `src/otto/utils.py` (4), `src/otto/logger/formatters.py` (3), `src/otto/configmodule/version.py` (2), `src/otto/monitor/server.py` (2), `src/otto/version.py` (1). Get sites: `uv run ruff check src/otto/configmodule src/otto/logger src/otto/monitor src/otto/utils.py src/otto/version.py --select ANN001,ANN002,ANN003,ANN201,ANN202,ANN204,ANN206 --config 'lint.ignore=[]' --output-format concise`.

- [ ] **Step 1: Annotate every flagged def fully** (same approach). `logger/formatters.py` `__init__` → `-> None`; the `originalMsg`-style locals are already renamed (Phase 4), just annotate signatures.
- [ ] **Step 2: Confirm the WHOLE tree is ANN-clean, then remove the ANN codes from TEMP.**
  ```bash
  uv run ruff check . --select ANN001,ANN002,ANN003,ANN201,ANN202,ANN204,ANN206 --config 'lint.ignore=[]'   # expect 0 tree-wide
  ```
  Then delete `ANN001 ANN002 ANN003 ANN201 ANN202 ANN204 ANN206` from the `.ruff.toml` TEMP block (leave `ANN401` in the permanent deny-list; leave `D1xx`/`PGH003` in TEMP for Phases D/S).
- [ ] **Step 3: Verify.**
  ```bash
  uv run ruff check .            # green (ANN now enforced everywhere; 0 violations)
  uv run ruff format --check .
  make typecheck
  make docs
  make coverage-unit
  ```
- [ ] **Step 4: Stage** the touched files + `.ruff.toml` (explicit paths).

---

## End-of-Phase verification (after all 4 tasks, before handoff)

1. **TEMP holds only `D1xx/D2xx/D3xx/D4xx` (Phase D) + `PGH003` (Phase S)** — every ANN code gone; `ANN401` still permanently denied.
2. **Whole-tree clean:** `uv run ruff check .` → 0; `uv run ruff format --check .` → clean.
3. **Types:** `make typecheck` → clean.
4. **Docs (critical for this phase):** `make docs` → 0 warnings (Sphinx-nitpicky). Confirm no new `nitpick_ignore` entry silences more than the one type it was added for.
5. **Behavior:** `make coverage` (full bed, 92% floor). Offer `make nox`.
6. **Opus final whole-branch review** — attention to: annotation correctness (does each type match the real value?), no `__future__`/`TYPE_CHECKING` introduced, no `Any`, quoted-vs-imported choices sound (no new import cycle), and `make docs` clean.
7. **PAUSE** — give Chris a single paste-able commit message.

## Self-review notes

- Roadmap Phase A = "ANN minus ANN401, real 3.10 annotations + module-top imports, NEVER `__future__`/TC/UP037, re-verify `make docs`." Covered. ✅
- Chris's decision: `docs/conf.py` ANN exempted (build glue, no doc-output benefit). ✅
- After Phase A, TEMP = `D1xx` + `PGH003` only → Phases D and S finish the ratchet (TEMP empty).
