# Feedback on the top two TODO refactoring items

**Date:** 2026-08-02
**Scope:** the first two bullets under `## General` in [TODO.md](TODO.md) — (1) code
sprawl/churn and the `@cli_exposed` question, (2) code-browsing groundwork. This is
feedback and measurement only; no refactoring was performed. Effort scale used below:
**S** ≤ 1 day, **M** 2–3 days, **L** ≥ 1 week.

---

## Item 1 — churn, aging, and the `@cli_exposed` hypothesis

### 1.1 The cited evidence, measured

The command-timeout change (`c99d63e2`, 45 files, +3751/−275) decomposes as:

| Bucket | Lines (+/−) | Share |
|---|---|---|
| Plan + design docs (`docs/superpowers/`) | 2228 | 59% |
| Tests | 1072 | 29% |
| Library src (`src/otto/`, non-CLI) | 723 | 19% of total, ~48% of non-doc |
| User-facing docs | 22 | — |
| **CLI glue (`src/otto/cli/`)** | **2** | **~0.05%** |

Three observations that reframe the "hundreds of lines" feeling:

1. **59% of the diff is process artifact, not code.** The SDD plan (1583 lines) and
   design doc (645 lines) ride in the feature commit. They're valuable, but they make
   every `git show` and churn metric read ~2.5× worse than the change actually was.
2. **The test bucket was almost entirely *new* coverage, not repair.** Only −54 lines
   were deleted across all 16 test files. `test_run_timeout.py` +390 and
   `test_unix_host.py` +161 are new behavior coverage; the +62 in
   `test_dynamic_host_commands.py` are deliberate new tests *of the synthesizer*
   (help text, bounds, `--timeout inf`), not fixtures asserting the old default.
3. **Two real bugs (SSH `exec` never enforced its timeout; `LocalHost.exec` bounded
   per-readline) and nine retired `asyncio.wait_for` workarounds in `tunnel/`/`link/`**
   account for much of the library spread. That part is the system getting *simpler*,
   bundled into the same diff.

So the honest reading: the change was genuinely wide, but the width came from the
semantics being wide (every host class, every exec path), plus bug fixes and cleanup —
not from mechanical mirror-updating. The one true mirror cost was 22 lines of prose docs.

### 1.2 The `@cli_exposed` question: the evidence says keep it

This commit is close to a controlled experiment for exactly the hypothesis in the TODO,
and it lands against it:

- The **only** CLI-layer change was one line in `src/otto/cli/param_synth.py:223`, and
  it was a *capability addition* (teaching the synthesizer numeric `min=` bounds), not
  a mirror update. The plan itself notes `otto host <id> run --help` showed
  `[default: 30.0]` **for free** the moment the library default changed.
- The decorator (`src/otto/utils.py:171-198`) is pure marker-setting; option names,
  types, defaults, help, and the verb menu are all *derived* from the Python signature
  at synthesis time (`src/otto/cli/expose.py`, `param_synth.py`). There is no cached
  or hand-written CLI surface to go stale — completion synthesizes verbs live, and the
  docs termynal captures shell out to the real `--help`.
- Reverting `otto host` to hand-registered commands would create **40 hand-written verb
  commands** (40 decoration sites across 6 host modules), each a new mirror of a library
  signature. The `30.0` default would gain a fourth home. The diff for this same change
  would have been larger, not smaller.
- On "command registration changed soon after": the registry unification didn't obsolete
  `@cli_exposed` — they compose. `host` is a normal `CommandSpec` registry entry
  (`src/otto/cli/builtin_commands.py:48-50`) whose loader happens to be the
  self-synthesizing group; `registry.py` even reuses `expose._synthesize_command`'s
  throwaway-Typer technique. `@cli_exposed` sits one level below the registry, not
  beside it.

**However, two costs of the mechanism are real, and both are fixable without
decoupling:**

