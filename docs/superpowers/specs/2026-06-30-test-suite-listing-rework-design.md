# Test/Suite Listing Rework — Design

**Date:** 2026-06-30
**Status:** Draft for review
**Scope:** `otto test` listing flags (`--list-suites`, `--list-tests`, `--list-markers`) and the
underlying collection machinery. This is **Part 1** of a larger "under-tested areas" effort;
Part 2 (remove the `repeat` feature) and Part 3 (coverage tests for `nc`/`interact`/`docker`/
`completion_cache`) get their own specs and are **out of scope here**.

---

## 1. Problem

`otto test --list-suites` is broken three ways, and the breakage exposed an architectural
mistake in how suites are listed.

### The three defects

| | Symptom | Root cause |
|---|---|---|
| **A** | Hard traceback | The inner collection `pytest.main()` in `Repo.collect_tests()` inherits `filterwarnings=error` from the rootdir `pyproject.toml`. `pytest_asyncio` is already imported in the parent `otto` process, so when the inner config marks plugins for assertion rewrite it emits `PytestAssertRewriteWarning: Module already imported…`, which the `error` filter escalates to a fatal config-time error. |
| **B** | "(no tests found)" despite real tests | `collect_tests()` redirects `stdout`/`stderr` to `io.StringIO()`, which has no `fileno()`. Any collected `conftest`/plugin that needs a real fd crashes collection. In-repo this is otto's own `tests/conftest.py:149` `faulthandler.register(...)` → `io.UnsupportedOperation: fileno` → `INTERNALERROR` → 0 items. A downstream user's `conftest`/plugins can hit the same class of failure. |
| **C** | The failure is **silent** | `collect_tests()` ignores `pytest.main()`'s return code and returns `collector.items` regardless. A failed collection becomes an empty list, indistinguishable from "this repo genuinely has no tests." This masked A and B and would mask any future collection failure. |

### The architectural mistake

`Repo.collect_tests()` runs a **full inner pytest collection** as a proxy to discover suite names.
But a "suite" is a `@register_suite()` class, and `otto test <SuiteName>` dispatches to a
subcommand built directly from `_SUITE_REGISTRY` (`cli/test.py:631-632`). The registry is the
**authoritative** source of runnable suite names; `collect_tests()` re-derives a subset of that
information the slow, fragile way. `collect_tests()`'s only `src/` consumer is the listing display
(`cli/test.py:388`).

### Why the test suite missed all of it (the real gap)

- The unit tests (`tests/unit/cli/test_listing.py`) **mock `Repo.collect_tests`** or pass
  pre-built `CollectedTest` items — they exercise panel *rendering*, never the real collection.
- The e2e completion test only asserts that `--list-suites` *appears as a flag* in completion
  output; it never runs the command.
- `otto test <SuiteName>` (actually running a suite) goes through a **different** inner pytest
  (`run_suite`), so running real tests never exercises `collect_tests()`.

Net: **nothing drives the real `collect_tests()` → callback → panel chain against a real repo.**
That hollow spot is the thing to fix, not just the symptom.

---

## 2. Goals

1. `--list-suites` reads the **suite registry** — no pytest collection, no A/B/C, accurate.
2. Harden `collect_tests()` (still needed for `--list-tests`): fix A, B, and C.
3. `--list-tests` becomes **selector-aware** — lists tests filtered by the current suite and/or
   `--markers`, then **exits without running** the suite.
4. Add `--list-markers` — list the markers available to filter on.
5. Delete the unused, unaddressable `get_test_files_panel`.
6. Add **real, non-mocked** tests that run the listing commands against a fixture repo and assert
   real suite/test/marker names — coverage that would have caught A/B/C.

### Non-goals

- Removing the `repeat` feature (Part 2 spec).
- Coverage tests for `nc.py`/`interact.py`/`docker*`/`completion_cache.py` (Part 3 spec).
- Changing how suites are *run* (`run_suite` is untouched).
- Allowing `otto test` to run an individual test method (suites remain the run unit; tests are
  informational + marker-filterable).

---

## 3. Design

### 3.1 `--list-suites` from the registry

`_SUITE_REGISTRY` currently stores `(suite_class.__name__, sub_app)`. The decorator already
computes `suite_file = inspect.getfile(suite_class)` but discards it.

- **Extend the registry entry** to carry the suite's source file (and class), e.g. a small
  `RegisteredSuite(name, sub_app, file, cls)` record (or a 4-tuple). `cli/test.py:631-632`
  updates to unpack accordingly.
- **Add `Repo.registered_suites()`** → the registry entries whose `file` resolves under this
  repo's `sut_dir`. This is the per-repo attribution that preserves the existing per-repo panel
  layout.
- `get_test_suites_panel` no longer takes collected `items`; it renders from
  `self.registered_suites()` (reusing `_make_test_panel` for the banner/box). `list_suites_callback`
  stops calling `collect_tests()`.

Result: `otto test --list-suites` lists exactly the runnable `otto test <name>` subcommands,
grouped by repo, with zero inner pytest.

### 3.2 Harden `collect_tests()`

Three changes to the inner `pytest.main()` block:

- **(A)** Add `"--override-ini", "filterwarnings="` so collection never escalates warnings to
  errors. Collection is metadata-gathering; warnings-as-errors is inappropriate there.
- **(B)** Replace the `io.StringIO()` redirect with a **real-fd sink** (`open(os.devnull, "w")` for
  both streams), so `fileno()`-dependent code in collected conftests/plugins works. *(Verified:
  with this change `collect_tests()` returns 16 items in the real otto process where the
  `StringIO` version returned 0.)*
- **(C)** Capture the `pytest.main()` return code. On a collection failure (`INTERNAL_ERROR`,
  `USAGE_ERROR`, etc.), **surface a clear error** (raise or log with the repo name and a hint),
  rather than silently returning `[]`. "No tests" and "collection crashed" must be
  distinguishable.

