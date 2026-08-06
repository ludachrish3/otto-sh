# Docs alignment after the churn-review wave, and one page that names every quality gate

**Date:** 2026-08-05
**Status:** designed
**Context:** the 44 commits between `56ffc7ab` and `77af429d` implemented Tier 0
in full plus four Tier 1 items of `todo/churn-and-design-review-2026-08-03.md`.
They updated 22 doc files. This spec closes the drift they left in
`docs/architecture/**`, adds the quality-tooling page Chris asked for, and
finishes the plan-coordinate policy (review §5.4) in the tree it never covered.

## Decision

Three parts, one wave, one squash.

**A. Align five architecture pages** with what the wave actually shipped. Scope
is the wave, not a full audit: `docs/` holds 171 non-spec pages and most have
not changed since they were written, so a full sweep would bury the real drift
in unrelated churn.

**B. Add `docs/architecture/quality-gates.md`** — a Python | TypeScript table of
what tool performs each kind of check, and, for each row, what *binds* it.

**C. Finish review §5.4** ("no plan coordinates in shipped source"). The gate
landed claiming a zero baseline and holds neither half of it: its pattern misses
8 live sites in `src/otto/**`, and it never covered `web/src/**` at all, where
~158 comment blocks cite plans that commit `158a6e81` deleted. Widen the
pattern, gate both languages, fix both trees.

## Part A — architecture pages that drifted

Each item below was verified against the current tree, not inferred from commit
messages.

### A1. `architecture/lifecycle.md` — the interrupt policy is absent

The page owns "the command lifecycle" and does not contain the strings `SIGINT`,
`SIGTERM`, `interrupt`, `deadline`, or `sync_phase`. Three waves of lifecycle
work landed in this window (`f8ab9821`, `62089db0`, `d333087f`) and the
`2026-08-03-command-lifecycle-uniformity-design.md` spec is marked implemented.

Add to the existing walk-through, in the page's own voice:

- the two-stage interrupt policy on the `run_command` path — first signal is
  graceful, second signal or `OTTO_TEARDOWN_DEADLINE` expiry force-exits
  `128+signum`;
- `lifecycle.sync_phase` as the synchronous sibling for a phase owning its own
  event loop, and that `otto test`'s pytest session is the one caller — it
  *composes* the primitive rather than escaping the policy;
- that registration is the opt-in: a plain `async def` leaf reaches the policy
  through the leaf-invoke bridge, so third-party commands get it for free;
- one correction of fact — the diagram's teardown node already reads "exit code
  derived from the Result". That only became true in `607a948b`. Leave the
  wording, add the sentence that says which seam makes it true, so the next
  reader can check it.

`@async_typer_command` is not mentioned on this page, so its deletion needs no
edit here.

### A2. `subsystems/registries.md` — `INSTRUCTIONS` moved

Line 128 attributes the `INSTRUCTIONS` registry to `otto.cli.run`. Tier 1.5
(`aa4c454a`) moved it to `otto.instructions` to kill two core→cli backward
imports. Line 26's `{func}~otto.cli.run.instruction` is the *decorator* and
stays correct — only the module-list line changes, plus a clause naming why the
registry and its decorator live apart.

### A3. `subsystems/extension-points.md` — five shipped changes, four BREAKING

None of these appears anywhere on the page:

- `Product.stage/install/uninstall` return `Result`, not `tuple[Status, str]`
  (`ee688b05`, BREAKING for third-party products);
- an `@instruction` must be `async def`, rejected loudly at registration
  (`c579d502`, BREAKING);
- a lab-bound `@cli_command` must be `async def` too, rejected before
  registration (`da268979`, BREAKING);
- a registered command's returned `Result` now derives the process exit code
  (`607a948b`, BREAKING — it used to be silently discarded);
- `SupportsHostSummaries` (`7b2658cf`) — a new *optional* capability, not
  breaking, that a third-party lab backend implements to make completion fast.
  It belongs in the page's extension-point table.

`docs/guide/**` already carries all five — the wave updated
`guide/hosts/capabilities.md`, `guide/extending-cli.md`, `guide/run/index.md`
and `guide/setup/host-database.md`. This is the architecture-level index of
extension points being stale, not the guide.

### A4. `subsystems/bootstrap.md` — `discover()`'s shape

`discover()` returns `(env, repos, errors)` and `invalidate()` is the documented
public recovery path (`5475ff27`, Tier 0.6). The page describes the two phases
and mentions neither. Add the third element and one sentence on why errors ride
the cached tuple rather than a module global.

