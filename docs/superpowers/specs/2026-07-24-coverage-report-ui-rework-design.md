# Coverage report UI rework — design

**Date:** 2026-07-24
**Status:** Draft for review
**Interactive mockups:** `docs/superpowers/specs/assets/2026-07-24-coverage-ui/` (open in a
browser; self-contained). These are the approved reference for layout and interaction.

## 1. Goal

Replace the Jinja-generated coverage HTML report with a React SPA that has functional and
aesthetic parity with the otto monitor web app: same tokens, same dark mode, same component
vocabulary. Every technical feature of the current report is preserved. New capabilities:
directory tree pages with rollup stats, real routing, per-line context drilldowns with host
identity, a runs/contexts page, a report-wide context focus filter, configurable coverage
thresholds, and CI hostability.

### Non-goals (documented, deliberately out)

- **Per-ticket coverage** — future work. Tickets are **per code commit** (derived from git
  authorship/commit history, e.g. blame → commit → ticket), *not* per run. The file page
  reserves a gutter column **left of the line numbers** for a per-line ticket, collapsed
  until that plumbing exists. The schema keeps a slot for it.
- **Decision coverage data** — no producer exists. The stats schema and UI carry a
  type-extensible slot; decision columns render "no decision data" until real data arrives.
- **File splits and duplicated files** — validity follows renames only as far as
  `git diff -M` tracks them. Splits/copies are documented as "re-prove coverage".
- **Merge-conflict resolution validity** (capture on a side branch, HEAD merges it with
  conflict resolutions differing from both parents) — queued as the next validity work item.
- **PRO Untitled UI components** — not purchased; see component sourcing policy (§5).

## 2. Delivery model

**Static, self-contained, `file://`-first.** `otto cov report` output is a directory that
renders anywhere a browser can reach it, with no server and no Node at report time:

- The SPA is built once by `make web` (a second Vite app in the `web/` workspace,
  `web/src/covapp/`) into the coverage renderer's static dir, replacing the current
  `covreport.js` IIFE lane. The wheel embeds it (`make wheel-check`).
- Report generation = copy the prebuilt bundle + emit data files (§7). Python only.
- All asset paths are **relative** (`./assets/…`), so the report works at `file://`, any
  Jenkins job URL depth, or a GitLab Pages subpath.
- Zero network fetches (existing air-gap rule): fonts bundled, no CDN, no telemetry.

### CI hosting (explicit requirement)

- **GitLab**: works out of the box — Pages site or the artifacts browser.
- **Jenkins**: stock CSP for archived HTML blocks all JS. We minimize the required
  relaxation and document it: **no inline `<script>`, no `eval`, no WASM** (Shiki must use
  its JS-regex engine), so `script-src 'self'` suffices via the HTML Publisher +
  `hudson.model.DirectoryBrowserSupport.CSP` mechanism.
- **Gate**: a browser test serves the built report from a subpath **with the documented
  minimal CSP header** and asserts the app boots. A stray inline script can never silently
  regress Jenkins support.

## 3. Routing

Hash routing (wouter + `useHashLocation`, as in the monitor) — no server rewrite rules,
deep links work on any static host:

| Route | Page |
| --- | --- |
| `#/coverage` | Directory page at repo root |
| `#/coverage/<repo-relative dir>` | Directory page scoped to that subtree |
| `#/coverage/<repo-relative file>` | Annotated source page |
| `#/runs` | Runs & contexts page |

`#/runs` is outside the `#/coverage/…` namespace so it can never collide with a real
directory. Context focus encodes into the query (`?ctx=<run-label>`) so a focused view is
shareable/bookmarkable. Unknown paths render a not-found page with a link to `#/coverage`.

## 4. Pages and chrome

The three approved mockups are the layout/interaction reference. Summary of what they pin:

### Shared chrome (every page)

- **App bar**: `⬡ otto coverage · <repo>` brand mark; **focus chip** (violet, dismissable ✕)
  visible whenever a context focus is pinned; theme toggle; **⋮ overflow menu** containing:
  keyboard shortcuts, the **Focus context** switcher (a vendored UUI **Select** listing
  "All contexts" + every run, ✓ on active), and the **coverage key rendered inline as
  informational menu content** (tiers, states, branch pill legend) — not a toggle, not a
  floating panel.
- **Breadcrumbs** (the existing `ui/Breadcrumbs.tsx`): home crumb = repo root, one crumb per
  path segment, each routing to its directory page; current node highlighted.
- **Stats card, upper right**: tier × type matrix — rows: each tier in precedence order +
  "All tiers"; columns: Line, Branch, Decision. Cells: percentage (threshold-colored) +
  `hit/total`. Scope line names the current node; stats cover **the node and everything
  inside it** (repo root on the main page).
