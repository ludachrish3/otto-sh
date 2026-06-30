# Selection-Based `otto test` — Listing + Running by Suite / Marker / Test

**Date:** 2026-06-30
**Status:** Draft for review
**Scope:** The `otto test` invocation model — how tests are *selected*, *listed*, and *run*. This
is **Part 1** of a larger "under-tested areas" effort. Part 2 (remove the `repeat` feature) and
Part 3 (coverage tests for `nc`/`interact`/`docker`/`completion_cache`) get their own specs and are
**out of scope here**.

**Build order:** the design is unified, but the implementation is **phased**:

- **Phase 1 — Listing + the `--list-suites` bugfix.** Ships first; fixes a live user-facing bug.
- **Phase 2 — Running by selector** (`--markers` / `--tests`, cross-suite). Builds on Phase 1's
  selection plumbing.

---

## 1. Motivation

### 1a. A live bug: `otto test --list-suites` is broken three ways

| Bug | Symptom | Root cause |
|---|---|---|
| **A** | Hard traceback | The inner collection `pytest.main()` in `Repo.collect_tests()` inherits `filterwarnings=error` from the rootdir `pyproject.toml`. `pytest_asyncio` is already imported in the parent `otto` process, so the inner config's assertion-rewrite step emits `PytestAssertRewriteWarning: Module already imported…`, which `error` escalates to a fatal config-time error. |
| **B** | "(no tests found)" despite real tests | `collect_tests()` redirects `stdout`/`stderr` to `io.StringIO()`, which has no `fileno()`. Any collected `conftest`/plugin needing a real fd crashes collection. In-repo this is otto's own `tests/conftest.py:149` `faulthandler.register(...)` → `io.UnsupportedOperation: fileno` → `INTERNALERROR` → 0 items. |
| **C** | The failure is **silent** | `collect_tests()` ignores `pytest.main()`'s return code and returns `collector.items` regardless. A failed collection becomes an empty list, indistinguishable from "genuinely no tests." This masked A and B and would mask any future collection failure. |

### 1b. An architectural mistake

`Repo.collect_tests()` runs a **full inner pytest collection** to discover suite names. But a
"suite" is a `@register_suite()` class, and `otto test <SuiteName>` dispatches to a subcommand
built directly from `_SUITE_REGISTRY` (`cli/test.py:631-632`). The registry is the **authoritative**
source; `collect_tests()` re-derives a subset of it the slow, fragile way. Its only `src/`
consumer is the listing display.

### 1c. Why the test suite missed all of it (the gap to close)

The unit tests **mock `Repo.collect_tests`** or pass pre-built items (panel rendering only); the
e2e completion test only asserts `--list-suites` *appears as a flag*; and `otto test <Suite>`
(running a suite) goes through a **different** inner pytest (`run_suite`). **Nothing drives the real
`collect_tests()` → callback → panel chain against a real repo.**

### 1d. The opportunity: selection-based `otto test`

Today `otto test` **requires** naming a registered suite; you cannot run "all `slow` tests" or "this
handful of tests across two suites." pytest can (`-m`, `-k`, nodeids), and we don't want to lose that
power. The selectors we must add for `--list-tests` (marker/suite/test filters) are the *same*
selectors that would drive a run. So we unify the model rather than bolt on listing alone.

---

## 2. The unified model

**`otto test` resolves a *selection* of tests, then applies a *verb*.**

- **Selectors** (compose; all optional):
  - **suite** — the registered suite subcommand, e.g. `otto test TestDevice` (a convenient, common
    selector; under the hood a class scope).
  - **`--markers <expr>`** — marker expression (pytest `-m`). Spans suites.
  - **`--tests <pattern|nodeids>`** — specific tests (pytest `-k`/nodeid). May span suites.
- **Verbs:**
  - **run** *(default)* — execute the selection.
  - **list** — `--list-tests` lists the selection instead of running it.
