# Coverage exclusion rules that move the numbers

**Status:** approved design, not yet planned.
**Breaks:** `[coverage.exclusions] markers` (removed outright; see §2).
**Store format:** bumps `STORE_FORMAT_VERSION` 6 → 7.

## Motivation and provenance

`[coverage.exclusions] markers` exists today and does nothing to any
percentage. Verified against source during the brainstorm of 2026-08-23:

1. `otto.coverage.exclusions.scan_excluded_lines` is called from exactly one
   place — `_build_file_chunk` in `src/otto/coverage/renderer/spa_data.py`
   (line 520) — at **render** time, and its result is assigned to
   `FileRecord.excluded_lines` as a side effect of building a chunk (line 524).
2. `_file_stats` sets `stats["lines"]["total"] = len(lines)` over every
   `LineRecord` the file has. `excluded_lines` reaches only
   `stats["flags"]["excluded"]` (line 189), a display pill. **An excluded line
   is still in the denominator.**
3. The built-in `LCOV_EXCL_*` markers do move the numbers, but only because
   `geninfo` strips them before otto parses anything — which helps solely on
   the `.gcda` path otto captures itself. A harvested `.info` produced by
   someone else's CI keeps its marked lines.
4. `docs/guide/cli/cov/exclusions.md` states the gap plainly: custom markers
   are "render-only today ... The line still counts toward the coverage
   percentages; only its visual presentation changes."

A substring that can only be placed in a comment is also the weakest possible
matcher. Real projects need to exclude debug code compiled *into* the coverage
build, vendored and generated trees, and platform arms the lab cannot reach.

The promise this spec implements: **an exclusion rule removes lines and
branches from the measured data, uniformly across every ingest path, and the
rule language is strong enough to name a preprocessor arm.**

## Decision record (2026-08-23)

| decision | choice | rejected |
| --- | --- | --- |
| Where exclusion applies | **Filter the merged `CoverageStore`** after all folds, before attribution. | Rewriting `.info` before load (structurally blind to the `capture.json` and manual-store paths — `_load_captures` / `_load_manual_store` never touch a tracefile at report time; fixing that means baking rules into stored artifacts, so editing a rule stops applying retroactively). Pushing markers into `geninfo --rc` (covers only the path otto captures, cannot express arms or globs, couples to lcov 2.x rc names). |
| Excluded records | **Deleted** from the store. | Flagged with a state honored by each stats path (every call site becomes a place to forget; a new one regresses silently). |
| Rule vocabulary | `marker`, `preprocessor`, `path`, `regex`. | Function-name patterns (lcov `FN` records carry a start line but no extent; ends would be guessed). |
| Config shape | Array of tables, `kind` discriminator. | Flat per-kind lists with `branch_*` twins (a wart that grows per kind). Per-kind lists with string-or-table entries (two spellings for one rule). |
| Arm scope | **The matching arm only.** | Whole `#if`…`#endif` (silently drops the production `#else` and nothing in the report says so). A per-rule `arms` knob (surface nobody asked for). |
| Complex `#if` | `macros`, matched by **positive reference** with negation parity. | Evaluating conditions against a declared macro environment (§9). Regex-only (brittle across formatting, operand order, `defined(X)` vs `defined X`). |
| Branch-only rules | `stat = "line" \| "branch"` on every kind. | Line-level only (leaves `assert(x)` lines permanently short on branch coverage with no recourse but hand-marking). |
| Audit surface | **None.** Percentages change; nothing else is reported. | A per-rule accounting table plus a zero-match warning (offered and declined 2026-08-23; a mis-scoped rule is therefore invisible, accepted knowingly). |
| Migration | None. `OttoModel` sets `extra="forbid"`, so an old `markers` key fails validation naming the offending field. | A compatibility shim (otto has no users; breaking is cheap now and will not stay cheap). |

## 1. What a rule removes

A rule resolves to a set of source line numbers per file, then `apply()`
mutates the store:

- `stat = "line"` (default) — delete the `LineRecord`. Its branches go with it.
- `stat = "branch"` — clear `LineRecord.branches`, keep the line.
- `kind = "path"` — drop the whole `FileRecord`.

Deleting rather than flagging is what makes this correct **with zero
downstream changes**: `_file_stats`, the directory rollups, per-ticket
coverage, overrides, `store.json` and the SPA all compute over records that
are simply not there.

## 2. Config surface

`markers` is removed. The replacement is an array of tables:

```toml
[[coverage.exclusions.rules]]
kind = "marker"
name = "MYPROJ_NO_COV"   # family: _LINE / _START / _STOP

[[coverage.exclusions.rules]]
kind = "preprocessor"
macros = ["DEBUG_LOG", "TRACE_VERBOSE"]

[[coverage.exclusions.rules]]
kind = "preprocessor"
pattern = '#if 0'

[[coverage.exclusions.rules]]
kind = "path"
patterns = ["vendor/**", "**/*_generated.c"]

[[coverage.exclusions.rules]]
kind = "regex"
pattern = '\bassert\('
stat = "branch"
```

Every rule accepts `stat`, defaulting to `"line"`. Patterns are Python `re`
syntax; the docs must show TOML literal strings (single quotes) so backslashes
need no escaping.

Validation lives in `CoverageExclusionsSpec` (`src/otto/models/settings.py`) as
a discriminated union on `kind`. A `preprocessor` rule must set **exactly one**
of `pattern` / `macros`. Every regex compiles at load; a bad one fails loud
naming the rule index and the `re.error`, before any coverage work starts.

## 3. Rule kinds

### `marker`

`name` is the family **base**, and otto derives the members exactly the way
lcov derives its own:

| | `stat = "line"` | `stat = "branch"` |
| --- | --- | --- |
| single line | `{base}_LINE` | `{base}_BR_LINE` |
| block open | `{base}_START` | `{base}_BR_START` |
| block close | `{base}_STOP` | `{base}_BR_STOP` |

So `name = "MYPROJ_NO_COV"` yields `MYPROJ_NO_COV_LINE` / `_START` / `_STOP`.
This is the robustness upgrade over the old flat substring — custom markers
finally get block form, and the branch family falls out of the same base
instead of needing a second declaration.

It also means **the built-in set is not a special case**: `LCOV_EXCL_LINE`,
`LCOV_EXCL_START`, `LCOV_EXCL_STOP` and their `_BR_` counterparts are exactly
what `MarkerRule(name="LCOV_EXCL")` derives at each `stat`. The engine carries
them as two always-on rules rather than as hardcoded tuples, which is the
strongest available evidence that the family model is the right shape.
Consequence: **otto now enforces the standard markers itself**, so they work on
harvested tracefiles, not just on captures otto ran `geninfo` for.

Note this is a behavior change beyond the config rename: the old `markers`
entry matched its string *bare*, so a project writing `// MYPROJ_NO_COV` must
now write `// MYPROJ_NO_COV_LINE`. Deliberate — one convention, lcov's, rather
than two.

Matching stays substring-based, resolved **longest token first** at each
position. (The current `_marker_events` uses bare `str.find` per marker and can
fire two events for one token; harmless today because both outcomes exclude the
line, but it is a trap once families are user-named. Pin it with a test.)

Two families can still collide: base `FOO` at `stat = "branch"` derives
`FOO_BR_LINE`, and so does base `FOO_BR` at `stat = "line"`. Identical strings,
so longest-first cannot separate them. **Config validation rejects any two
rules whose derived token sets intersect**, naming both rule indices.

### `preprocessor`

Excludes the arm whose own directive matches. Two spellings:

**`pattern`** — a regex searched against the *normalized logical directive
line*: continuations joined, the `#`-to-keyword gap collapsed, so
`#  ifdef  X` and a backslash-continued condition both present as `#ifdef X …`.
Surgical; the right tool for `#if 0`.

**`macros`** — a list of names. The condition expression is tokenized and each
identifier's **negation parity** tracked; the arm is excluded when a listed
macro appears at even parity (positive):

```
#if defined(A) && defined(DEBUG_LOG)    DEBUG_LOG positive  → excluded
#if defined(A) && !defined(DEBUG_LOG)   negated             → kept
#ifndef DEBUG_LOG                       negated             → kept
#if DEBUG_LOG > 2                       positive            → excluded
#if !(A && !defined(DEBUG_LOG))         double negation     → excluded
#elif defined(DEBUG_LOG)                positive            → excluded
#else                                   no condition        → never matches
```

Parity needs a token scan and a paren stack, **not a parse tree**: `!` applies
to the next primary (identifier, `defined(...)`, or parenthesized group).

**Why no evaluator.** otto never needs to know which arm the preprocessor
selected, because a dead arm has no `DA:` records — gcc emitted no code for it,
so deleting it is a no-op. This single observation removes the need for a macro
environment, for build-system integration, and for agreeing with the build
about anything.