- **Thresholds** (gcovr-style, configurable): `[coverage.report] high = 80`, `medium = 70`.
  ≥ high → green, ≥ medium → yellow, below → red. Applied to all percentage text and bars.
- Toasts for transient feedback; page title + meta line (file/line counts, generation
  timestamp, otto version).

### Directory page

- Tree view of the covered source tree from the current node down, one row per child
  (dir or file): chevron (expand/collapse), folder/file icon, name (dirs drill in,
  re-scoping breadcrumbs + route + stats card), then stat columns:
  **Lines (hit/total) · Line % (threshold bar) · Branch % (threshold bar) · one column per
  tier (%) · flag badges (stale / aging / excluded counts)**.
- All column headers sortable (numeric, per sibling group).
- Root page additionally shows a collapsed **Runs & captures** disclosure (summary table;
  the full experience lives at `#/runs`).

### File page

- Code card in the UUI code-snippet idiom: header = file icon + name + language badge
  (**no copy button**); sticky column-header row.
- Per-line grid: **[reserved ticket gutter, collapsed] · line # · one hit-count column per
  tier (tier-dot headers) · branch pills · source · runs expander**.
- Row coloring = today's winner-take-all precedence, verbatim: excluded > highest-precedence
  tier with a hit > aging > stale > uncovered; lines with no `LineRecord` are uncoverable
  (muted, never red). Tint + colored left-edge accent.
- Branch pills: taken / not-taken / unreachable (struck through), tooltips naming
  block/branch and per-tier hits.
- **Per-line contexts**: right-gutter `▸ N` expands an inline panel of run chips — tier dot +
  run label + **host id pill** + hit count; revoked credits render struck-through
  ("revoked"), aging runs annotated. "Expand contexts" in the card header opens all.
- Syntax highlighting: Shiki, bundled, JS-regex engine, grammar set limited to the
  languages otto targets (C/C++ first; configurable later if needed).

### Runs & contexts page (`#/runs`)

- **One row per run** — multi-host is the norm: hosts render as pills on the row, and the
  expanded detail contains a **Per-host lines** breakdown (host, line count, bar).
- Row: run label, tier chip, host pills, board, labs, date, lines-contributed bar, status
  badge (OK / aging / stale) + remap marker (`✎ remapped`).
- Filters: tier chips + free-text search over label/host/ticket/board.
- Expanded detail: capture metadata (hosts, board, labs, captured date, tester, ticket,
  base commit incl. `→ HEAD (remapped)`, note), per-host lines, contribution by type
  (line/branch/decision; stale runs show "N credits revoked — anchor unverifiable"),
  top files (links to file pages), and **Focus this context**.

### Context focus (report-wide filter)

