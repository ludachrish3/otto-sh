# Strict-Linting Phase D — Docstrings (`D1xx`/`D2xx`/`D3xx`/`D4xx`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clear the `D` (pydocstyle) family from the ruff TEMP ratchet — fix all docstring-formatting violations, deny the two convention-incompatible codes (D105/D107), and write real, meaningful docstrings for every undocumented public module/class/method/function/package — leaving TEMP holding only `PGH003` (Phase S).

**Architecture:** The ratchet (`.ruff.toml` `select=["ALL"]` minus a principled deny-list) currently TEMP-ignores 12 `D` codes. This phase removes them. Execution is split into **four committed sub-phases** (Chris's decision): **D-1 Formatting & deny** (mechanical, low-risk), then three **content** sub-phases that write real docstrings clustered by module responsibility. Codes leave TEMP only when *all* their sites are clean: D-1 removes the five formatting codes; the **last** content sub-phase (D-4) removes the five content codes. D105/D107 move from TEMP to the permanent deny-list in D-1.

**Tech Stack:** ruff 0.15.x (pydocstyle `convention = "pep257"`), Sphinx-nitpicky (`-W`) autodoc, ty 0.0.55, Python 3.10 floor, nox (py3.10–3.14).

## Global Constraints

- **STAGE-ONLY.** Agents never commit (otto's prepare-commit-msg hook needs `/dev/tty`). Each sub-phase is staged; Chris commits + pushes, then says "move on." PAUSE between sub-phases.
- **Docstrings are AUTODOC'd** — they render into the Sphinx API docs. The quality bar is *real documentation*, never stubs (`"""Method."""`). A stub passes ruff but pollutes the rendered docs and fails review.
- **The ratchet is removed from the TEMP block ONLY** (`.ruff.toml` lines 38–53), never the permanent deny-list above it. Fix while the code is still TEMP-ignored (gate stays green), verify zero remaining with `--config 'lint.ignore=[]'`, then delete from TEMP.
- **`ruff check .` is authoritative** and covers `scripts/` + `docs/` (the scoped `--select … src scripts` view misses `docs/`). Every sub-phase's final verification runs the bare `ruff check .`.
- **`make docs` (Sphinx `-W`, 0 warnings) runs in EVERY task** — docstrings are autodoc'd, so a malformed docstring (bad cross-ref, broken RST/doctest) breaks the docs gate even when ruff is green.
- **No `from __future__ import annotations`, no new `TYPE_CHECKING` blocks, no unquoting forward-refs.** (Docstring work shouldn't touch imports, but doctests must not introduce them either.)
- **pydocstyle convention = pep257.** Summary line in imperative mood, one-line summary then blank line then body (D205), first line ends with a period (D400).
- **Per-file-ignores already exempt tests + `docs/conf.py`** from `D` — this phase touches **`src/` and `scripts/` only**.
- Final gate before handoff each sub-phase: `uv run ruff check .` (0 D in scope), `uv run ruff format --check .`, `make typecheck`, `make docs`, `make coverage-unit`. Run full `make coverage` (live bed) at the end of the last content sub-phase.

### The docstring quality bar (the heart of this phase)

otto already has good exemplars — match them:

- **Module (`D100`/`D104`):** one line stating the module/package's responsibility. E.g. `"""Async host abstraction: the Host protocol and concrete base implementation."""`
- **Class (`D101`):** what the class *is* and its role; note key collaborators. See the `Status`/`CommandStatus` docstrings in `src/otto/utils.py` (summary + doctest).
- **Method/function (`D102`/`D103`):** imperative summary of what it does. Add an `Args:`/`Returns:`/`Raises:` block **only when it adds signal** beyond the typed signature (otto uses Google-style sections — see `split_on_commas` in `utils.py`). For trivial accessors a single summary line is correct and complete (see `Status.is_ok`). Add a `>>>` doctest only where it genuinely clarifies and is cheap to keep green.
- **Protocol/ABC methods** (e.g. the `Host` protocol in `host/host.py:213`): the docstring documents the **interface contract** — what an implementer must guarantee — not an implementation. The concrete `BaseHost` override may carry a short summary; do not paste the protocol's full contract into every override.
- **NO redundant noise.** Don't restate the type annotation in prose. Don't write `"""Initialize."""` anywhere (D107 is denied for exactly this reason).

### Scout command (every task uses this to enumerate its exact sites)

```bash
# Content codes for a cluster's files:
uv run ruff check <files...> --select D100,D101,D102,D103,D104 --config 'lint.ignore=[]' --output-format concise
# Formatting codes:
uv run ruff check <files...> --select D200,D205,D301,D400,D401 --config 'lint.ignore=[]' --output-format concise
# Per-cluster done-check (must print "All checks passed!" / 0 errors):
uv run ruff check <files...> --select D100,D101,D102,D103,D104 --config 'lint.ignore=[]'
```

---

## Sub-Phase D-1 — Formatting & deny (101 fixes + ratchet edits)

**Mechanical/low-risk. One commit.** Fixes all five docstring-*formatting* codes across `src/` + `scripts/`, denies D105/D107, and removes the formatting codes from TEMP.

Live counts (re-scout to confirm — numbers drift): `D205`=76 (blank line after summary), `D401`=12 (imperative mood), `D301`=9 (escape-sequence → raw string), `D400`=3 (trailing period), `D200`=1 (unnecessary multiline). Spread thin across ~50 files.

### Task D1.1: Fix all docstring-formatting violations

**Files:** every `src/` + `scripts/` file flagged by the formatting scout (run it to get the live list; top files: `host/session.py`, `host/unix_host.py`, `models/settings.py`, `host/command_frame.py`, `cli/expose.py`).

**Interfaces:** none (docstring text only; no signatures change).

- [ ] **Step 1: Enumerate sites**

```bash
uv run ruff check src scripts --select D200,D205,D301,D400,D401 --config 'lint.ignore=[]' --output-format concise
```

- [ ] **Step 2: Fix each, by code**
  - **D205** (blank line after summary): insert one blank line between the one-line summary and the body. Do **not** merge the body into the summary.
  - **D401** (non-imperative mood): reword the summary to imperative ("Returns the…" → "Return the…"). Judgment call — keep meaning intact.
  - **D301** (escape sequence): prefix the docstring with `r` (`r"""…\d…"""`) where it contains backslashes (regex, Windows paths). Do not alter the text.
  - **D400** (trailing period): add `.` to the end of the first line.
  - **D200** (unnecessary multiline): collapse a one-sentence docstring onto a single line `"""Summary."""`.
  - ruff offers `--unsafe-fixes` for some of these — **do not blanket-autofix**; D401 rewording and D301 raw-prefixing need human eyes. Apply fixes by hand or per-file, then verify.

- [ ] **Step 3: Verify formatting codes are zero**

```bash
uv run ruff check src scripts --select D200,D205,D301,D400,D401 --config 'lint.ignore=[]'
```
Expected: `All checks passed!`

### Task D1.2: Ratchet edits (deny D105/D107, remove formatting codes from TEMP)

**Files:** Modify `.ruff.toml`.

- [ ] **Step 1: Move D105 + D107 into the permanent deny-list.** Delete the `"D105",` and `"D107",` lines from the TEMP block. Add to the permanent deny-list (after `"ANN401",` in Group 4, or a new clearly-labelled docstring group):

```toml
    # --- Group 4 (cont.): docstring conventions otto documents elsewhere ---
    "D105",  # magic-method docstrings: dunder semantics are conventional; class docstring covers them
    "D107",  # __init__ docstrings: construction is documented in the class docstring (D101), per numpy/google style
```

- [ ] **Step 2: Remove the five formatting codes from TEMP.** Delete the `"D200",`, `"D205",`, `"D301",`, `"D400",`, `"D401",` lines from the TEMP block. After this edit, TEMP must read exactly: `D100, D101, D102, D103, D104, PGH003`.

- [ ] **Step 3: Verify the whole tree is ruff-clean** (TEMP no longer hides the formatting codes; deny no longer counts D105/D107):

```bash
uv run ruff check .
uv run ruff format --check .
```
Expected: `All checks passed!` for both.

### Task D1.3: Gate D-1

- [ ] `make typecheck` — clean (formatter reflow can orphan `# ty: ignore`; re-check).
- [ ] `make docs` — 0 warnings (reworded/raw-prefixed docstrings must still render).
- [ ] `make coverage-unit` — passes, ≥85% floor (doctests in reworded docstrings still execute).
- [ ] **Stage explicit paths, verify clean index:**

```bash
git add .ruff.toml src scripts
git status --short | grep -v '^.M todo/TODO.md' || true   # nothing tracked left unstaged except todo
```

- [ ] **PAUSE.** Hand Chris a paste-able commit message; wait for "move on."

---

## Sub-Phase D-2 — Host docstrings (58 sites, one commit)

The public Host API surface — the highest-visibility autodoc. Two tasks.

### Task D2.1: `host/host.py` (34 sites)

**Files:** `src/otto/host/host.py`.

**Context:** `Host` (line 213) is a `Protocol` — its ~28 method stubs define the **interface contract**; document what an implementer must guarantee (preconditions, return semantics, the `(Status, str)` convention, timeout/raise behavior). `BaseHost` (line 316) is the concrete ABC; its overrides get a short summary, not a re-paste of the contract. Module docstring (D100) states the file's responsibility. Existing attribute docstrings (lines 214–230) show the house style.

- [ ] **Step 1:** `uv run ruff check src/otto/host/host.py --select D100,D101,D102,D103,D104 --config 'lint.ignore=[]' --output-format concise`
- [ ] **Step 2:** Read each flagged symbol's code and write a real docstring per the quality bar. For the `Host` protocol methods, document the contract; for `BaseHost` concrete methods, a concise summary (and Args/Returns only where they add signal).
- [ ] **Step 3:** `uv run ruff check src/otto/host/host.py --select D100,D101,D102,D103,D104 --config 'lint.ignore=[]'` → 0
- [ ] **Step 4:** `make docs` — 0 warnings (these render into the Host API page).

### Task D2.2: Rest of the host package (24 sites)

**Files:** `host/embedded_host.py` (5), `host/session.py` (4), `host/transport.py` (3), `host/transfer/base.py` (3), `host/local_host.py` (2), `host/connections.py` (2), `host/transfer/registry.py` (1), `host/transfer/progress.py` (1), `host/telnet.py` (1), `host/remote_host.py` (1), `host/__init__.py` (1). (Re-scout for exact lines.)

- [ ] **Step 1:** scout these files (content codes).
- [ ] **Step 2:** write real docstrings; `host/__init__.py` D104 = the host package's one-line responsibility.
- [ ] **Step 3:** `uv run ruff check src/otto/host --select D100,D101,D102,D103,D104 --config 'lint.ignore=[]'` → 0 (whole host pkg, incl. host.py from D2.1).
- [ ] **Step 4:** `make docs` — 0 warnings.

### Task D2.3: Gate D-2

- [ ] `uv run ruff check .` (D100-104 still TEMP-ignored elsewhere — gate green), `make typecheck`, `make docs`, `make coverage-unit`.
- [ ] `git add src` (no `.ruff.toml` change this sub-phase); verify clean index.
- [ ] **PAUSE** — commit message to Chris; wait for "move on."

---

## Sub-Phase D-3 — Models / coverage / configmodule docstrings (70 sites, one commit)

The data + config layer. Three tasks by package.

### Task D3.1: `models/` (30 sites)

**Files:** `models/options.py` (16), `models/settings.py` (8), `models/host.py` (5), `models/base.py` (1).

- [ ] Scout → write docstrings (these are pydantic models; document the field-group's purpose at class level, and any non-obvious validator/method) → `uv run ruff check src/otto/models --select D100,D101,D102,D103,D104 --config 'lint.ignore=[]'` → 0 → `make docs` 0-warn.

### Task D3.2: `coverage/` (20 sites)

**Files:** `coverage/store/model.py` (13), `coverage/correlator/paths.py` (2), `coverage/store/__init__.py` (1), `coverage/renderer/__init__.py` (1), `coverage/fetcher/__init__.py` (1), `coverage/correlator/__init__.py` (1).

- [ ] Scout → write docstrings (subpackage `__init__` D104 = one-line responsibility each) → `uv run ruff check src/otto/coverage --select D100,D101,D102,D103,D104 --config 'lint.ignore=[]'` → 0 → `make docs` 0-warn.

### Task D3.3: `configmodule/` (20 sites)

**Files:** `configmodule/repo.py` (13), `configmodule/__init__.py` (3), `configmodule/version.py` (2), `configmodule/lab.py` (2), `configmodule/configmodule.py` (1).

- [ ] Scout → write docstrings → `uv run ruff check src/otto/configmodule --select D100,D101,D102,D103,D104 --config 'lint.ignore=[]'` → 0 → `make docs` 0-warn.

### Task D3.4: Gate D-3

- [ ] `uv run ruff check .`, `make typecheck`, `make docs`, `make coverage-unit`.
- [ ] `git add src`; clean index; **PAUSE** — commit message; wait for "move on."

---

## Sub-Phase D-4 — CLI / context / suite / supporting / scripts + final ratchet (78 sites, one commit)

The application/glue layer, the long tail, and the **final TEMP removal** for the content codes. Three tasks + the ratchet edit.

### Task D4.1: CLI + context + suite (35 sites)

**Files:** `context.py` (9), `cli/main.py` (6), `cli/param_synth.py` (4), `cli/run.py` (3), `cli/test.py` (2), `cli/login.py` (1), `cli/host.py` (1), `cli/banner.py` (1), `suite/suite.py` (5), `suite/plugin.py` (2), `suite/__init__.py` (1).

- [ ] Scout → write docstrings → `uv run ruff check src/otto/context.py src/otto/cli src/otto/suite --select D100,D101,D102,D103,D104 --config 'lint.ignore=[]'` → 0 → `make docs` 0-warn.

### Task D4.2: Supporting packages (logger / examples / reservations / storage / monitor / docker / top-level) (35 sites)

**Files:** `logger/formatters.py` (5), `logger/levels.py` (1), `logger/__init__.py` (1), `examples/reservations.py` (4), `examples/lab_repository.py` (2), `reservations/null_backend.py` (3), `reservations/json_backend.py` (3), `reservations/check.py` (1), `storage/protocol.py` (1), `storage/json_repository.py` (1), `storage/factory.py` (1), `monitor/server.py` (1), `monitor/collector.py` (1), `docker/staging.py` (1), `docker/build.py` (1), `utils.py` (2), `version.py` (1), `__main__.py` (1), `__init__.py` (1). (Re-scout for exact set.)

- [ ] Scout → write docstrings (top-level `otto/__init__.py` D104 = the package's headline one-liner; `examples/` are teaching samples — docstrings should read as documentation) → verify each path's content codes → 0 → `make docs` 0-warn.

### Task D4.3: `scripts/` (8 sites)

**Files:** `scripts/stability_campaign.py` (7), `scripts/lint_markdown_doctests.py` (2), `scripts/lab_health.py` (1), `scripts/junit_failures.py` (1).

- [ ] Scout → write docstrings → `uv run ruff check scripts --select D100,D101,D102,D103,D104 --config 'lint.ignore=[]'` → 0. (`scripts/` isn't autodoc'd, but keep summaries real.)

### Task D4.4: Final ratchet — remove content codes from TEMP

**Files:** Modify `.ruff.toml`.

- [ ] **Step 1: Confirm zero D100-104 anywhere in scope** *before* editing TEMP:

```bash
uv run ruff check src scripts --select D100,D101,D102,D103,D104 --config 'lint.ignore=[]'
```
Expected: `All checks passed!` (if not, a site was missed — fix before proceeding).

- [ ] **Step 2:** Delete `"D100",`, `"D101",`, `"D102",`, `"D103",`, `"D104",` from the TEMP block. TEMP must now contain exactly one entry: `"PGH003",`.

- [ ] **Step 3: Whole-tree authoritative check** (D codes no longer TEMP-hidden):

```bash
uv run ruff check .
```
Expected: `All checks passed!`

### Task D4.5: Final gate D-4 (full bed)

- [ ] `uv run ruff check .`, `uv run ruff format --check .`, `make typecheck`, `make docs` (0-warn — the whole new API doc surface renders).
- [ ] `make coverage` (full live bed, 92% floor) — docstring doctests run against the bed; confirms no doctest regressions.
- [ ] (Offer `make nox` py3.10–3.14.)
- [ ] `git add .ruff.toml src scripts`; verify `git status --short` shows only `M todo/TODO.md`.
- [ ] **PAUSE** — final commit message; wait for Chris.

---

## Final whole-branch review

After D-4 stages, dispatch the opus whole-branch review (superpowers:requesting-code-review) over the full Phase-D diff (`git diff <D-1 base>..HEAD` once D-1..D-3 are committed, or the staged tree). Review focus: **docstring QUALITY (no stubs, accurate, render-clean), no signature/behavior changes, TEMP reduced to exactly `PGH003`, D105/D107 in the permanent deny-list with rationale.**

## Self-Review (run before dispatching Task D1.1)

1. **Coverage:** every live D code is either fixed (D100/101/102/103/104/200/205/301/400/401) or denied (D105/D107). ✓
2. **Ratchet end-state:** TEMP = `PGH003` only after D-4. ✓
3. **No placeholders:** each task names its files + scout command + 0-check + `make docs`. The *docstring text* is intentionally authored per-symbol by the implementer (reading the code) — that is the work, not a placeholder. ✓
