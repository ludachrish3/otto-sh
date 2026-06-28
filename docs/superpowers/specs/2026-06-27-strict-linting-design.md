# Strict Linting & Formatting — Design

**Date:** 2026-06-27
**Status:** Approved (design); pending implementation plan
**Related:** GitHub issue #55 (ty `missing-override-decorator`, already adopted) — this is the broader "make linting as strict as possible" effort that #55 was the first instance of.

## Goal

Make otto's automated quality gates as strict as the tooling allows — across **ruff** (lint + format), **ty** (type check), and **nox/make** (gate wiring) — while (a) keeping the Sphinx-nitpicky (`-W`) docs build green, (b) preserving Python 3.10 runtime correctness, and (c) preserving otto's deliberate code patterns. Strictness is reached by a **ratchet**, never a big bang: every step lands green, gated, and reviewable.

## Current state (baseline, ground-truthed 2026-06-27)

- **ruff lint** — opt-in only (`nox -s lint`, not in the default gate). Minimal `select = ["E501","I","D","N802","N803","N815","N816"]`. Carries **238 open violations** even under that minimal set (204 auto-fixable: D209/I001/D403; 34 manual: 19 D401, 15 E501).
- **ruff format** — checked but **never applied**: `ruff format` would reformat **274 of 345 files** (churn is spacing/alignment, not quotes — the tree is already majority double-quoted).
- **ty** — already maximal: `[tool.ty.rules] all = "error"`, no demotions (clean after #55). In the default gate.
- **nox** — `nox.options.sessions = ["tests_unit","typecheck","docs"]`. `lint` excluded; no `format` session.
- **`ruff --select ALL` on `src/otto` = 4167 violations**, of which ~1710 are formatter-owned, ~870 are otto-deliberate patterns, ~390 are docs/annotation completeness, and the remainder (~1000) is the genuine hygiene/bug-class debt.

## Decisions

1. **Target model:** `select = ["ALL"]` minus a **principled deny-list** (the "ALL-minus-deny" model). New ruff rules apply on upgrade; each permanent deviation is a documented `ignore` entry.
2. **Line length:** **100** (down from 120). `[tool.doc8] max-line-length` lowered 120 → 100 to match.
3. **Quote style:** `[format] quote-style = "double"` (ruff default; matches the majority of the tree). The formatter owns quotes; the lint `Q` family stays denied. (`preserve` rejected as off-goal; `single` rejected as more churn.)
4. **preview:** `false` — do not enable unstable preview rules.
5. **pydocstyle convention:** keep `pep257`. **pylint/mccabe thresholds:** keep the existing `[lint.pylint]` / `[lint.mccabe]` settings.
6. **Tests** are linted (not excluded) but **relaxed via `per-file-ignores`**.
7. **Documentation rules (`D1xx` undocumented-*) and annotation-completeness rules (`ANN`) are deferred to explicit tail phases**, not the principled deny-list — they are scheduled, not abandoned.
8. **ty stays at `all = "error"`** (already maximal). Optionally extend ty to `tests/` as a final, separate phase.

## The principled deny-list (the asymptote)

This is the `ignore` set the ratchet shrinks **toward** — what stays disabled permanently. Four labeled groups:

### Group 1 — Formatter-owned (ruff's own documented formatter-conflict set)
`W191, E111, E114, E117, D206, D300, Q000, Q001, Q002, Q003, COM812, COM819, ISC001, ISC002`
Rationale: the **formatter** enforces indentation, quotes, and trailing commas. Enabling these as lint rules double-reports or conflicts. (`E501` is **kept** — it catches non-splittable over-length lines the formatter leaves.)

### Group 2 — otto deliberate patterns (user-confirmed)
| Code | Pattern preserved | Example |
|---|---|---|
| `TID252` | relative imports | `from ..connections import ConnectionManager` |
| `PLC0415` | lazy/local imports (startup speed) | `from .context import try_get_context` inside a fn |
| `G004` | f-string logging | `_logger.debug(f"{self._name}: FTP get {src} -> {dst}")` |
| `TRY003`, `EM101`, `EM102`, `EM103` | terse inline exception messages | `raise ValueError(f"{type} is {origin}, not a Literal...")` |

### Group 3 — Annotation-safety cluster (protects Sphinx-nitpicky + 3.10)
`FA100, FA102, TC001, TC002, TC003, UP037`
Rationale: otto's working model is **real runtime annotations + module-top imports**. These rules break it:
- `FA100/102` would force `from __future__ import annotations`, making all annotations lazy strings → Sphinx nitpicky emits spurious unresolved-xref warnings → `-W` docs gate fails (the documented otto ban).
- `TC001/2/3` would move annotation-only imports into `if TYPE_CHECKING:`, so the type is not importable at runtime → autodoc cannot resolve the xref under nitpicky → docs gate fails.
- `UP037` would unquote forward-refs such as `-> "ScpFileTransfer"` (a class referenced before it is defined) → `NameError` at runtime on 3.10 with no `__future__` import to defer evaluation.

(`TC004/005/006` are benign — they do not hide imports — and stay **enabled**.)

**Note:** this group does **not** weaken annotation *completeness*. The scheduled `ANN` phase still requires real, present, 3.10-valid annotations (`def run(self, cmd: str) -> tuple[Status, str]:`), written in the runtime + module-top style — only `ANN401` (`Any`) stays denied as noise.

### Group 4 — Low-value / noisy
`FBT001, FBT002, FBT003` (boolean-trap), `FIX002, TD002, TD003` (TODO formatting), `CPY001` (mandatory copyright header — otto uses none), `ANN401` (`Any`).

**FBT — considered and declined (2026-06-27).** Of the 125 FBT001/002 hits in `src`, 26 are in `src/otto/cli/*.py` Typer command functions where `bool = False` *is* the idiom for declaring a `--flag` and no positional Python call site exists (false positives). The remaining ~50 boolean params in library/host code are genuine traps, but adopting them would mean a non-autofixable, test-driven interface-churn phase (keyword-only signatures + call-site fixes, several on `@cli_exposed` host methods feeding the CLI synthesizer). Judged not worth the churn; FBT stays denied. Note: ruff has **no** rule enforcing blanket keyword-only — only booleans — so full keyword-only could only ever be a hand-applied convention, not a gate.

## Per-file ignores

- `tests/**`: `S101` (assert), `D` (docstrings), `PLR2004` (magic values), `SLF001` (private access), `ANN` (annotations), `ARG` (unused fixture/args). Rationale: standard test idioms; keep the strict bar where it matters (`src`).
- `**/__init__.py`: `F401` (re-export without `__all__` churn).

## Delivery strategy — ratchet by shrinking the ignore-list

`select = ["ALL"]` is set on day one. The **working `ignore`** begins as `principled deny-list ∪ {every rule still violated after an autofix pass}`, so the gate is green immediately and strictness can only increase. Each subsequent phase **removes a batch from `ignore`**, auto-fixes, manually clears the residual, and re-greens. The principled deny-list above is the asymptote.

Rejected alternatives: **big-bang** (fix ~1000 violations at once — unreviewable, blocks everything); **allow-list growth** (silently omits good rules ruff adds later, and contradicts the chosen ALL-minus-deny model).

The per-phase temp-ignore contents are a mechanical implementation detail (generated from current violations), **not** fixed by this spec.

## Phase sequence

Each phase is one plan/PR, lands green, and is added to (or already in) the default gate.

| Phase | Scope | Method | Acceptance |
|---|---|---|---|
| **0 — Formatter** | `line-length = 100`, `quote-style = "double"`; `ruff format` the whole tree (src + tests); doc8 → 100 | One mechanical reformat | `ruff format --check` clean; `make docs` green; unit suite green (no behavior change) |
| **1 — Strict scaffold + gate** | `select = ["ALL"]`; `ignore` = principled deny-list + temp-ignore; tests `per-file-ignores`; `ruff check --fix` the auto-fixable debt; wire `lint` + `format --check` into `nox.options.sessions` + `make lint`/`make format`/`make ci` | autofix + config | `ruff check` + `ruff format --check` green; `nox -s lint` in default sessions; gate green |
| **2 — Bug/simplify** | un-ignore `B, C4, SIM, PIE, FLY` | un-ignore → autofix → manual → green | batch clean; gate green |
| **3 — Modernize** | un-ignore `UP` (minus `UP037`), `RUF`, `PERF`; evaluate `RUF012` vs pydantic | same | batch clean; gate green |
| **4 — Naming/bug-class** | un-ignore `N` (incl `N806`), `DTZ`, `BLE`, `SLF001`/`PLR2004` (src), `S` (src; tests exempt) | same | batch clean; gate green |
| **S — Suppression audit** | adopt `PGH003`/`PGH004` (force `# type: ignore[code]`); audit the ~30 pyright-era `# type: ignore` (NOT honored by ty) — delete dead ones, migrate genuinely-needed ones to `# ty: ignore[code]`; decide whether to drop the dormant `[tool.pyright]` config; re-apply `BLE001`/`ARG002` *intent* (as real handling or plain comments) at the sites whose `# noqa` were stripped in Phase 1a when those rules ratchet in | un-ignore → audit → green | gate green; no unused/dead suppressions; `ty`'s `unused-ignore-comment` clean |
| **D — Documentation** (tail) | un-ignore `D100–D104` (and decide `D105`/`D107`); document modules/classes/methods | large standalone | gate green; docs build green |
| **A — Annotations** (tail) | un-ignore `ANN` (minus `ANN401`); add real 3.10 annotations + module-top imports (never `__future__`/`TC`/`UP037`) | large standalone | gate green; **re-verify `make docs` green** |
| **ty — tests** (optional) | drop `tests` from `[tool.ty.src] exclude`; resolve resulting errors | separate lever | `make typecheck` green incl. tests |

**End state:** `select = ["ALL"]`, `ignore` = only the principled deny-list, tests relaxed per-dir, and **lint + format + typecheck all in the default nox/make gate**.

## ty's role

ty is already maximal (`all = "error"`) and in the gate. No config change is needed except to keep it there and adopt new ty rules deliberately as they appear (exactly as #55 did — ty rules are type-aware and ruff structurally cannot enforce them). The only optional escalation is extending ty to `tests/` (phase ty).

## Known interactions / risks

- **`UP037` (denied):** confirmed unsafe against otto's quoted forward-refs (e.g. `-> "ScpFileTransfer"`). Permanently denied.
- **`RUF012` mutable-class-default:** tends to false-positive on pydantic model fields (which are not `ClassVar`). Evaluate in phase 3; per-rule ignore or `ClassVar` annotations as appropriate.
- **`S101` assert (src):** kept enabled for `src`; the ~29 existing asserts must be judged in phase 4 — converted to real errors where they are production invariants (asserts compile out under `-O`), or `# noqa: S101` where they are genuine internal checks.
- **Pre-existing `from __future__ import annotations`:** a few files (e.g. `transfer/ftp.py`) already carry it. The `FA` deny means we never *force* it; whether to strip the existing ones is a separate, out-of-scope cleanup.
- **Format churn at line-length 100:** larger than at 120 (more wraps). Phase 0 absorbs it in one isolated, mechanical commit so later phases review cleanly.
- **`ruff format`/autofix vs the type checker (learned in Phase 0/1a):** reformatting splits one-line statements, which **orphans line-specific `# ty: ignore` directives** from the lines ty attributes errors to; and the `B010` autofix rewrites deliberate `setattr(x, "__signature__", v)` → `x.__signature__ = v`, **re-exposing** ty errors that `setattr` hid. Both broke `ty check` while `make docs`/unit stayed green. **Every phase's verification (and the final review) MUST run `make typecheck`** — formatting/lint changes are not type-checker-neutral. ty's `unused-ignore-comment` rule (at `all = "error"`) is the backstop that catches orphaned directives.
- **Stripped `# noqa` intent (Phase 1a):** `RUF100` removed `# noqa: BLE001`/`ARG002` comments that became "unused" once those rules were TEMP-ignored, losing inline rationale for deliberate broad-excepts / unused-args. Recoverable from git; **re-applied in Phase S / when each rule ratchets in** (Phases 2/4).

## Out of scope / non-goals

- Refactoring otto's deliberate patterns to satisfy denied rules (relative/lazy imports, f-string logging, terse raises).
- Forcing `from __future__ import annotations` or `TYPE_CHECKING`-hidden imports.
- Stripping existing `__future__` imports (separate cleanup).
- pyright (ty is the trialled type checker; pyright config is left as-is).

## Success criteria

1. `select = ["ALL"]` with only the principled deny-list remaining in `ignore`.
2. `ruff check` + `ruff format --check` + `ty check` all green and all in the default `nox`/`make` gate.
3. Sphinx `-W` docs build stays green throughout (no regression from annotation/import churn).
4. The suite passes on Python 3.10 at every phase.
5. Each deviation from strict defaults is a documented `ignore` entry with a rationale.

## Process note

Per the established otto workflow, implementation lands **stage-only** (agents never commit; the prepare-commit-msg hook needs `/dev/tty`). This spec is written to disk for Chris to commit.