**"Matching arm" is defined against the rule, not against arm selection.** otto
does not evaluate the condition and has no notion of which arm was compiled. An
`#else` arm is *selected* whenever its `#if` chain is false — that is ordinary
and frequent — but it carries no expression, so no rule can name it. Both
polarities land correctly, and the dead-arm observation is what makes the
bottom-left cell harmless:

| build state | `#ifdef DEBUG_LOG` arm | `#else` arm |
| --- | --- | --- |
| `DEBUG_LOG` defined | live, rule matches → **excluded** | dead, no records — nothing to delete |
| `DEBUG_LOG` undefined | dead, rule matches → deletes nothing | live, unnameable by any rule → **kept** (the production path) |

Arm extent: from the matching directive through the next directive at the same
nesting depth, exclusive. Nested constructs inside a matched arm are excluded
with it. The directive lines themselves are marked excluded — never executable,
so this is display-only consistency.

### `path`

Globs anchored to the `[coverage]` repo root via the existing `anchor_path()`
convention (`src/otto/utils.py`), matched against the store path made relative
to that root. Files outside the root are matched on their absolute path only.
Matching files are dropped whole and never opened.

Python's floor here is 3.10, so `PurePath.full_match` (3.13+) is unavailable
and `pathspec` is not a dependency. Translate the glob to a regex in-house:
`**/` → `(?:.*/)?`, `*` → `[^/]*`, `?` → `[^/]`. Roughly fifteen lines, fully
testable, no new runtime dep.

### `regex`

Searched against each raw source line.

## 4. Interaction between rules

Rules **union**; there is no precedence to reason about. `path` rules
short-circuit — an excluded file is never read. A line excluded at
`stat = "line"` subsumes any branch-level exclusion on the same line.

## 5. The scanner

All four kinds share **one pass** per source file, because they need the same
lexical state:

- block-comment tracking, so a `#if` inside `/* … */` is not a directive
- line-comment handling
- backslash continuations, joined into one logical directive
- `#if` / `#ifdef` / `#ifndef` → `#elif` / `#else` → `#endif` depth tracking
- leading whitespace before `#`, and whitespace between `#` and the keyword

Output per file: `(excluded_lines, branch_excluded_lines)`.

No file-extension gate. A `#ifdef` pattern cannot match a non-C source anyway,
and lcov data is C-family by construction.

## 6. Package and pipeline placement

`src/otto/coverage/exclusions.py` becomes a package:

- `rules.py` — the rule models plus `load_exclusion_rules(cov_config, sut_dir)`,
  following the `load_ticket_spec` / `load_override_config` precedent where the
  typed spec validates and the coverage package re-reads the raw dict.
- `scan.py` — the single-pass scanner of §5.
- `apply.py` — resolve to an `ExclusionMap` (`{path → (lines, branch_lines)}`)
  and mutate the store.

`CollectionInputs.extra_markers: list[str]` becomes
`exclusion_rules: list[ExclusionRule]`. Both readers of `[coverage.exclusions]`
switch to the loader: `src/otto/cli/cov.py` (`_resolve_cov_settings`) and
`src/otto/suite/run.py`.

`CoverageReporter.run()` gains a step between `_fill_tier_colors()` and the
`repo_root`-gated attribution block. That position is load-bearing in three
ways:

1. **After every fold**, so `.gcda` captures, harvested tiers, `capture.json`
   e2e captures and the manual store are all filtered by one pass.
2. **After the dirty-tree remap** in `_load_captures`, which rewrites capture
   hits from HEAD into working-tree coordinates. The scan reads working-tree
   source, so the line numbers it deletes are in the same coordinate space.
3. **Before attribution**, so an excluded line never reaches per-ticket
   coverage or override resolution.

Import the package lazily inside `run()`, as `SpaRenderer` already is — the
`cov` import-budget surface is measured and an eager import would charge it.

Source root for the scan: `self.repo_root` when set, else `self.source_root`.
Store keys are absolute; the scan reads `fr.path` directly, as the renderer
does today.

## 7. Store and renderer contract

`FileRecord.excluded_lines` stays, now populated by the filter stage. Add
`branch_excluded_lines`. Because records are deleted, `excluded_lines` holds
line numbers with no `LineRecord` behind them — which is fine: grey rendering
keys off the source-line list in `chunk.excluded`, never off line records, and
`stats["flags"]["excluded"]` is a `len()` of the set.

`STORE_FORMAT_VERSION` bumps 6 → 7 under the existing no-migration-shim policy
(a mismatched file fails loud in `CoverageStore.load` telling the caller to
regenerate).