- **Discovery helpers** (eager, exit): **`--list-suites`** (runnable suite names),
  **`--list-markers`** (markers available to filter on).

| Invocation | Selection | Verb |
|---|---|---|
| `otto test TestDevice` | one suite | run |
| `otto test --markers slow` | all `slow` tests, any suite | run |
| `otto test --tests test_login` | matching tests, any suite | run |
| `otto test TestDevice --markers slow` | `slow` tests in `TestDevice` | run |
| `otto test --list-tests` | everything | list |
| `otto test --list-tests --markers slow` | `slow` tests | list |
| `otto test --list-tests TestDevice` | `TestDevice`'s tests | list |
| `otto test --list-suites` | — | list suite names |
| `otto test --list-markers` | — | list markers |

The wiring hook is the same throughout: `invoke_without_command=True` + `ctx.invoked_subcommand`
let the parent `otto test` callback see "selectors but no named suite" and act (list or run) without
a subcommand.

---

## 3. Goals / Non-goals

**Goals**

1. Fix the `--list-suites` bug (A/B/C) by switching suite listing to the **registry** and hardening
   `collect_tests()` for the remaining `--list-tests` use.
2. Add **selector-aware** collection (`markers`, `suite`, `tests`) shared by listing and (Phase 2)
   running.
3. `--list-tests` (list a selection, no run); `--list-markers`; delete the unused
   `get_test_files_panel`.
4. **Phase 2:** run a selection by `--markers` / `--tests`, **across suites**, generalizing
   `run_suite`.
5. **Real, non-mocked tests** for both listing and selector-running.

**Non-goals**

- `repeat` removal (Part 2); coverage tests for other modules (Part 3).
- Per-suite `Options` binding in a no-suite selector run (see §5.4 — name the suite for that).
- Running an individual test *method* as its own command (selection by `--tests` covers this need).

---

## 4. Design — Phase 1 (listing + bugfix) — **build first**

### 4.1 `--list-suites` from the registry

`_SUITE_REGISTRY` stores `(suite_class.__name__, sub_app)`; the decorator already computes
`suite_file = inspect.getfile(suite_class)` but discards it.

- **Extend the registry entry** to carry the suite's source file (and class) — e.g. a small
  `RegisteredSuite(name, sub_app, file, cls)` record. `cli/test.py:631-632` unpacks accordingly.
- **Add `Repo.registered_suites()`** → registry entries whose `file` resolves under this repo's
  `sut_dir`, giving per-repo attribution for the existing panel layout.
- `get_test_suites_panel` renders from `self.registered_suites()` (reusing `_make_test_panel`)
  instead of taking collected items. `list_suites_callback` stops calling `collect_tests()`.

Result: `--list-suites` lists exactly the runnable subcommands, grouped by repo, with no inner
pytest — bugs A/B/C cannot occur on this path.

### 4.2 Harden `collect_tests()` (+ selectors)

Three fixes to the inner `pytest.main()` block:

- **(A)** Add `"--override-ini", "filterwarnings="` — collection never escalates warnings to errors.
- **(B)** Replace the `io.StringIO()` redirect with a **real-fd sink** (`open(os.devnull, "w")` for
  both streams) so `fileno()`-dependent collected code works. *(Verified: returns 16 items in the
  real otto process where `StringIO` returned 0.)*
- **(C)** Capture the return code; on collection failure (`INTERNAL_ERROR`/`USAGE_ERROR`/…),
  **surface a clear, per-repo error line** (repo name + hint) rather than silently returning `[]`.
  "No tests" and "collection crashed" must be distinguishable; one bad repo must not silently blank
  the others.

**New selector parameters** `collect_tests(markers=None, suite=None, tests=None)`:

- `markers` → inner `-m <expr>`.
- `suite` → restrict to that registered suite (via its registry `file`, and/or `-k <ClassName>`).
- `tests` → inner `-k <pattern>` / nodeid selection.