1. **Type-erasure (the big one).** `cli_exposed` is typed
   `Callable[..., Any] -> Callable[..., Any]` (`utils.py:172-178`), so every decorated
   method loses its signature from `ty`'s view — the timeout plan measured that a
   decorated `run` silently accepts `timeout="banana"` and even `bogus_kwarg`. Callers
   had to be found by grep during the refactor. This is precisely the "changes are
   harder than they should be" tax, and it will tax every future host refactor.
   **Fix: make the decorator signature-preserving (`ParamSpec`/`TypeVar` identity).
   Effort: S** (the decorator change is minutes; budget the rest of the day for the
   ty sweep it will newly enable, which may surface real latent call-site errors —
   that's the point).
2. **Hand-written docs mirrors with no gate.** `docs/guide/cli-reference.md:222-260`
   (a 20-verb table plus a `run` options table with a literal `Default: 30.0` column)
   and `docs/guide/hosts/capabilities.md:~353` (quotes the `run` signature verbatim as
   the canonical teaching example). Sphinx `-W` cannot catch any of them going stale.
   **Fix options:** (a) generate the tables at docs-build time from the synthesizer /
   live `--help` (the `capture_docs_termynal.py` pattern already exists) — **M**;
   (b) a cheaper check-only gate that diffs the prose tables' verb list and defaults
   against live `--help` output and fails the docs build — **S**. I'd do (b) now,
   (a) opportunistically.

### 1.3 Where the amplification actually lives

- **Repeated literal defaults across layers.** `DEFAULT_COMMAND_TIMEOUT` appears as a
  signature default at ~20 sites across `host.py`, `session.py`, `interact.py`,
  `app_shell.py`, `embedded_host.py`, `unix_host.py`, each with a docstring naming it.
  The constant lives in one place (good), but the *pattern* means any change to default
  **semantics** (e.g. making it config-driven) touches every signature again. This was
  a deliberate design choice in the 2026-07-29 design doc, and it has genuine upsides
  (self-documenting signatures, Sphinx `:data:` links). I would **not** rework it now —
  it just landed — but if the *next* default-semantics change fans out the same way,
  that's the signal to move to a resolve-at-one-choke-point pattern. Effort if/when: M.
- **Wrapper layers re-declaring parameters.** `src/otto/context.py:184,315` and
  `src/otto/config/fleet.py:270,293` re-declare `timeout` in their own signatures, and
  `context.py:180-186` needed a deferred import to dodge a `context → host` circular
  import. Library-to-library signature duplication, nothing to do with the CLI. A
  small audit of which wrappers restate parameters vs. pass through would tell you how
  much of this exists. Effort for the audit: S.
- **The host subsystem is the churn center, and it's big.** Since 2026-05-01:
  `host/session.py` 50 commits (1919 lines), `unix_host.py` 43 (941), `host.py` 36
  (1242), `embedded_host.py` 35, `docker_host.py` 30, `local_host.py` 28. The
  subsystem totals ~12.3k lines of the repo's ~52k. `session.py` at 50 commits × 1919
  lines is the single strongest decomposition candidate in the codebase — top churn ×
  top size is exactly where refactoring investment pays. Effort: L (needs its own
  design pass; it owns shell lifecycle, exec, expect, and liveness concerns that
  intertwine).
- **Overall shape is healthier than it feels.** Median commit touches 2 source files;
  mean 8, p90 20. Test churn runs 1.73× src churn — high, but consistent with the
  test-heavy discipline here, and the timeout commit shows much of it is new coverage,
  not repair. The fat tail (p90+) is dominated by deliberate cutover commits
  (renames, the lab.json cutover, registry unification), which is what "changing
  architecture before we have real users" is supposed to look like.
- **Subsystem co-change:** `cli + host` co-change in 48 commits, `cli + configmodule`
  41, `cli + suite` 39, `cli + models` 36 — the CLI co-changes with everything, which
  is expected for a CLI product (features land surface-first), not necessarily
  pathological. `configmodule + host` at 32 is the pair I'd watch.
- **One concrete "reinvented rather than reused" instance already exists** — see §2.4:
  the web tree has two app shells, two `/`-key handlers, and two search
  implementations with no shared component. Item 2's groundwork forcibly pays this
  down, which is a point in favor of doing it soon.

### 1.4 Candidate technical fixes, with effort

| # | Fix | Effort | Payoff |
|---|---|---|---|
| 1 | Signature-preserving `cli_exposed` (`ParamSpec`) + the ty sweep it enables | S | Restores typechecker leverage for all future host refactors; highest value-per-line in this list |
| 2 | Docs-tables staleness gate (diff prose tables vs live `--help`) | S | Kills the last real mirror site |
| 3 | Wrapper-parameter audit (`context.py`, `fleet.py`, others) | S | Sizes the library-to-library duplication before deciding anything |
| 4 | Generated CLI reference tables | M | Supersedes #2 properly |
| 5 | `session.py` decomposition (own design pass first) | L | Attacks the top churn×size hotspot |
| 6 | Choke-point default resolution for command timeouts | M | **Defer** — wait for a second fan-out of the same shape before paying this |
| 7 | Standing churn dashboard (the analysis above, scripted; e.g. `scripts/churn_report.py`) | S | Makes "where is it aging" a measurement, not a feeling, for every future round |

### 1.5 Policy candidates (AGENTS.md / CONTRIBUTING.md, future round)

- **Separate process artifacts from feature diffs.** Commit SDD plans/design docs in
  their own commit (or count `docs/superpowers/` separately in any churn review).
  The timeout change *felt* 2.5× its real size because of this.
- **One value, one home.** A semantic constant/default may be defined once; signatures
  may reference it; prose may link it (`:data:`) but never restate the value. The
  `Default: 30.0` docs column is the violation class to name.
- **Decorators must be signature-preserving.** A `Callable[..., Any]` decorator on a
  public method silently disables the typechecker for every caller. This one already
  bit; make it a rule.
- **Wrappers pass through, they don't re-declare.** If a layer restates a lower
  layer's parameter defaults, it becomes a mirror site.
- **Diff-size expectations by change class.** "Change a default" should be a
  1-file change + tests; when it isn't, the PR description should say which of
  (wide semantics | bugs found | cleanup bundled) explains the spread — that's the
  graceful-aging tripwire, cheap to enforce in review.

---

## Item 2 — code-browsing groundwork

### 2.1 The premise, checked

"Otto is fairly well set up for this" is **half right, and the right half is the
backend.**

Genuinely strong and browser-ready:

- **The git layer is the best asset.** `src/otto/coverage/capture/gitio.py` already has
  batch `cat_blob`/`cat_blobs` (`gitio.py:259,267`) — a ready-made "read any file at
  any rev" primitive — plus first-parent walks, `-U0` diff streamers, and rename
  tracking. `src/otto/coverage/attribution.py` is a from-scratch blame:
  `attribute_lines` (`attribution.py:239-306`) returns `{path: {line: owning-sha}}`.
  `remap.py`/`anchor.py` map lines across revisions. A code browser's history features
  are mostly *already written*.
- **The rendering seam is already clean.** `web/src/ui/CodeView.tsx` is genuinely
  presentation-only (its `CodeLine` contract takes pre-rendered HTML and opaque gutter
  cells; a test pins it as generic), `web/src/covapp/highlight.ts` is a pure
  `(source, lang) → html[]` layer, and `web/src/covapp/data.ts` is a clean fetch layer
  with a test seam. The decomposition the TODO asks for is *partially done*.

Overstated or missing:

- **"It already has syntax highlighting" — only for C/C++.** `highlight.ts:23-29`
  maps `c/h/cpp/…` and sends everything else to plain text. Adding Shiki grammars is
  cheap, but it's not "already well formed" for a general browser.
- **The highlighter throws away the structure a browser needs.** `highlightLines`
  calls `codeToHtml` then string-splits on `<span class="line">` (`highlight.ts:86-110`),
  and CodeView injects the result as opaque `dangerouslySetInnerHTML`
  (`CodeView.tsx:230`). There is **no token-level DOM** — and click-on-symbol, hover,
  call-stack highlighting, and scenario recording (the whole walkthrough direction)
  all need positioned tokens. This is the single most important gap.
- **No symbol layer exists at all.** Zero symbol/identifier extraction anywhere
  (checked: no tree-sitter/ctags/pygments/monaco/codemirror; Python `ast` is used only
  for completion caching and `init` doctor checks). All existing history knowledge is
  *line*-level, not *symbol*-level. Blame gets you a history gutter, not
  go-to-definition. The `/`-search-for-symbols idea has no index to search yet.

### 2.2 Decide this first: the delivery-mode fork

The coverage report is a **static, `file://`-compatible, CSP-locked artifact**: no
fetch, no ES modules, no WASM, 2 MB chunk ceiling, data injected as classic-script
`cov_data/*.js` chunks (all enforced by e2e lanes — `tests/e2e/cov/report_browser/`,
`tests/_fixtures/_csp_server.py:17-19`). The monitor is the only live server and never
touches coverage. **No backend serves source code today** — source text is baked into
per-file chunks at report-generation time (`src/otto/coverage/renderer/spa_data.py:500-535`).

A code browser must pick a lane, and the choice changes what "decompose for sharing"
means:

- **(a) Stay inside the static constraint.** Symbol index and all file chunks emitted
  at generation time. Maximally shareable (same `file://`/Jenkins story coverage has),
  but the browser wants *all* repo files, not just covered ones, and search must ship
  precomputed.
- **(b) A live server lane** (like monitor). On-demand `cat_blob` at any rev, room for
  a real LSP later. Richer, but forfeits the artifact story for this surface.
- **(c) Both, behind a provider seam.** Define a `SourceProvider` interface
  (path → source/lang/metadata) that the static chunk loader implements today and a
  server can implement later. `data.ts` and `CodeView` are already shaped for this.

**Recommendation: (c), decided via a short spec before any decomposition work** —
effort S for the spec. Doing groundwork without this decision risks decomposing along
the wrong seam.

### 2.3 The actual groundwork, with effort (in order)

1. **Token-preserving highlight refactor** — switch `highlight.ts` from
   `codeToHtml` + string-split to `codeToTokens`, keep per-line HTML as a *rendering*
   of the tokens, and let CodeView optionally take tokens instead of an HTML blob.
   Pure refactor, no visible change, unblocks every walkthrough feature. **S–M.**
2. **Split `FilePage.tsx` (761 lines) along its natural seams.** It is currently
   route + chunk loader + highlight driver + annotation compiler + stats card + shell
   wrapper in one file. Extract: a `useSourceFile(path)` hook (the load/highlight
   state machine at `FilePage.tsx:521-550`), the source→`CodeLine[]` compiler
   (`FilePage.tsx:596-621`), and turn gutters into composable providers — the ticket
   gutter (`buildTicketGutter:347`) is structurally identical to a future blame/symbol
   gutter. Result: the browser becomes a new *consumer* of shared machinery instead of
   a fork of FilePage. **M.**
3. **Unify the `/`-search and shortcut infrastructure across the two shells.** Today
   there are two app bars with no shared component (`web/src/shell/AppBar.tsx` vs
   `web/src/covapp/chrome/AppShell.tsx`), two `/` handlers (`useGlobalShortcuts.ts`
   monitor-only; `TicketSearch.tsx:47-61` covapp-local), a single-slot global
   search-focus registry (`searchFocus.ts` — needs multi-target), and a command
   palette whose registry depends on the monitor's zustand store (`commands.ts:25`) —
   covapp deliberately carries no zustand. The `/` symbol box you want *in both apps*
   forces this unification; it's also the concrete instance of item 1's "reinvented
   rather than reused." **M–L.**
4. **Emit `per_line_sha` into `cov_data`.** It's already computed in
   `reporter.py:764` and thrown away. Emitting it is the cheapest possible bridge to
   history-aware display, and it gives *coverage reports* a blame gutter option before
   any browser exists. **S** (emit) — the gutter UI itself is a feature, not
   groundwork; hold it if you want to stay strictly non-feature.
5. **Re-open the sanctioned-component constraint deliberately.** The coverage-SPA plan
   pinned CodeView as "the LAST sanctioned new `ui/**` component"
   (`docs/superpowers/plans/2026-07-25-coverage-spa.md:35-37`, echoed in
   `CodeView.tsx:2`). Groundwork that adds shared `ui/` pieces needs that constraint
   formally amended, or the next session will fight it. **S** (a docs edit + the
   decision).
6. **Defer symbol extraction entirely** until the §2.2 fork is decided. When it
   comes: language ceiling says start with Python `ast` (stdlib, no WASM-CSP fight)
   emitting a `symbols.js` chunk. **L**, later.

One scope note on the search box: for coverage reports *alone*, the near-free win is
file-path/ticket/context search over data already in `IndexPayload` — no symbol index
required. Worth being precise about whether the coverage-side `/` box needs *symbols*
at all, or whether symbols are purely the browser's requirement; the answer moves
~all of item 6's cost off the shared path.

### 2.4 Cross-cutting: item 2 is a lever for item 1

The two-shells/two-shortcut-systems duplication is the clearest live example of the
aging pattern item 1 describes (a mechanism existed; a second one got built). The
code-browsing groundwork cannot proceed without resolving it — which makes item 2's
prep work double as item 1's debt paydown. Sequencing both TODO items through the
shared-shell/search unification first gets you paid twice.
