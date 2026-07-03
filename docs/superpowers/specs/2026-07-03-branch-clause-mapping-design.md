# Branch → boolean-clause mapping for the coverage HTML report — design

**Date:** 2026-07-03
**Status:** Phase 1 (frontend parity + docs screenshot) approved for
implementation **now**. Phases 2–3 (boolean-clause and switch mapping)
**deferred** — benefit does not justify cost at this time; the design is
retained here, probe-verified and ready, for future pickup.
**Roadmap item:** `todo/coverage_roadmap.md` — "linkage from boolean clauses
and branches that are taken"
**Builds on:** the clang gcov support effort (stamp-based toolchain
discovery, `worktree-clang-gcov-support`)

## Problem

The coverage HTML report renders one anonymous pill per gcov branch
outcome. For `if (a && b || c)` the user sees six pills whose tooltips say
`block=0 branch=3` — there is no way to tell *which clause* a partially
covered pill belongs to, so branch coverage is unactionable at exactly the
lines where it matters most.

Goal: annotate the rendered source so each boolean clause shows how well
its outcomes were exercised, and each pill names its clause.

## Verified ground truth (probed on gcc 13.3 / clang 18 / lcov 2.0)

These facts were established empirically on the dev VM and are the load-
bearing assumptions of the design:

1. **GCC emits exactly 2 branch outcomes per condition, in evaluation
   order.** `if (a && b || c)` produces 6 BRDA records on that line,
   pairing as (0,1)=`a`, (2,3)=`b`, (4,5)=`c`. Short-circuit is visible:
   with `a && b` deciding, `c`'s pair reports never-executed. Bare boolean
   expressions (`return x > 0 && x < 10;`) get pairs the same way.
2. **The true/false polarity of a pair is NOT fixed.** The true-edge was
   the fallthrough outcome for `a` but the jump outcome for `b` in the
   same expression. Counters alone support "N of 2 outcomes exercised",
   not "TRUE never seen".
3. **Clang's gcov emulation lacks per-condition counters.** The same
   source produced 4 outcomes (not 6) for the 3-condition line, and one
   pair (not two) for `x > 0 && x < 10`. Order-matching cannot honestly
   label clang-gcov data; no gcno/DWARF work recovers information the
   counters do not carry.