**New optional selector parameters** on `collect_tests(markers=None, suite=None)`:

- `markers` → passed to the inner pytest as `-m <expr>`.
- `suite` → restrict collection to that registered suite (via its registry `file`, and/or `-k
  <ClassName>`), so `--list-tests <Suite>` only collects that suite's tests.

Default call (no args) behaves as today (all tests in the repo's `tests` dirs).

### 3.3 `--list-tests` (selector-aware, exits without running)

- A `--list-tests` option on the **parent `otto test` callback** (`main`), handled in the callback
  **body** (not as an `is_eager` callback — it must observe `--markers`, which an eager callback
  fires before).
- When set, the callback:
  1. reads `markers` (a parent option, already parsed),
  2. reads `ctx.invoked_subcommand` (the suite name, or `None` for "all suites"),
  3. calls `repo.collect_tests(markers=…, suite=…)` per repo,
  4. renders the per-repo `get_tests_panel(items)` (each line `relpath::[Class::]test`),
  5. `raise typer.Exit` — the suite is **not** run.

**UX** (parent flags precede the suite name, consistent with how `--markers` works today):

```
otto test --list-tests                     # every test in every repo
otto test --markers slow --list-tests      # tests matching marker `slow`
otto test --list-tests TestDevice          # tests in the TestDevice suite only
```

> **Open implementation detail (resolve via TDD):** Typer/click attaches options that appear
> *after* a subcommand name to the subcommand, so the suite-scoped form is `--list-tests <Suite>`
> (flag before the name). If a `otto test <Suite> --list-tests` ordering is also desired, the suite
> subcommands would need to honor a shared "list-only" mode read from `ctx.meta`. Start with the
> parent-flag form; add per-suite list mode only if the ergonomics demand it.

`get_tests_panel` is kept (it already exists, untested) and now has a real consumer + tests. The
`_test_run_syntax` class branch (currently uncovered) is exercised by a class-bearing fixture.

### 3.4 `--list-markers` (new)

- Eager-exit flag on the `otto test` callback. Lists the markers a user can pass to `--markers`.
- **Source (no inner collection):** each repo's configured pytest markers (the `markers =` lines
  in the repo's pytest config / `pyproject.toml [tool.pytest.ini_options]`) plus otto's own
  documented markers. Rendered per repo via `_make_test_panel`.
- Rationale for not collecting: markers available for filtering are a static config concern;
  deriving them from a fragile collection would reintroduce the failure surface we're removing.

### 3.5 Delete `get_test_files_panel`

Files are not `otto test`-addressable; the method is unused and unwired. Remove it (and any
references). `_test_run_syntax` stays (used by `get_tests_panel`).

---

## 4. Testing (the point of the exercise)

Add coverage that exercises the **real** chain, not mocks:

1. **Integration/e2e (subprocess) tests** — run the actual CLI against a fixture repo with a lab,
   in a subprocess, and assert real output:
   - `otto test --list-suites` lists the fixture's registered suite names (e.g. `TestDevice`,
     `TestCoverageProduct`) and exits 0 — **this directly reproduces and guards bug A/B/C**.
   - `otto test --list-tests` lists individual tests; `--markers <m> --list-tests` filters;
     `--list-tests <Suite>` scopes to one suite.
   - `otto test --list-markers` lists configured markers.
2. **`collect_tests()` regression unit tests** (no mocking of the inner pytest) against a tiny
   `tmp_path` fixture repo:
   - Collects N items with `stdout` redirected (guards **B** — would fail on `StringIO`).
   - Collects cleanly when `pytest_asyncio` is imported in the process (guards **A**).
   - When collection fails (e.g. a deliberately broken conftest), `collect_tests()` **surfaces an
     error** instead of returning `[]` (guards **C**).
   - `markers=`/`suite=` selectors actually narrow the result set.
3. **Registry-driven `--list-suites` unit tests** — `Repo.registered_suites()` attributes suites
   to the right repo; the panel renders registry names.
4. **Keep** the existing panel-render unit tests (`test_listing.py`) but **drop** the mocked
   `collect_tests` assumptions where they hid the bug; re-point them at the real or
   tmp-fixture path where feasible.

A fixture repo with at least one `@register_suite` class (incl. a parametrized test and a
marker) is needed; `tests/repo1` already provides this shape and can seed the fixture.

---

## 5. Risks & mitigations

- **`--list-tests` wiring** (eager vs. selectors vs. subcommand options): the single most fiddly
  piece. Mitigated by the parent-flag UX in §3.3 and TDD on the real CLI via `CliRunner`/subprocess.
- **`collect_tests` rc-surfacing shape**: decide whether a collection failure raises (and aborts
  the whole listing) or degrades per-repo with a visible error line. Lean **per-repo visible error**
  so one bad repo doesn't blank the others — but it must be loud, never silent.
- **`--override-ini filterwarnings=`** also silences *legitimate* collection warnings. Acceptable:
  collection is ID-gathering, not validation; the real run (`run_suite`) keeps strict warnings.
- **`--list-markers` source**: configured markers may under-report dynamically-registered ones.
  Acceptable for a "what can I filter on" helper; documented as config-sourced.

---

## 6. Delivery

- Stage-only; **no self-commit**. A paste-able commit message is provided on completion.
- Gate: `make coverage` per phase; full gate (`coverage` + `nox` + `typecheck` + `docs`) before
  hand-off. No import-budget snapshot change expected (no module add/remove), but re-run
  `make import-snapshot` if the import graph shifts. Single `-n auto` passes only.
- Lands as one focused commit (listing rework + bugfix + tests), separate from Parts 2 and 3.
