# Makefile quality/test parity: Python ↔ TS

**Date:** 2026-07-17
**Status:** Approved (brainstorm session with Chris)

## Goal

Restructure the Makefile's quality and test targets so every aspect has three
names — `<verb>-python`, `<verb>-ts`, and bare `<verb>` = both — and close the
two infrastructure gaps that currently make the TS side weaker than the Python
side. The TS/web code is held to the same standards as the Python code: same
verbs, same gate authority, same "the gate can actually fail" discipline.

## Decisions (made in the brainstorm)

1. **Naming axis: language** (`-python` / `-ts`), not frontend/backend.
   Toolchains are per-language; the Playwright dashboard lane is cross-cutting
   (pytest-driven frontend testing) and stays its own named lane. "Backend"
   would misname the CLI/testbed product.
2. **Static family: narrow verbs + `check` umbrella.** `lint` and `typecheck`
   stay separate (ruff/biome are sub-second; ty/tsc are the slow legs);
   `check-X = lint-X + typecheck-X`. Precedent: `biome check`, `cargo check`.
3. **Gap closure in scope:** e2e TS coverage (Playwright → merged, gated TS
   report) and knip (project-scope dead-code/unused-deps). **Out of scope:**
   bundle-size budget, dependency audit (pip-audit/npm audit), Node version
   matrix, renaming the Python tier slices.
4. **`format` means "apply all safe autofixes"**, not formatter-only:
   `format-python` = `ruff check --fix` + `ruff format`; `format-ts` =
   `biome check --write`. This is the only semantics under which
   `make format` reliably clears everything auto-fixable that `make lint`
   gates (Biome import-sorting is an assist, not formatting). Behavior
   change: ruff's safe lint autofixes start applying on `make format`.
5. **`web-test` → `test-ts`** (fast vitest run, no coverage). Deliberately no
   `test-python` twin and no bare `test` umbrella — this repo has no
   `make test` by design; the fast Python lane remains `coverage-unit`. Say so
   in `test-ts`'s help text.
6. **Hard rename, no deprecated aliases.** This is a tidy-up; all references
   are updated in the same change.

## Target map (after)

Suffix rule: `-python`/`-ts` are language legs. The existing tier/resource
suffixes (`coverage-unit`, `-integration`, `-hostless`, `-unix`, `-embedded`)
are an orthogonal family — slices of the Python suite — and keep their names.

### Static

| Target | Runs |
| --- | --- |
| `lint-python` | `ruff check .` + `ruff format --check .` |
| `lint-ts` | `biome check .` (rules + format + assists — the authoritative gate) + `knip` |
| `lint` | both |
| `typecheck-python` | `ty check` |
| `typecheck-ts` | `tsc --noEmit` |
| `typecheck` | both |
| `check-python` | = lint-python + typecheck-python |
| `check-ts` | = lint-ts + typecheck-ts |
| `check` | = check-python + check-ts |

This fixes the standing trap: `lint-ts` currently aliases the rules-only
`web-lint`, which the Makefile itself documents as strictly weaker than
`biome check` (unsorted imports pass it). After this change `make lint`
equals the CI Biome gate.

knip lands as `web/knip.json` + an npm script, wired into `lint-ts`. It is
the TS parity for ruff's unused-code detection at project scope: unused
exports, unused files, unused dependencies. Vendored Untitled UI source and
generated files get the same exclusions Biome and vitest-coverage already use.

### Format

| Target | Runs |
| --- | --- |
| `format-python` | `ruff check --fix .` then `ruff format .` (fix before format: fixes may need reformatting) |
| `format-ts` | `biome check --write .` |
| `format` | both |

### Test & coverage

| Target | Runs |
| --- | --- |
| `test-ts` | `vitest run` (renamed from `web-test`; no coverage, fast loop) |
| `coverage-python` | `dashboard` prerequisite (browser e2e feeding Python cov via `--cov-append`), then full pytest `-m "not stability and not browser"` with the 95 gate. This is today's bare `coverage` minus the web bolt-on. |
| `coverage-ts` | vitest coverage (JSON out) + the e2e TS coverage artifact, merged into one istanbul report with one threshold gate (see below) |
| `coverage` | = coverage-python + coverage-ts |
| `coverage-unit` / `-integration` / `-hostless` / `-unix` / `-embedded` | unchanged Python tier/resource slices |

### Umbrellas & pipeline

| Target | Runs |
| --- | --- |
| `validate-python` | clean-dist + check-python + `$(COVERAGE_TARGET)` (default `coverage-python`; `ci` still overrides with `coverage-hostless`) + docs |
| `validate-ts` | check-ts + coverage-ts (replaces `web-check`) |
| `validate` | both |
| `all` / `ci` / `release` | rewired to the new names; semantics unchanged |