- Pinning a focus (from a run's detail or the ⋮ Select) sets a **global** filter: every
  stat card, tree % column, bar, and file-page tint recomputes to that run's coverage
  alone; everything else renders uncovered/neutral.
- Visible everywhere via the app-bar chip; cleared via chip ✕ or "All contexts".
- State: encoded in the route query (`?ctx=`), persisted per report in `localStorage`
  (key namespaced by report stamp — reports on a shared CI origin must not fight).
- This is the same machinery a future per-ticket filter will reuse (filter by
  commit-derived ticket instead of run label).

## 5. Component sourcing policy

Precedence order, spec-level rule:

1. **Vendored Untitled UI component** when one exists (Table, Badge, Button, Dropdown,
   Select, Tabs, Tooltip, Input…). Vendored tree stays byte-exact — never hand-edited.
2. **Existing hand-built `ui/**` component** — reuse what we already built for the monitor:
   `Breadcrumbs`, `Disclosure`, `theme.ts`, toast/command patterns. Same source tree,
   direct imports. **Functional and aesthetic parity across all otto web interfaces is a
   requirement, not a preference.**
3. **New `ui/**` component** only when neither covers it. This rework adds exactly two:
   `TreeView` (react-aria Tree + stat columns) and `CodeView` (Shiki-highlighted,
   coverage-decorated source grid). Both PRO-tier UUI components they resemble were
   evaluated and not purchased; these are otto-owned, react-aria-based, token-styled.

Dark mode: the monitor's mechanism exactly — one `.dark-mode` class on `<html>`, seeded
from OS preference, persisted in `localStorage["otto-theme"]` (same key across otto tools —
the user's preference follows them), applied pre-paint. No `dark:` variants in authored
code; semantic tokens only.

## 6. Data contract

`store.json` remains the canonical, versioned store (bump `STORE_FORMAT_VERSION` 3 → 4):

- `RunRecord` gains an explicit **`host`** identity (display name / host id) — context
  identity in the UI is (run label, host). Runs sharing a label group into one context
  with multiple hosts.
- Report-level config block: tier order/colors (existing), **thresholds** (high/medium),
  and a type-extensible stats vocabulary (`line`, `branch`, `decision`, …).
- Per-line ticket slot reserved (absent until the per-commit plumbing exists).

For the SPA, the renderer emits **classic-script data chunks** (`window.__OTTO_COV__`
pattern — modules and `fetch` don't work on `file://`):

- `cov_data/index.js` — full tree with **per-directory rollups precomputed in Python**,
  runs table, config. Loaded at boot.
- `cov_data/files/<mangled-path>.js` — source text + per-line hits/branches/contexts for
  one file. Loaded on navigation. Constant page-load cost regardless of repo size.
- Every chunk embeds the report stamp; a stamp mismatch (report overwritten under an open
  tab) or an old store version renders a friendly "regenerate this report" screen — never
  a blank page.

The empty-report contract is unchanged: `otto cov report` with nothing to report exits 1
naming every searched location; an emitted report always renders content.

## 7. Generation pipeline

`CoverageReporter.run` keeps its collection model (merge `.gcda` → captures → unit harvest
→ manual folds → validity) and swaps the render step: `HtmlRenderer` (Jinja) is replaced by
a `SpaRenderer` that copies the prebuilt bundle and emits `store.json` + data chunks.
CLI: `--report` renamed to `--dir` (existing TODO). `--prefix` display-strip behavior and
default output dir handling are preserved.

## 8. Validity policy (rulings)

1. **Whitespace changes never revoke** manual credits. **Line-ending-only changes never
   revoke.** Encoding-only changes (BOM addition, transcoding sweeps) are NOT exempt:
   git diff has no encoding-insensitive mode, so affected lines revoke and must be
   re-proven — a documented limitation, not a bug. Any semantic change revokes the
   changed lines.
2. **Renames**: followed exactly as far as git tracks them (`git diff -M`). No custom
   rename inference. Splits/copies → re-prove.
3. **Revert-resurrection is a feature**: a file edited (credits stale) then reverted to a
   matching blob regains its credits via the blob fast-path. Pinned by test so it is never
   "fixed" away.
4. Anchor chain failures degrade loudly to stale — never a crash — including: base commit
   GC'd (post-squash-merge), shallow clones (base absent; degradation message suggests
   deepening), submodule/nested-repo ambiguity.
5. **Same-context re-capture supersedes**: a newer capture with the same (run label, host)
   replaces the older one's credits entirely — accumulation would double-count the same
   context. The superseded capture is dropped from the runs table.
6. Merge-conflict resolutions: deferred (non-goal §1).

Implementation of (1)+(2): the per-capture tree diff runs
`git diff -M -w --ignore-cr-at-eol --name-status base..HEAD` (the flags *are* the policy),
plus a whitespace-normalized content hash as a fast path that skips diffing entirely when
only whitespace moved.

## 9. Performance & caching

The dominant cost is git subprocess fan-out, not computation (remaps are in-memory over
`-U0` hunks; history length is irrelevant to tree-to-tree diffs). In order of leverage:

1. **Batch git ops — on both anchor paths**: one tree-wide `diff -M -w` replaces per-file
   diffs while the base commit resolves; when it does not (squash-merged away, shallow
   clone), the blob fallback batches too — one `hash-object --stdin-paths`, one
   `cat-file --batch-check`, one `cat-file --batch`, and one dir-level
   `diff --no-index -w -U0` over two temp trees for the changed pairs. Target (achieved):
   O(captures) git processes, not O(files × captures) — ≤6 spawns per capture either way.
2. **Validity cache: descoped (checkpoint ruling 2026-07-24).** This section originally
   committed to a content-addressed cache under `.otto/coverage/cache/` keyed by
   (capture blob SHA, current blob SHA). The Task 9/10 checkpoint profile killed it:
   batching the fallback collapses 2 602 spawns / 1.53 s (SSD, 2 000 files, 10% churn)
   to 5 flat spawns / 49 ms, while the cache as specified could not touch the O(N)
   hash spawns that dominated (a content-addressed key requires observing current
   content regardless) and trimmed only ~15% of spawns warm. Post-batching, its residual
   value is ≤3 spawns (~60 ms at a pessimistic 10 ms-per-spawn NFS projection) — below
   the carrying cost of a persistent store's pruning/corruption/atomicity surface. The
   key design stays valid if a future profile finds a real repeat-fold hotspot.
3. Emission is linear in repo size and unchunked pages are constant — no caching needed on
   the render side.

**NFS is a first-class constraint** and spawn/round-trip counts are the metric that
transfers from a local-SSD dev box to network filesystems (each git spawn re-touches
`.git` metadata over the wire) — which is why the fallback fix is batching, not caching.
Should the cache ever be revived, the original backend ruling stands: JSON whole-file
read + atomic-rename write; sqlite's POSIX-lock/WAL dependence disqualifies it on NFS.

Reports stay **single-process**.

## 10. Testing strategy

### RepoTimeline harness (new)

A tmp_path-only fixture builder scripting `(mutation, expected per-line disposition)`
timelines: `commit → capture → mutate → report → assert {line: valid|stale|shifted(+n)|…}`.
Table-driven rows covering, at minimum:

- shift up/down; edit-inside-region; **formatter sweep and EOL-only flips (must NOT
  revoke — pins ruling §8.1); encoding-only flips (BOM addition, transcoding sweeps)
  DO revoke — pins §8.1's documented limitation**
- `git mv` clean and rename+edit (follows `-M`); file deleted (graceful vanish)
- **squash-merge survival via blob anchor** (capture on branch → squash → gc → report);
  rebase; base GC'd + blob changed (stale, no crash); **shallow clone degradation**;
  nested repo/submodule
- **revert-resurrection**; aging boundary (frozen clock, UTC/DST); future-dated capture;
  `max_age` tightened post-capture; aging line re-covered by a fresh run
- overlapping captures on one line (no double-count; per-run traceability); same-label
  re-capture (supersede/accumulate policy pinned); wrong-repo capture errors loudly
- non-UTF-8 source renders; coverage past EOF after shrink; moved `LCOV_EXCL` markers
- one **golden mixed-history timeline** (months of history, several captures, a dozen
  mutation kinds) asserting aggregate sanity

**Deferred (not delivered by this plan):** rebase; nested repo/submodule; `max_age`
tightened post-capture; aging line re-covered by a fresh run; moved `LCOV_EXCL` markers;
the encoding-only-flips-revoke pin for ruling §8.1's documented limitation. These remain
listed above as the target table-driven coverage, not as claims of what shipped — tracked
as a follow-up issue.

### Scale & performance benchmark

Synthetic repo (thousands of files, several captures): wall-clock budget and
**git-subprocess count assertions** on both anchor paths (deterministic; catch
per-file-spawn regressions on any machine — spawn counts are the NFS proxy, §9). The
JSON-vs-sqlite `CacheStore` comparison died with the cache descope (§9 ruling).

### Frontend

- vitest for `covapp` + new `ui/` components (thresholds bucketing, tree rollup rendering,
  focus state, precedence coloring) — under the existing console-guard and coverage gates.
- Browser e2e (`tests/e2e/cov/report_browser/`) migrates to the SPA: `file://` lane
  (today's contract) **plus a served lane with the documented minimal CSP** (§2 gate),
  both themes, sort/drill/focus/context interactions against the real built bundle.
- The shared report fixture gains multi-host runs, a stale run, and an aging+remapped run
  so every UI state is exercised; docs screenshots regenerate from it.
- Existing gates inherited by living in `web/`: biome/knip/tsc, air-gap check, brand-token
  check, TS coverage fold. New: CSP boot gate; bundle-size ceiling (Shiki grammars curated).

## 11. Migration

- Delete: Jinja templates, `report.css`, `covreport/` sorter + its vite config. Migrate its
  browser tests and unit pins to SPA equivalents (row precedence, legend, run table,
  prefix display, out-of-range tolerance — behavior preserved, assertions relocated).
- `make web` builds `covapp`; `make web-clean`/`wheel-check`/CI lanes updated.
- Settings additions: `[coverage.report] high`, `medium`. Store v4. CLI `--report`→`--dir`.
- Docs: guide/cli-reference/architecture pages updated; docs media regenerated.

## 12. Open items (future work queue)

1. **Per-ticket coverage report** (per-commit ticket attribution, blame-based; will need
   its own git batching/caching). Beyond the per-ticket focus filter, the report must show
   not only the ticket's covered lines but a **direct listing of its MISSING lines** —
   grouped by file as line ranges, each linking to the file page scrolled to and
   highlighting that range — so developers can comb out the last uncovered lines without
   hunting through the tree. (A general "missing lines" listing view may fall out of the
   same machinery; design it so the ticket filter is one instance of it.)
2. Decision coverage producer (gcno+DWARF R&D feeds the reserved schema slot).
3. Merge-conflict resolution validity.
4. Report-side keyboard navigation (menu item exists; bindings TBD).
5. `--report-name` subtitle (existing TODO), per-run supersede UX refinements.