> **Superseded 2026-08-05 by this wave's own `0804904e`**
> (`refactor(bootstrap)!: discover() returns a DiscoveryResult, not a
> 3-tuple`). As approved above, `discover()` returned the bare 3-tuple
> `(env, repos, errors)`. It now returns a frozen `DiscoveryResult` dataclass
> with `env` / `repos` / `errors` fields, while `bootstrap()` returns the
> separate `BootstrapResult` that adds `warnings` from the dependency pass.
> The A4 requirement is unchanged in substance — document all three elements
> and why errors ride the cached result rather than a module global — only the
> shape it names has moved. The approved text above is left as written; this
> note is the correction.

### A5. `subsystems/monitoring.md` — two structural facts

- `collector.spawn_collection()` is the blessed open-then-spawn seam and `run()`
  now *refuses* an unopened DB rather than opening lazily in-task (`15299154`,
  Tier 0.7). This is architecture, not trivia: it is the fix for a five-issue
  flake wave, and the page is where a reader would look for the ordering rule.
- Broadcaster subscriber queues are bounded (`SUBSCRIBER_QUEUE_MAX`,
  drop-oldest) and `MetricStore` series are capped (`_SERIES_POINTS_MAX`), with
  the reconnect resync being what makes dropping safe (`d663fcd6`, Tier 0.2).

### Explicitly not in Part A

`architecture/testing.md` — it is about test tiers and markers, is accurate, and
the static-analysis gates it never mentions are what Part B exists to hold. No
edit.

## Part B — `docs/architecture/quality-gates.md`

### Shape

A table whose rows are kinds of check and whose two columns are Python and
TypeScript, followed by a "what binds each gate" section and a short statement
of the gated-vs-prose principle.

Every cell states a tool. Two cells are honestly **empty** — TypeScript has no
module-layering or scoped-pattern-rule gate today. Filling them with something
adjacent (knip is not tach; Biome rules are not ast-grep) would be exactly the
prose-that-reads-true-and-is-not this repo keeps finding.

| Kind of check | Python | TypeScript |
| --- | --- | --- |
| Lint rules | `ruff check` | `biome check --error-on-warnings` |
| Formatting | `ruff format --check` | Biome — same command (rules + format + **assists**) |
| Type checking | `ty` (pinned `==0.0.64`) | `tsc --noEmit` via `scripts/typecheck_web.sh` (vendored Untitled UI diagnostics filtered) |
| Unused code / deps | ruff (`F401`, `ARG`, …) | `knip` — unused files, exports, dependencies |
| Module layering | `tach` against `tach.toml` (ratchet baseline; `tach sync` is forbidden as a fix) | — none today |
| Scoped pattern rules | `ast-grep` against `.ast-grep/rules/` | — none today |
| Import cost | `scripts/import_budget.py` — module-count caps, snapshots, denylist | — (knip covers dependencies only) |
| Tests | `pytest` (+ `xdist`, `repeat`, `hypothesis`) | `vitest` |
| Coverage floor | `coverage.py` / `pytest-cov` | `@vitest/coverage-v8` + `nyc`, browser leg merged via `monocart-coverage-reports` |
| Browser e2e | `pytest-playwright` — drives **both** lanes, Chromium and WebKit | (same lane) |
| Cross-language contract | `tests/_fixtures/covapp_contract.json`, asserted from both sides; `types.gen.ts` codegen + `git diff --exit-code` | (same two mechanisms) |
| Docs | `sphinx -W` (clean rebuild), `doc8`, Sphinx doctest, `scripts/lint_markdown_doctests.py`, `--doctest-modules` over `src/otto` | — |
| Built-bundle gates | — | `check_airgap.sh`, `check_brand_tokens.sh`, `check_untitledui_hash.sh`; `build_web_no_warnings.sh` (warnings-as-errors) |

### The "bound by" section

A table listing each gate's Makefile target, nox session, and CI job. This is
the half that keeps the page from decaying into a tool list, and it records one
thing worth knowing: **not every gate runs in CI.**