## E2E TS coverage (the real plumbing)

Today the Playwright dashboard suite drives the built TS bundles but collects
zero TS coverage; the vitest thresholds were *lowered* to 81/73/80/82 because
TopologyPage.tsx is "covered by the e2e instead" — coverage asserted nowhere.

Design:

- **Measure the real artifact.** No instrumented second build (a green
  browser test must certify the shipped bundle). Both prod bundles (dashboard
  and covreport) emit **hidden sourcemaps** (`build.sourcemap: "hidden"` —
  `.map` files emitted, never referenced). The maps ride along in dist and
  the wheel; benign (no absolute URLs, so the air-gap gate is unaffected;
  wheel grows by a few MB).
- **Collection:** a pytest fixture in the dashboard/covreport browser suites
  starts Playwright's Chromium V8 JS-coverage per page and dumps raw V8
  coverage JSON at session end. Chromium-only, mirroring how the Python side
  pins one engine for coverage (numbers are engine-independent); the fixture
  no-ops on firefox/webkit so `dashboard-all` still works.
- **Conversion + merge:** a small node-side script converts the V8 dumps to
  istanbul JSON via the sourcemaps and merges them with vitest's JSON output
  into one report; the single threshold gate runs on the merged report.
  Default choice: `monocart-coverage-reports` (handles V8→istanbul,
  sourcemap resolution, and merging in one tool); fall back to hand-wiring
  `v8-to-istanbul` only if it can't express the exclusion list.
- **Self-healing artifact:** the e2e coverage JSON is a real file target
  stamped on `WEB_SRCS` + the browser-suite test sources, produced by the
  dashboard lane — same pattern as `DASHBOARD_DIST`. A cold
  `make coverage-ts` runs the browser lane itself (honest, if heavy; the fast
  loop is `test-ts`). Inside bare `coverage`, make's file stamping means the
  dashboard runs once, not twice.
- **Thresholds:** after the merge lands, measure, then raise the gate to at
  least the pre-topology values (85/78/83/86), setting exact numbers from
  measurement minus a small margin. Measure first, then set — the gate must
  be able to fail.

## Retired names & migration

Retired (hard-removed): `web-lint`, `web-format`, `web-format-check`,
`web-biome`, `web-typecheck`, `web-coverage`, `web-check`. The former
alias-only `lint-ts`, `format-ts`, `typecheck-ts`, `coverage-ts`,
`validate-ts` become real targets.

Kept as `web-*` (artifact/dev family, not language-parity): `web` (build
dist), `web-dev`, `web-install`, `web-clean`.

References to update in the same change:

- `.github/workflows/ci.yml`: the web-quality job is Node-only (no browsers),
  so it cannot run the merged gate inside `validate-ts`. It runs
  `make check-ts coverage-ts-unit` instead — `coverage-ts-unit` is the
  vitest-only floor (today's thresholds, kept in vite.config.ts), the exact
  analogue of `coverage-hostless`'s reduced CI gate vs. the full local 95.
  The merged `coverage-ts` gate runs locally via `make coverage`/release.
  `tests/unit/test_ci_web_gate.py` is rewritten (not deleted) to pin the new
  chain. *(Amended during planning: the original line here said
  "web-check → validate-ts", which the browserless CI runner can't do.)*
- `release` recipe: `web-check` → `validate-ts`; `lint-python`/
  `typecheck-python` legs can become `check-python`.
- `all`, `ci`, `validate` target bodies.
- `help` output: annotate new targets into the existing categories; Makefile
  gets section banners (Static / Test & Coverage / Build & Release / Docs /
  Lab / Dev).
- docs/contributing.md and any other docs naming retired targets.
- noxfile.py is untouched (CI's nox lint/typecheck/tests sessions stay).

## Verification

- Every new target runs green on a clean tree; `make help` renders the new
  layout.
- **Proven-red gates** (regression guards must fail against the pre-fix
  state): `lint-ts` fails on an unsorted-import fixture that old `web-lint`
  passed; the merged TS coverage gate fails when its threshold is raised
  above measured (then set correctly); knip fails on a planted unused export.
- The merged report shows nonzero coverage for TopologyPage.tsx — the
  concrete file that motivated the e2e-coverage work.
- `grep -r` over the tree (docs, scripts, CI, Makefile, memory of README
  snippets) finds zero references to retired target names.
- `make -n` dry-run safety warnings (release, wheel-check) preserved.
- Full gate: `make validate` green locally; CI green.
