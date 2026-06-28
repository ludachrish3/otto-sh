# Strict Linting — Phase 0 + Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adopt `ruff format` across the tree at line-length 100, then establish the strict `select = ["ALL"]`-minus-deny-list ruff config (green via a temporary ratchet ignore) and wire `lint` + `format --check` into the default nox/make gate.

**Architecture:** Two phases of the broader strict-linting roadmap ([spec](../specs/2026-06-27-strict-linting-design.md)). Phase 0 lands the formatter as one mechanical reformat. Phase 1 sets `select = ["ALL"]` with a permanent principled deny-list plus a **temporary ignore** of every rule still firing after a safe autofix pass — so the gate is green immediately and strictness can only increase as later phases shrink that temp block. Nothing here changes runtime behavior; the verification oracle is ruff + the existing docs/test gates, not new unit tests.

**Tech Stack:** ruff 0.15.x (lint + formatter), ty (unchanged), nox/nox-uv, GNU make, doc8, Sphinx (`-W` nitpicky docs gate), Python 3.10 floor.

## Global Constraints

- **Stage-only — do NOT commit.** otto's prepare-commit-msg hook needs `/dev/tty`; agents must leave changes UNSTAGED in the working tree and never run `git commit`/`git add`. The maintainer commits. (The "Commit" steps below are written for completeness; the executor stages nothing and hands over paste-able messages.)
- **Python floor 3.10.** No change may break 3.10 runtime.
- **Sphinx `-W` docs gate must stay green** through every task — no annotation/import churn that breaks nitpicky autodoc.
- **No behavior change.** Phases 0–1 are formatting + lint-config + gate wiring only. Every task verifies the unit suite + docs still pass.
- **Line length = 100** (top-level `line-length`, formatter, `E501`, and doc8 all aligned to 100).
- **Quote style = `double`** (ruff default; the formatter owns quotes — the lint `Q` family stays denied).
- **Permanent deny-list = exactly the four groups in the spec.** Everything else still firing goes in the **TEMP** ignore block, regenerated mechanically (never hand-curated), to be removed batch-by-batch in later phases.
- **`ruff check --fix` uses SAFE fixes only** — never pass `--unsafe-fixes`.
- Direct ruff invocations use `uv run --no-sync` (don't dirty `uv.lock`). `make`/`nox` gate targets are the canonical full-gate checks; if `uv run nox` dirties `uv.lock`, restore with `git checkout uv.lock`.

---

### Task 1: Phase 0 — adopt `ruff format` at line-length 100

**Files:**
- Modify: `.ruff.toml` (top-level `line-length`; `[format]` `quote-style`, `docstring-code-line-length`)
- Modify: `pyproject.toml` (`[tool.doc8] max-line-length`)
- Modify: the whole tree (`src/`, `tests/`, any top-level `*.py`) — mechanical reformat output only

**Interfaces:**
- Produces: a fully `ruff format`-clean tree at line-length 100, so Phase 1's lint rules operate on formatted code.

- [ ] **Step 1: Lower the line length and pin the formatter quote style**

In `.ruff.toml`, change the top-level `line-length` and the `[format]` block:

```toml
line-length = 100
```

```toml
[format]
indent-style = "space"
line-ending = "lf"
quote-style = "double"
docstring-code-format = true
docstring-code-line-length = 100
```

(Only `line-length`, `quote-style` (new line), and `docstring-code-line-length` change; leave `target-version`, `indent-width`, `extend-exclude`, `indent-style`, `line-ending`, `docstring-code-format` as-is.)

- [ ] **Step 2: Align doc8 to 100**

In `pyproject.toml`, change the doc8 table:

```toml
[tool.doc8]
max-line-length = 100
ignore-path = ["docs/_build"]
```

- [ ] **Step 3: Reformat the whole tree**

Run: `uv run --no-sync ruff format .`
Expected: `NNN files reformatted, MMM files left unchanged` (roughly ~280 reformatted — spacing/alignment normalization, not behavior).

- [ ] **Step 4: Verify the tree is now format-clean**

Run: `uv run --no-sync ruff format --check .`
Expected: `NNN files already formatted` and a zero exit code (no "Would reformat" lines).

- [ ] **Step 5: Verify docs gate (doc8@100 + Sphinx `-W` + doctests) stays green**

Run: `make docs`
Expected: doc8 passes, Sphinx HTML builds with 0 warnings, Sphinx doctests pass. (Catches any `docstring-code-format` reflow that could have broken a doctest.)

- [ ] **Step 6: Verify the unit suite (incl. `--doctest-modules`) is unchanged**

Run: `make coverage-unit`
Expected: all unit tests pass, coverage floor met. Formatting is behavior-neutral; this confirms no doctest or import broke.

- [ ] **Step 7: Commit (stage-only — hand message to maintainer)**

```bash
git add .ruff.toml pyproject.toml src tests
git commit -m "style: adopt ruff format at line-length 100

Whole-tree mechanical reformat; set quote-style=double, lower line-length
120->100 and align doc8. No behavior change (docs + unit suite green).
Foundation for the strict-linting ratchet (Phase 0)."
```

---

### Task 2: Phase 1a — strict `select = ["ALL"]` config + safe autofix (gate green via temp-ignore)

**Files:**
- Modify: `.ruff.toml` (`[lint]` `select`/`ignore`; add `[lint.per-file-ignores]`; keep `[lint.pylint]`/`[lint.pydocstyle]`/`[lint.mccabe]`)
- Modify: the tree — only `ruff check --fix` safe-fix output (e.g. import sorting, docstring blank lines, simplifications)

**Interfaces:**
- Consumes: the format-clean tree from Task 1.
- Produces: `ruff check .` and `ruff format --check .` both green under `select = ["ALL"]`; a clearly-delimited TEMP ignore block that later phases shrink.

- [ ] **Step 1: Replace the `[lint]` select/ignore with the strict scaffold**

In `.ruff.toml`, replace the existing `select = [...]` and `ignore = [...]` under `[lint]` with the following. **Leave `[lint.pylint]`, `[lint.pydocstyle]` (`convention = "pep257"`), and `[lint.mccabe]` exactly as they are** — `convention = "pep257"` auto-suppresses the mutually-exclusive `D` rules, so they need not be listed.

```toml
[lint]
select = ["ALL"]
ignore = [
    # ===== PERMANENT deny-list (spec groups 1-4) — these stay disabled forever =====
    # --- Group 1: formatter-owned (ruff's documented formatter-conflict set) ---
    "W191", "E111", "E114", "E117", "D206", "D300",
    "Q000", "Q001", "Q002", "Q003",
    "COM812", "COM819",
    "ISC001", "ISC002",
    # --- Group 2: otto deliberate patterns (user-confirmed) ---
    "TID252",   # relative imports (from ..x import)
    "PLC0415",  # lazy/local imports (deliberate, startup speed)
    "G004",     # f-string in logging calls
    "TRY003", "EM101", "EM102", "EM103",  # terse inline exception messages
    # --- Group 3: annotation-safety (protect Sphinx-nitpicky + py3.10) ---
    "FA100", "FA102",          # would force `from __future__ import annotations`
    "TC001", "TC002", "TC003", # would hide annotation imports under TYPE_CHECKING
    "UP037",                   # would unquote forward refs -> NameError on 3.10
    # --- Group 4: low-value / noisy ---
    "FBT001", "FBT002", "FBT003",  # boolean-trap (considered & declined, see spec)
    "FIX002", "TD002", "TD003",    # TODO comment formatting
    "CPY001",                      # mandatory copyright header (otto uses none)
    "ANN401",                      # typing.Any

    # ===== TEMP (ratchet) — NOT permanent. Generated in Step 3; removed =====
    # ===== batch-by-batch in Phases 2..A. Do NOT hand-edit individual codes. =====
    # <generated TEMP codes go here in Step 3>
]

[lint.per-file-ignores]
"tests/**" = ["S101", "D", "PLR2004", "SLF001", "ANN", "ARG"]
"**/__init__.py" = ["F401"]
```

- [ ] **Step 2: Apply safe autofixes, then re-format**

Run: `uv run --no-sync ruff check . --fix`
Expected: `Found N errors (M fixed, K remaining).` (M ≈ 1000+ safe fixes: import sorting, docstring blank lines, comprehensions, etc. Do NOT use `--unsafe-fixes`.)

Then re-run the formatter (autofixes can leave reformattable lines):

Run: `uv run --no-sync ruff format .`
Expected: a handful of files reformatted or all clean.

- [ ] **Step 3: Generate the TEMP ignore block from what still fires**

Run this to produce ready-to-paste, sorted ignore lines for every rule still firing:

```bash
uv run --no-sync ruff check . --statistics 2>/dev/null \
  | awk '$2 ~ /^[A-Z]+[0-9]+$/ {print $2}' | sort -u \
  | sed 's/.*/    "&",/'
```

Paste that output into the `# <generated TEMP codes go here>` slot from Step 1. Sanity-check it is a clean list of rule codes (e.g. `"ANN001",`, `"D102",`, `"RUF012",` …) — no descriptions, no permanent-deny codes (those don't fire, so they won't appear).

- [ ] **Step 4: Verify ruff is fully green**

Run: `uv run --no-sync ruff check .`
Expected: `All checks passed!`

Run: `uv run --no-sync ruff format --check .`
Expected: `NNN files already formatted` (zero exit).

- [ ] **Step 5: Verify the autofixes were behavior-neutral**

Run: `make coverage-unit`
Expected: all unit tests pass, coverage floor met. (Safe fixes — e.g. F401 unused-import removal, SIM/C4 rewrites — must not change behavior; this proves it.)

Run: `make docs`
Expected: docs build green (0 warnings), doctests pass.

- [ ] **Step 6: Commit (stage-only — hand message to maintainer)**

```bash
git add .ruff.toml src tests
git commit -m "build(ruff): strict select=ALL scaffold + safe autofixes (Phase 1a)

select=[\"ALL\"] with the principled four-group deny-list + tests per-file
ignores; applied all safe autofixes; the remaining unclean rules sit in a
clearly-marked TEMP ignore block that later phases remove batch-by-batch.
ruff check + format --check green; unit suite + docs green (no behavior
change)."
```

---

### Task 3: Phase 1b — wire `lint` + `format --check` into the default gate

**Files:**
- Modify: `noxfile.py:40` (`nox.options.sessions`) + the opt-in comment above it
- Modify: `Makefile` (add `lint` + `format` targets; insert `lint` into the `validate` chain; add both to `.PHONY`)

**Interfaces:**
- Consumes: the green strict config from Task 2.
- Produces: `nox` default sessions and `make validate`/`ci`/`all` both run ruff check + format check; `make lint`/`make format` exist.

- [ ] **Step 1: Add `lint` to the default nox sessions**

In `noxfile.py`, replace the opt-in comment and the sessions line (currently lines ~37-40):

```python
# `lint` runs ruff check + format --check; it is part of the default gate now
# that the strict config (select=ALL minus the deny-list) is green.
nox.options.sessions = ["lint", "tests_unit", "typecheck", "docs"]
```

(The existing `def lint(session)` already runs `ruff check .` then `ruff format --check .` — no change to the session body.)

- [ ] **Step 2: Add `make lint` and `make format` targets**

In `Makefile`, add these targets (place them near `typecheck`):

```make
lint: ## Run ruff lint + format checks (part of validate/ci/all)
	uv run ruff check .
	uv run ruff format --check .

format: ## Apply ruff autoformat to the tree
	uv run ruff format .
```

- [ ] **Step 3: Run `lint` first in the `validate` chain**

In `Makefile`, change the `validate` target to run `lint` before the slow steps:

```make
validate: ## Run validation (clean-dist, lint, typecheck, coverage, docs) without building dist
	@$(MAKE) clean-dist \
		&& $(MAKE) lint \
		&& $(MAKE) typecheck \
		&& $(MAKE) $(COVERAGE_TARGET) \
		&& $(MAKE) docs
```

- [ ] **Step 4: Add the new targets to `.PHONY`**

In `Makefile`, add `lint` and `format` to the `.PHONY:` line (line 3).

- [ ] **Step 5: Verify the nox session passes and is wired as default**

Run: `uv run nox -s lint`
Expected: `ruff check .` passes (`All checks passed!`) and `ruff format --check .` passes; session green. (If `uv run nox` dirties `uv.lock`, restore it: `git checkout uv.lock`.)

Run: `uv run nox --list`
Expected: `lint` is marked as a selected/default session alongside `tests_unit`, `typecheck`, `docs`.

- [ ] **Step 6: Verify the make wiring**

Run: `make lint`
Expected: both ruff commands pass.

Run: `make -n validate`
Expected: the printed command plan includes a `lint` invocation before `typecheck`.

- [ ] **Step 7: Commit (stage-only — hand message to maintainer)**

```bash
git add noxfile.py Makefile
git commit -m "build(gate): enforce ruff lint + format in nox + make (Phase 1b)

Add lint to nox.options.sessions and to the make validate chain (so ci/all
run it); add make lint/format targets. Lint is no longer opt-in now that the
strict config is green."
```

---

## Self-Review notes

- **Spec coverage:**
  - ☑ Formatter @100 + quote-style double + doc8 100 → Task 1.
  - ☑ `select = ["ALL"]` + four-group permanent deny-list + tests `per-file-ignores` → Task 2 Step 1.
  - ☑ Safe-autofix the auto-fixable debt → Task 2 Step 2.
  - ☑ Shrinking-ignore ratchet: TEMP block generated + delimited for later phases → Task 2 Step 3.
  - ☑ Wire lint + format-check into the default gate → Task 3.
  - ☑ ty unchanged (already maximal) — out of scope for this plan, correctly untouched.
  - Later phases (2, 3, 4, D, A, ty-on-tests) are deliberately separate plans, per the spec roadmap.
- **No placeholders:** the TEMP block is generated by an exact, reproducible command (Task 2 Step 3) — not a hand-waved "fill in." Every config block is complete.
- **No new pytest tests:** intentional/YAGNI — a lint config has no unit-testable logic; the oracle is `ruff check`/`ruff format --check`/`make docs`/`make coverage-unit`/`nox -s lint`. Each task verifies behavior-neutrality through the existing suites.
- **Type/name consistency:** `nox.options.sessions` list, the `lint`/`format` make target names, and the `validate` chain are referenced identically across Task 3 steps.
- **Ordering:** Phase 0 (format) strictly precedes Phase 1 (lint), because the formatter owns quotes/commas/spacing and several lint rules; autofix (Task 2) runs on already-formatted code and re-formats after.