Default (no args) behaves as today (all tests in the repo's `tests` dirs).

### 4.3 `--list-tests`

**Model:** `--list-tests` tells the parser to *list* rather than *run*; the suite and `--markers`
are resolved normally and the resolved selection determines what's listed.

- A `--list-tests` option on the **parent `otto test` callback**, handled in the callback **body**
  (not `is_eager` — it must observe `--markers`).
- When set: read `markers`; read `ctx.invoked_subcommand` (resolved suite, or `None`); call
  `repo.collect_tests(markers=…, suite=…)` per repo; render `get_tests_panel(items)` (lines
  `relpath::[Class::]test`); `raise typer.Exit` — the suite is **not** run.

**UX** (the flag sets list-mode; an optionally-specified suite/marker narrows it):

```text
otto test --list-tests                 # every test in every repo
otto test --list-tests --markers slow  # tests matching marker `slow`
otto test --list-tests TestDevice      # tests in the TestDevice suite only
```

> **Decided:** only this ordering ships. Typer/click attaches options appearing *after* a subcommand
> to the subcommand, so the suite-scoped form is `--list-tests <Suite>` (flag before the name). The
> `otto test <Suite> --list-tests` ordering is **deferred** until users ask.

`get_tests_panel` (currently unused/untested) gains a real consumer + tests; its class branch
(`_test_run_syntax`) is exercised by a class-bearing fixture.

### 4.4 `--list-markers` (new)

Eager-exit flag. Lists markers available to `--markers`, sourced from each repo's configured pytest
markers (the `markers =` ini lines) plus otto's documented markers — **no inner collection** (a
static config concern; collecting would reintroduce the failure surface we're removing).

### 4.5 Delete `get_test_files_panel`

Files are not `otto test`-addressable; the method is unused and unwired. Remove it and references.
`_test_run_syntax` stays (used by `get_tests_panel`).

### 4.6 Phase 1 testing

1. **Subprocess CLI tests** against a fixture repo + lab, asserting real output:
   `--list-suites` lists the fixture's registered suite names and exits 0 (**directly reproduces
   A/B/C**); `--list-tests` (+ `--markers`, + `<Suite>`) lists the right tests; `--list-markers`
   lists configured markers.
2. **`collect_tests()` regression unit tests** (no mocking of the inner pytest) on a tiny `tmp_path`
   repo: collects with `stdout` redirected (guards **B**); collects with `pytest_asyncio` imported
   in-process (guards **A**); a deliberately broken conftest makes `collect_tests()` **surface an
   error**, not `[]` (guards **C**); `markers=`/`suite=`/`tests=` actually narrow the result.
3. **Registry-driven `--list-suites`**: `Repo.registered_suites()` attributes suites to the right
   repo; the panel renders registry names.
4. **Keep** existing panel-render unit tests but **drop** the mocked-`collect_tests` assumptions
   that hid the bug; re-point at real/tmp-fixture paths where feasible.

---

## 5. Design — Phase 2 (run by selector) — **build after Phase 1**

### 5.1 Generalize `run_suite` into a selection run

`run_suite` today runs one suite via `pytest.main([suite_file, "-k", ClassName, … , -m markers])`
plus `OttoPlugin(sut_test_dirs=…)`, stability, coverage, monitor, junit. A selection run is the same
pipeline with the **single-suite scoping removed**:

- Target `*sut_test_dirs` (all repos' test dirs) instead of one `suite_file`.
- Drop the `-k ClassName` scope; apply the **selectors** instead: `-m <markers>` and/or
  `-k <tests>` / nodeids.
- Everything else (OttoPlugin, `--junitxml`, stability `--iterations`/`--duration`, `--cov`,
  monitor) carries over unchanged at session scope.

Refactor: extract the arg/plugin assembly so `run_suite(suite)` and a new
`run_selection(markers, tests)` share one core, differing only in the target/selector args and the
`Options` plugin (§5.4).

### 5.2 Invocation wiring

- The `otto test` callback gains `invoke_without_command=True`.
- In the callback, when `ctx.invoked_subcommand is None`:
  - selectors present (`--markers`/`--tests`) and **not** `--list-tests` → **run the selection**;
  - `--list-tests` → list (Phase 1);
  - neither → help (preserves today's `no_args_is_help` behavior).
- A named suite subcommand continues to run that suite (with its `Options`), now also honoring
  `--markers`/`--tests` as additional filters within it.

### 5.3 Cross-suite, multiple suites in one session

This is a **first-class capability** (run all `slow` tests; run a handful spanning suites), not an
edge case. It is **structurally already supported**: `OttoPlugin` takes `sut_test_dirs` plural and
gates collection via `pytest_ignore_collect`, and suite setup/teardown is implemented with
**pytest class-scoped fixtures** (`setup_class`/`teardown_class`, cached per-class —
`suite/plugin.py:183-185`). pytest fires each selected class's setup/teardown as it enters/exits
that class, so a multi-suite session runs each suite's lifecycle correctly. The single-suite
`-k ClassName` scope is the *only* thing that has kept this from being exercised.

**Design tasks for Phase 2** (validate via TDD on a fixture repo with ≥2 suites sharing a marker):
per-suite setup/teardown fires once per suite in a multi-suite run; junit/stability/coverage
aggregate correctly across suites; ordering and lab/host lifecycle are sane when suites touch the
same hosts.

### 5.4 The `Options` tradeoff

Each `@register_suite` class may declare an inner `Options` dataclass (suite-specific CLI flags),
bound today via `OttoOptionsPlugin(opts_instance)`. A **no-suite selector run** spans suites with
differing `Options`, so it binds **no** suite-specific options — it runs with shared
`RepoOptions`/defaults (`opts_instance=None`, which the option plugin already tolerates for
suites without `Options`). To pass suite-specific options, **name the suite**. This mirrors pytest
(`pytest -m slow` has no per-suite CLI options) and is documented, not hidden.

### 5.5 Phase 2 testing

Real subprocess runs against a fixture repo with **two** suites sharing a marker:
`otto test --markers <shared>` runs tests from both suites (assert via junit/output);
`otto test --tests <pattern>` runs a cross-suite handful; each suite's setup/teardown fires;
stability (`--iterations`) and `--cov` still function on a selection run; a selector matching nothing
exits cleanly with a clear message (not a traceback).

---

## 6. Risks & mitigations

- **`--list-tests` wiring** (eager vs. selectors vs. subcommand options) — mitigated by the
  parent-flag UX (§4.3) and TDD via `CliRunner`/subprocess.
- **Multi-suite session semantics** (§5.3) — the biggest Phase-2 unknown despite the structural
  support; gated behind a real ≥2-suite fixture test before claiming it works.
- **`collect_tests` failure surfacing** — must be loud and per-repo, never silent (the whole point
  of fixing C).
- **`--override-ini filterwarnings=`** also silences legitimate *collection* warnings — acceptable;
  the real run (`run_suite`/`run_selection`) keeps strict warnings.
- **`--list-markers` source** may under-report dynamically-registered markers — acceptable for a
  "what can I filter on" helper; documented as config-sourced.
- **`--tests` vs `--list-tests` naming** — distinct (selector vs. verb); ensure help text makes the
  difference obvious.

---

## 7. Delivery

- Stage-only; **no self-commit**. Paste-able commit messages provided on completion.
- **Two commits**, matching the phases: (1) listing rework + bugfix + tests; (2) run-by-selector +
  tests. Phase 1 is independently shippable and fixes the live bug.
- Gate per phase: `make coverage`; full gate (`coverage` + `nox` + `typecheck` + `docs`) before each
  hand-off. No import-budget snapshot change expected (no module add/remove); re-run
  `make import-snapshot` if the import graph shifts. Single `-n auto` passes only.