`branch_excluded_lines` is serialized into `store.json` but **not** emitted to
the SPA chunk, because nothing renders it. The frontend contract is therefore
untouched: only the numbers move.

The renderer stops scanning source and stops mutating the store while building
a chunk; `_build_file_chunk` reads `fr.excluded_lines` instead.

### A guard this design deliberately reverses

`tests/unit/cov/test_pipeline.py` asserts
`not hasattr(CoverageReporter, "_apply_exclusions")`, pinning "exclusion
display is render-time, not baked into the store". Its stated rationale: a
single-valued `LineRecord.state` cannot express "excluded always wins" over
covered / stale / aging.

**That rationale does not apply here.** This design deletes records rather than
assigning a state, so there is no precedence to express. The plan must update
that guard and its docstring with this reasoning — not route around it, and not
delete it silently. The comment at the render step in
`src/otto/coverage/reporter.py` ("Exclusion display is render-time (spec
§8/§9) … the reporter never bakes state=excluded into the store") becomes false
and must be rewritten in the same change.

## 8. Failure modes

| condition | behavior |
| --- | --- |
| Source unreadable (`OSError`) | File keeps all lines; warn. Matches the renderer's existing tolerance. |
| Invalid regex in config | Fail loud at load, naming rule index and `re.error`, before any coverage work. |
| `preprocessor` rule setting both `pattern` and `macros`, or neither | Validation error. |
| Unbalanced `#if` (no `#endif`) | Treated as closed at EOF; debug-level note. Never raises. |
| A `path` rule removing every file | Empty store, empty tree. No special case. |
| `LineRecord` past current EOF (shrunk-file tolerance) | Never excluded — the scan only sees lines the current source has. Existing behavior preserved. |

## 9. Known limitations

Written down because none of them will be reported at runtime (§ decision
record: no audit surface).

1. **A mis-scoped or typo'd rule is invisible.** Nothing warns that a rule
   matched nothing, and nothing reports how many lines a rule removed.
   Declined knowingly 2026-08-23.
2. **`||` over-matches.** `#if defined(PROD) || defined(DEBUG_LOG)` excludes the
   arm even when `PROD` is what made it live. Disambiguating requires
   evaluation. Escape hatch: use `pattern`. The docs must say this rather than
   imply `macros` is exact.
3. **An `#else` arm is unreachable by any rule.** `macros` cannot name it (no
   expression) and `pattern` cannot either (no distinguishing text). This only
   bites when *both* arms carry records, which requires a store merging builds
   with different flags — reachable, since `LcovMerger` merges `.info` across
   hosts. Workaround: a source marker or a `path` rule. The real fix, if it ever
   matters, is per-config stores, not a smarter rule.
4. **No condition evaluation.** Deliberate: it would need the build's real
   defines (`compile_commands.json`, `gcc -dM -E`, Kconfig `autoconf.h`), a
   partial environment silently returns wrong answers because undeclared
   identifiers evaluate to 0 per C semantics, and it buys only limitation 2 —
   gcov already resolved liveness for us.
5. **A `#if` at line start inside a string literal** is read as a directive.
   Vanishingly rare; not handled.

## 10. Testing

Scanner units over fixed strings: nesting, `#elif` and `#else` arms,
continuations, directives inside block comments, marker families,
longest-token-wins, negation parity including double negation, and each
`stat = "branch"` variant.

Apply-stage units: `LineRecord`s deleted, branches stripped with the line kept,
whole `FileRecord`s dropped, `excluded_lines` and `branch_excluded_lines`
populated.

**The load-bearing test is a stats delta.** Build one store, run it through
`_file_stats` and the tree rollup with rules on and with rules off, and assert
the percentage *moves* — asserting that a filtered store reports some number is
a guard that cannot fail, and this repo has a recurring history of exactly
that. Every exclusion test must inject the hostile condition rather than
inherit it: a fixture that would pass with the filter stage removed proves
nothing.

An ordering test must prove excluded lines never reach per-ticket attribution,
since that depends on placement (§6) rather than on any single function.

## 11. Documentation ripple

- `docs/guide/cli/cov/exclusions.md` — the "render-only today" section becomes
  false. Rewrite around the four rule kinds, and state limitations 2 and 3
  explicitly.
- `src/otto/cli/init_templates.py` — the commented `[coverage.exclusions]`
  sample currently shows `markers = ["GCOV_EXCL"]`; replace with the rule-array
  form.
- Any `[coverage]` reference listing `exclusions.markers`.