4. **gcno+DWARF (the roadmap's original sketch) is dominated.** For GCC
   the source-order guarantee makes binary parsing unnecessary; for clang
   the per-condition data is absent regardless. Linking gcno arcs to DWARF
   columns would require per-arch disassembly and breaks above `-O0`.
   Rejected.
5. **tree-sitter provides exact clause spans.** `binary_expression` nodes
   carry `operator`/`left`/`right` fields and byte/row/column spans;
   flattening a `&&`/`||` tree left-to-right yields leaves in evaluation
   order. Parsing needs no preprocessing, includes, or compile flags.
6. **GCC models a `switch` as one N-way branch on the switch line**: one
   outcome per unique case *target* — consecutive labels sharing a body
   (`case 1: case 2:`) collapse to a single outcome — in **strict source
   order, including an explicit `default`'s actual source position**; an
   implicit default is appended last. Verified for sparse (compare-chain)
   and dense (jump-table) switches, shared labels, unsorted case values,
   and default-in-the-middle. Source ordering means **no case-constant
   evaluation is ever needed** (`case FOO|BAR:` maps without knowing its
   value).

## Decisions (settled in brainstorming)

| Question | Decision |
| --- | --- |
| UX | **Clause-span highlighting**: tint each clause's source text by outcome coverage; pills keep working and gain clause labels. |
| Compiler scope | **GCC-first; clang-gcov degrades** to today's anonymous pills with an honest tooltip note. Clang native coverage-mapping ingest is a future effort (see Long view). |
| Source parsing | **tree-sitter** (`tree-sitter`, `tree-sitter-c`, `tree-sitter-cpp`), lazy-imported in the mapper only. |
| Claim level | **Outcome counts only**: 2/2, 1/2, 0/2, never-evaluated. No true/false attribution (fact 2). Polarity inference deliberately not built this effort. |
| Where mapping runs | **Report time, in the renderer** (approach A). Derived data, recomputed per report, nothing persisted; capture-time storage and gcno cross-checking rejected. |
| `switch` support | **Designed as phase 3, deferred with phase 2**: probe-verified semantics (fact 6) make it a contained generalization, not a gamble, whenever it is picked up. |
| Frontend testing | **Monitor-grade parity, and it comes FIRST (phase 1, the NOW scope)**: report JS becomes a TypeScript vite entry in `web/`, Vitest + Testing Library unit tests, Playwright browser suite over a rendered fixture report — all pinning the existing report before features build on it. Includes the coverage-guide screenshot via the docs-media pipeline. A React report viewer is future work, not this effort. |

## Architecture

### clause_mapper: a pure, liftable normalizer

New module `src/otto/coverage/renderer/clause_mapper.py`, exposing one
pure function:

```text
map_clauses(source: str, language: str, branches_by_line: Mapping[int, Sequence[BranchHits]])
    -> dict[int, list[ClauseAnnotation]]
```

The `language` argument is `"c"` or `"cpp"`, chosen by the renderer from
the file extension.

No I/O, no lab, no globals: source text and branch records in, clause
table out. This purity is a **design requirement**, not a style choice —
the Long view (below) may relocate this function from render time to
pre-merge normalization, and that must be a move, not a rewrite.

`ClauseAnnotation` (frozen pydantic dataclass, per house style):

- `key`: stable clause identity — `(decision_line, decision_col, leaf_index)`.
  Chosen so it is reproducible from source alone; it is the future
  cross-compiler merge key (Long view).
- `line`, `col_start`, `col_end`: the clause's span on its own line.
- `text`: the clause source text (tooltip label, truncated for display).
- `outcomes_covered`: 0 | 1 | 2, plus per-tier values mirroring how pills
  already consume `BranchHits` per tier.
- `state`: `full` (2/2) | `partial` (1/2) | `missed` (0/2, reached) |
  `unevaluated` (pair present but never reached — short-circuited).
  Switch targets (phase 3) use only `full`/`missed`.
- `kind`: `clause` | `case` | `default` — drives tooltip wording
  ("`b` — 1/2 outcomes" vs "`case 50:` — never taken").

Lines with no mapping produce no annotations; there is no "unmapped"
annotation object — absence is the fallback signal.

### Decision extraction rules

A **decision root** is any of:

- the condition of `if`, `while`, `do…while`, `for` (middle clause),
- the condition of a ternary (`conditional_expression`),
- any `&&`/`||` `binary_expression` whose parent is not itself a
  `&&`/`||` expression (covers `return a && b;`, assignments, args).

**Leaves** of a decision are its maximal subexpressions that are not
`&&`/`||` nodes, collected left-to-right (= evaluation order).
A parenthesized sub-tree is transparent; a function call, macro
invocation, or comparison is one leaf regardless of its internals.
A single-leaf decision (`if (x)`) is a valid decision with one clause.

### `switch` mapping (phase 3)

Ships after boolean clauses are green, in the same effort:

- **Extraction**: tree-sitter `switch_statement` → condition span + case
  labels; consecutive labels sharing a body form one target group; an
  explicit `default:` is a group at its source position; when absent, an
  implicit-default entry is synthesized (appended last, per fact 6).
- **Annotations are cross-line**: all outcomes attach to the `switch`
  line, but the highlightable spans (`case 50:`) live on their own
  lines. `ClauseAnnotation` already carries its own `line`, so the
  mapper may return annotations for lines that have no `BranchHits` of
  their own.
- **States are binary**: a case target is `full` (taken) or `missed`
  (never taken) — no pairs, no `partial`, no `unevaluated`. The implicit
  default has no source span; it surfaces only in the switch-line
  tooltip ("default — never taken").
- GNU case ranges (`case 1 ... 5:`) are one label, therefore one leaf;
  if a compiler emits per-entry outcomes instead, the signature
  mismatches and the decision quarantines — safe by construction.

Until phase 3 lands, switch lines fail the signature match by
construction and keep anonymous pills.

Multi-line decisions: each leaf is annotated on the line its span starts
on, matching GCC's habit of attaching a condition's pair to the
condition's own line. Count-matching (below) happens per line, but a
decision spanning lines is quarantined as a whole: if any line it
touches mismatches, every line of that decision is unmapped — one bad
line is evidence the decision's codegen deviates from the model.

A line may hold leaves from more than one decision (`if (a) x = b && c;`).
The line's leaf list is all leaves from all decisions on it, in source
order — which is evaluation order for the BRDA records too.

### Matching and gating

Each decision contributes an **expected outcome signature** to its line:

- **Boolean decision**: `2 × leaves` outcomes; leaf *i* takes outcomes
  (2i, 2i+1).
- **`switch` (phase 3)**: one outcome per unique case target in source
  order, plus one appended for the implicit default when no explicit
  `default:` exists.

Per source line: **map only when the line's `BranchHits` count equals
the sum of its decisions' signatures** — outcomes are then dealt to
decisions in source order. Any mismatch (optimized build, macro
expanding to multiple conditions, clang-gcov, compiler drift) leaves the
line unmapped: today's anonymous pills, no error, no partial guesses.

Report-level gate: if any toolchain recorded in `.otto_cov_meta.json`
resolves to an llvm-family gcov (name matches `llvm-cov(-N)?`, reusing
the discovery module's matcher), clause mapping is **disabled for the
whole report**; pills keep today's tooltips, with one added note: "per-
condition data not available from clang's gcov format". Rationale: the
merged `.info` cannot attribute BRDA records back to hosts, and clang
pairs can coincidentally count-match (single-clause lines are fine, but
we do not rely on luck). Mixed-bed users lose nothing they have today.

File-level gates: extension not recognized as C/C++ → skip; tree-sitter
import or grammar load failure → skip all files, log once at debug;
parse errors in a file → tree-sitter still yields a tree, and the
count-match gate quarantines any damage to the affected lines.

### Renderer and UI integration

`html_renderer.py` calls `map_clauses` per file (it already holds the
source text and per-line `BranchHits`) and threads annotations into the
line render model:

- Clause spans become `<span class="clause clause-{state}">` wraps inside
  the existing source markup; four tints (green/orange/red/grey) defined
  in both light and dark themes.
- Pills gain the clause label: "`b` — 1/2 outcomes", with the existing
  per-tier breakdown lines preserved beneath.
- Hovering a clause span highlights its pill and vice versa (shared
  `data-clause-key` attribute). The interaction JS is written in the
  phase-1 TypeScript entry — the frontend harness exists before this
  phase starts, so no throwaway vanilla JS is ever written.
- Aggregate numbers (line/branch percentages, tier tables) are **not
  derived from annotations** — they keep coming from `BranchHits` as
  today, so mapping can never change a coverage number.

### Frontend: build and testing parity with the monitor (phase 1)

Requirement from review: the coverage-report frontend must be tested
with the **same methodology and tools as the monitor dashboard**, and
this parity lands **first** — proving the existing report sane before
features are added on top. Today it has no frontend testing — 53 lines
of vanilla `report.js` (table sort), Jinja templates, and Python-side
string assertions only.

- **TypeScript in `web/`**: report JS moves to a second vite entry
  (`web/src/covreport/`, plain TS + DOM — no React; a static report
  page does not need a framework), built into the renderer's static
  assets the same way the monitor builds into `monitor/static/dist/`.
  It inherits the existing lanes: `tsc`, lint, air-gap URL gate,
  dist-guard (wheel must fail loudly if the built assets are missing),
  and the CI web lane.
- **Vitest + Testing Library + jsdom** unit tests beside the source
  (`web/src/__tests__/` pattern). Phase 1 pins existing behavior: table
  sort, tier display, pill tooltips. Phases 2–3 extend the same suite:
  clause↔pill hover linkage, tooltip content assembly, case states.
- **Playwright browser suite** `tests/e2e/cov/report_browser/`
  (pytest + `playwright.sync_api`, `hostless` + `browser` markers, own
  `xdist_group`, mirroring `tests/e2e/monitor/dashboard/`): a fixture
  report rendered by `html_renderer` from checked-in store data is
  driven in Chromium. Phase 1 pins today's report — sorting, tier
  columns, pill rendering, light and dark themes. Phases 2–3 add:
  clause hover highlights its pill and vice versa, clause/case states
  render with the right classes, the llvm-gate note appears when
  mapping is disabled. Runs in the browser-suite process alongside the
  dashboard tests so the coverage gate folds it in via the existing
  `--cov-append` flow.

The existing Python string-assertion e2e tests remain — they pin file
structure and content; the browser suite pins behavior.

### Dependencies

`tree-sitter`, `tree-sitter-c`, `tree-sitter-cpp` — prebuilt wheels,
runtime deps of the package, imported lazily inside `map_clauses` so
`import otto` and CLI startup are unaffected (import-budget snapshots
must not change). The getting-started dependency table gains the three
rows (drift test enforces this).

The frontend work adds **no Python dependencies**; it reuses the `web/`
workspace's existing toolchain (vite, vitest, TypeScript, Playwright
binaries already installed via `make browsers`).

## Long view: compiler-agnostic branch combination (not built now)

Chris's requirement to keep in mind: arbitrary products with arbitrary
toolchains, gcc and clang coverage combined, agnostic to compiler
versions.

Key observation: **mixed-compiler branch merging is already unsound
today**, before this design. `lcov --add-tracefile` and otto's own
`BranchHits` per-tier merge both key branches by `(line, block, branch)`
— gcov-internal ids with no cross-compiler (or even cross-version)
meaning. A gcc host's branch 2 and a clang host's branch 2 on the same
line are different branches summed as one.

The only stable cross-compiler identity for a branch is **source
semantics** — which is exactly `ClauseAnnotation.key`. The long-view
architecture this design points at:

```text
per-format normalizers            canonical space          merge & render
gcc-gcov BRDA  ──┐
clang coverage-  ─┼──► clause-space records ──► merge by clause key ──► report
mapping (future) ─┘    (file, decision pos,
                        leaf index, outcomes)
```

- The v1 mapper **is** the gcc-gcov normalizer, deliberately pure and
  relocatable from render time to pre-merge.
- Clang's native `-fprofile-instr-generate -fcoverage-mapping` format
  carries exact per-condition source regions (and MC/DC); its future
  ingest normalizes into the same clause space — that is the honest path
  to clang parity, not gcov emulation.
- GCC 14's `-fcondition-coverage` / `gcov --conditions` can later upgrade
  claim fidelity (true/false per condition) inside the same model.

None of this is implemented now; the deliverables of this effort are the
clause space's identity scheme and the mapper's purity, which make the
above incremental instead of a rework.

## Error handling

Universal rule: **no code path exists where clause mapping changes
coverage numbers, exit codes, report generation success, or existing pill
rendering.** Every failure mode — missing grammar, unparseable file,
count mismatch, llvm gate — degrades to the current report. Mapping
coverage ("mapped N of M branch-bearing lines") is logged at debug for
diagnosability.

## Testing

- **Mapper unit tests** with real captured fixtures (the probe's BRDA
  records from gcc 13 checked in as literals): single clause, `&&`/`||`
  chains, ternary, bare-expression decisions, multi-line decisions,
  short-circuit `unevaluated`, per-tier outcome counts, count-mismatch
  fallback, parse-error fallback, C++ file via tree-sitter-cpp.
- **Extraction unit tests**: decision-root and leaf rules on
  representative snippets (macros-as-leaf, parenthesization, nested
  ternary, single-leaf `if (x)`, `for` middle clause).
- **Switch fixtures (phase 3)**, from the probe programs checked in as
  literals: sparse, dense (jump table), shared labels, unsorted case
  values, default-in-the-middle, implicit default, case ranges
  (quarantine), switch condition containing `&&` (signature sums).
- **Frontend (phase 1, extended by 2–3)**: Vitest unit tests per
  behavior; Playwright suite as specified above; dist-guard test for
  the built assets.
- **Renderer tests**: annotations produce the span classes and
  `data-clause-key` linkage; llvm gate replaces tooltips with the note;
  numbers identical with mapping on/off.
- **E2E**: extend the existing gcc-based coverage e2e product with a
  multi-clause function driven so one clause is partial and one
  short-circuited; assert the report HTML contains the expected clause
  spans and states. Clang e2e asserts the degraded (anonymous-pill)
  report still renders.
- **Import budget**: unchanged snapshots prove laziness.

## Phasing

Ordering principle (from review): **prove sanity first, then add
features** — the existing report gets monitor-grade testing before any
new behavior is built on it.

1. **Phase 1 — NOW**: frontend parity on the *existing* report — TS
   entry in `web/`, Vitest suite, Playwright browser suite, build
   lanes, pinning today's behavior (sort, tiers, pills, themes) — plus
   a coverage-GUI screenshot on the coverage guide page via the
   build-time docs-media pipeline (below). One approved feature
   addition rides along: **`otto cov report --prefix`**, a
   display-only path-root strip (the `genhtml --prefix` analogue) —
   file paths under the prefix display relative, links and store keys
   stay full-path. It improves real reports (which today show raw
   absolute build paths) and lets the screenshot show
   `product/main.c` instead of a temp-dir path without any chdir
   tricks in the capture pipeline.
2. **Phase 2 — DEFERRED**: boolean clause mapper, gates, renderer/UI
   integration; when picked up, its UI lands with Vitest + Playwright
   coverage from day one on the phase-1 harness.
3. **Phase 3 — DEFERRED**: `switch` mapping (signature generalization,
   label grouping, cross-line annotations, implicit default).

Phase 1 gets an implementation plan now; phases 2–3 wait until the
feature's benefit justifies the cost, with this design (and its
probe-verified facts) as the starting point.

### Docs screenshot (phase 1)

The coverage guide (`docs/guide/coverage.md`) gets a screenshot of the
report GUI, following the monitor guide's pattern exactly:
`scripts/capture_docs_media.py` gains a coverage capture — a fixture
report rendered by `html_renderer` from deterministic checked-in store
data (the same fixture the Playwright suite drives, rendered with
`prefix=` so displayed paths are the short, deterministic
`product/main.c` form), screenshotted by headless Chromium into
`docs/_static/generated/` at docs-build time.
Never committed, regenerates when the renderer or fixtures change
(stamp inputs gain the coverage renderer), placeholder mode keeps a
broken-browser docs build degraded rather than blocked, and the
artifact joins the script's promised `ARTIFACTS` list.

## Out of scope (this effort)

- True/false polarity inference (revisit after real-world use of counts).
- Clang native coverage-mapping ingest (future normalizer; see Long view).
- GCC 14 condition coverage (`--conditions`) ingest.
- Moving the mapper pre-merge / clause-space merging.
- A React port of the report viewer (report data as JSON + viewer app —
  natural follow-on to the phase-1 frontend work, deliberately its own
  future effort).
- Non-C/C++ languages.