| Gate | Make | nox | CI job |
| --- | --- | --- | --- |
| ruff (lint + format) | `lint-python` | `lint` | `lint-python` |
| tach + ast-grep | `lint-arch` | `lint` | `lint-python` |
| ty | `typecheck-python` | `typecheck` | `typecheck-python` |
| Biome + knip | `lint-ts` | — | `check-ts` |
| tsc | `typecheck-ts` | — | `check-ts` |
| vendored-hash | `check-ts` | — | `check-ts` |
| vitest + unit coverage floor | `coverage-ts-unit` | — | `check-ts` |
| pytest (hostless matrix) | `coverage-hostless` | `tests_hostless` | `tests` (3.10–3.14) |
| test-isolation leak guard | — | `tests_unit_repeat` | `unit-repeat` |
| import budget | `profile` (adds hyperfine) | via `tests_hostless` | `tests` — enforced by `tests/unit/import_budget/` |
| air-gap + brand tokens + type drift | `web` | — | `dashboard`, `docs` (both run `make web`) |
| browser e2e | `dashboard` | `dashboard` | `dashboard` (Chromium, WebKit) |
| docs | `docs` | `docs` | `docs` |
| vendored-UI upstream drift | — | — | `untitledui-drift.yml` (own workflow) |
| Python coverage floor | `coverage` | — | **not in CI** — local and release only |
| chaos / stability lanes | `chaos`, `stability*` | — | **not in CI** — opt-in; nightly runs chaos tier 2 |

The last two rows are the point of the table. `make coverage` is the per-task
gate and CI never runs it; the chaos and stability lanes are bed-hostile and
excluded from every default gate. A contributor reading only the CI config would
conclude otherwise.

### Closing section

Four sentences on why the page exists, citing the review's §3/P5 finding: gated
lines held for five weeks with zero raises while prose rules decayed in the same
window. A gate belongs on this page; a convention that is only written down does
not, and that asymmetry is the page's editorial rule.

### Wiring

- New `quality-gates` entry in `docs/architecture/index.rst`'s toctree.
- `docs/architecture/principles.md` gains one cross-reference.
- `docs/contributing.md`'s tooling prose is **replaced** by a pointer, not
  duplicated. Removing a mirror is part of the deliverable; adding a second one
  would reproduce the review's P4 in the docs.

## Part C — finish the plan-coordinate gate

The rule's note reads "Swept to zero 2026-08-03". That claim is wrong on three
independent counts, each measured below.

### C1. The Python rule under-matches — 8 live sites in `src/otto/**`

`.ast-grep/rules/no-plan-coordinates.yml` matches exactly three shapes:
`Task \d+`, `plan §`, `review finding F\d`.

- **It has no `Plan <n>` alternative at all.** otto's monitor work was
  organized as "Plan 5a / 5b / 5c" and "Plan 4", and the rule never covered
  that spelling. Seven live sites: `models/jsonschema.py:213`,
  `models/monitor.py:526`, `monitor/archive_edit.py:1`, `monitor/server.py:240`,
  `monitor/session.py:139`, `suite/suite.py:589`, `suite/suite.py:598`.
- **The literal space cannot match wrapped prose.** `monitor/server.py:684`
  reads `` ``archive_path`` (Task\n      5) `` — the formatter wrapped the
  docstring between the word and the number. Eighth site.

Fix the pattern on both axes: add a `Plan\s+\d+[a-z]?` alternative, and use
`\s+` throughout (`Task\s+\d+`, `plan\s+§`, `review\s+finding\s+F\d`). Then fix
all eight sites, and correct the rule's note — a gate that states a zero
baseline it does not hold is worse than one that states nothing.

### C2. One TypeScript rule would silently cover only half the tree

**Verified experimentally before writing this:** ast-grep's `language: tsx`
matches `.tsx` files *only*. A probe file `web/src/a.ts` containing
`// Task 12` was **not** flagged by a `tsx` rule, while `web/src/b.tsx` was.
Most shipped web source carrying plan coordinates is `.ts` — `data/*.ts`,
`charts/options.ts`, `covapp/format.ts`, `topo/layout.ts`, `ui/*.ts` — so a
single `tsx` rule would have shipped as a gate that cannot fire over the
majority of its own subject matter.

So: **two** rule files, `no-plan-coordinates-ts.yml` (`language: typescript`)
and `no-plan-coordinates-tsx.yml` (`language: tsx`), sharing one pattern and one
note. Both matching `kind: comment`.

Scope, with `files:`/`ignores:` globs verified to work as written:

- `files: web/src/**`
- `ignores: web/src/**/__tests__/**`, `web/src/**/*.test.ts`,
  `web/src/**/*.test.tsx` — test trees, out of scope per below.
- `ignores: web/src/components/**` — vendored Untitled UI. No matches today,
  but `check_untitledui_hash.sh` forbids hand-editing it, so a future re-vendor
  introducing one must not produce an unfixable red gate.
- `ignores: web/src/api/*.gen.ts` — generated; see C3.

`make lint-arch` and `nox -s lint` both invoke `ast-grep scan src/otto` with a
positional path, so the scan path must be extended to `src/otto web/src` in both
places. A new rule file alone would never execute.

### C3. One site is in a generated file and must be fixed upstream

`web/src/api/export.gen.ts:396` carries `Plan 5a lost three fix waves ...`. It
is generated by `scripts/gen_web_types.sh` from otto's pydantic models and
guarded by a `git diff --exit-code` drift gate. Hand-editing it would be
reverted by the next regeneration *and* fail that gate.

Its source is `src/otto/models/monitor.py:526` — one of C1's eight. Fixing the
docstring there and re-running `scripts/gen_web_types.sh` fixes both files;
the regenerated `export.gen.ts` is committed so the drift gate stays green.
This is why `web/src/api/*.gen.ts` is in the TS rules' ignore list rather than
their scope: the generated tree is not where that class of defect is fixable.

### C4. Burn down the ~158 shipped-TS blocks

Rewrite each self-contained, following `9db0212a`'s precedent — restate the
rationale in place rather than deleting the comment. Most are a parenthetical
inside otherwise-good prose (`Moved here from DirectoryPage.tsx (Task 6) so
RunsPage.tsx's top-files links share ...`), where dropping the parenthetical
leaves a correct sentence. Several already cite a **surviving** spec
(`Task 7 spec §4`, and `FilePage.tsx`'s reference to
`docs/superpowers/specs/assets/2026-07-24-coverage-ui/file-page.html`): keep the
spec half, drop the plan half. A minority carry real design rationale that only
the plan held; those get the rationale written out.

`web/src/covapp/testUtils.tsx` (4 matches) **is** in scope: it is a test helper
but matches none of the exclusion globs. The exclusions stay mechanical —
path-shaped, not judgement-shaped — so the boundary is checkable.

### Explicitly out of scope, and named as such

`web/src/**` test files (~109 blocks, 40 files) and `tests/**` Python
(~195 blocks, 74 files) are **not** touched and **not** gated. The existing
Python rule has always scoped to `src/otto/**`, so shipped-source-only is the
consistent reading of the policy rather than a compromise — test code is not
published and no reader meets it by accident.

This is recorded in both rules' `note:` fields with the measured counts, so the
next reader sees a bounded, deliberate exclusion instead of inferring the trees
were swept. Silence here would reproduce the exact defect this part fixes.

## Verification

- `nox -s docs` — `doc8`, markdown-doctest lint, `sphinx-build -E -a -W` (clean
  rebuild, warnings as errors), Sphinx doctest, `--doctest-modules`. The `-E -a`
  matters: incremental Sphinx misses broken `:doc:` refs, so a toctree addition
  is only proven by the clean rebuild.
- `make lint-arch` — proven **red first** against four separate probes, one per
  hole being closed: a `Plan 5a` in a Python docstring, a wrapped `Task\n 5` in
  a Python docstring, a `Task 12` in a `web/src` **`.ts`** file, and one in a
  **`.tsx`** file. Each must fail on its own; the `.ts`/`.tsx` pair is the
  specific mistake C2 documents, and only a per-language probe catches it. Then
  green.
- `scripts/gen_web_types.sh` re-run after the `models/monitor.py` docstring fix,
  with the regenerated `export.gen.ts` committed, and `make web`'s type-drift
  `git diff --exit-code` gate green — otherwise C3's fix lands as a CI failure.
- `make check-ts` — Biome, knip, tsc, vendored hash. Comment-only edits still
  have to clear Biome's formatter, which reflows comments.
- `make coverage` and the full unit tier — Part C touches only comments and
  Part A/B only docs, so the expectation is no behavioural change; the run is
  what makes that a claim rather than an assumption.
- A grep-based count, recorded in the commit message: shipped-source blocks
  before and after, per tree, so the "swept to zero" claim is falsifiable in the
  way `9db0212a`'s was not.

## Non-goals

- The full 171-page docs audit. Wave-scoped by decision.
- The six remaining Tier 1 items (`1.2`, `1.3`, `1.7`, `1.8`, `1.9`, `1.10`) and
  the nine remaining Tier 2 items. Untouched here; still open.
- `architecture/testing.md`. Accurate as written.
- Any new gate for TypeScript layering or pattern rules. The table records their
  absence; building them is its own decision.
