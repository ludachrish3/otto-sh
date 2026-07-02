# pytest-Native Flexibility — repo-wide conftest, suite-less runs, auto-registration

**Date:** 2026-07-02
**Status:** Draft for review
**Scope:** otto's pytest-wrapping layer: conftest discovery, the `otto test` run paths, and suite
registration. Companion spec: `2026-07-02-otto-init-design.md` (scaffolding) — **this spec lands
first**; the scaffold's suite template depends on auto-registration.

**Relationship to prior work:** this spec is the successor to
`2026-06-30-selection-based-otto-test-design.md`. That spec's Phase 1 (listing + `--list-suites`
bugfix) shipped in `b5c42c1`. Its Phase 2 (run by selector) was never built; this spec incorporates
it by reference, **amends** its Phase 2 (§2.3, §2.4), and adds two new workstreams (§2.1, §2.2). Where the two documents conflict, this one wins.

---

## 1. Motivation

otto wraps pytest in-process (`pytest.main` at `cli/test.py:314`) and promises — in the `OttoSuite`
docstring — that "all standard pytest features work natively — fixtures, parametrize, markers,
conftest.py". Three gaps break that promise today:

1. **Repo-wide `conftest.py` is silently ignored.** `run_suite` passes
   `--confcutdir={suite_file.parent}` (`cli/test.py:280`), so only a conftest in the *same
   directory* as the suite file loads. A fixture defined at the repo root or at `tests/` (when
   suites live in `tests/feature_x/`) never appears. The restriction exists for one reason: to keep
   otto's own `tests/conftest.py` out of inner sessions when the suite lives inside the otto tree
   (the in-tree example repos).
2. **Individual tests are not runnable without a suite.** The only run path is
   `otto test <SuiteName>` → `pytest <suite_file> -k <ClassName>`. Plain pytest functions are
   collectable (`--list-tests`) but cannot be run through otto at all. The 2026-06-30 spec designed
   this (its Phase 2) but it was never implemented.
3. **`@register_suite()` is boilerplate.** Its only user-facing job is to mark a class the CLI can
   already identify by pytest's own rule (an `OttoSuite` subclass named `Test*` in a discovered
   test file). Forgetting the decorator silently hides the suite from the CLI.

## 2. Design

### 2.1 Repo-wide conftest: cut at the SUT repo root

`run_suite` (and the new selection-run path, §2.3) sets `--confcutdir=<repo.sut_dir>` — the
directory holding `.otto/` — instead of the suite file's parent. The suite file is mapped to its
discovered `Repo` (the file is under one of the repo's `tests` dirs; `Path.is_relative_to` over the
discovered repos). Fallback when no repo matches (defensive; should not happen): current behavior,
the file's parent.

Consequences:

- The full conftest hierarchy inside a user repo works: root, `tests/`, per-subdirectory.
- otto's own `tests/conftest.py` stays excluded for the in-tree example repos, because it sits
  *above* `tests/repo1/` — the original protection is preserved by construction.
- `tests/repo1/conftest.py` (a `sys.path` shim for direct-pytest runs) will now also load under
  `otto test`. It is idempotent; the e2e suite must prove it harmless (§4).

### 2.2 Auto-registration; `@register_suite()` deleted

`OttoSuite.__init_subclass__` registers every subclass whose name matches pytest's own collection
rule (`Test*`) into the existing `SUITES` registry:

- **Same key** (class name), **same duplicate rules** (same-file re-registration allowed;
  cross-file collision is a loud error), **same captured data** (class, inner `Options` class,
  source file via `inspect.getfile`), **same Typer sub-app synthesis** — moved out of the
  decorator body, triggered from `__init_subclass__` at class-creation time (identical timing:
  both fire when the test module is imported at bootstrap Phase 2).
- Shared base classes (`BaseSomething(OttoSuite)`) are skipped naturally — they don't match
  `Test*`. No opt-in/opt-out knobs. (Repos that override pytest's `python_classes` ini are out of
  scope; otto's contract is the `Test*` default.)
- `@register_suite()` is **deleted** (not deprecated). In-tree usages (example repos, unit tests,
  docs, `OttoSuite` docstring examples) are updated in the same change. A stale decorator import
  in a user repo becomes a loud `ImportError` naming the new model.

Downstream consumers are untouched mechanically: `otto test <SuiteName>` subcommands, per-suite
`Options` flags, `--list-suites`, duplicate detection, and completion cache v8 all read the same
registry, which is now populated automatically.

The `tests` key in `settings.toml` remains the discovery root and becomes *more* load-bearing:
it drives (a) bootstrap import of `test_*.py` — which is what fires auto-registration, (b) the
collection scope for `--list-tests` and selection runs, (c) `OttoPlugin.pytest_ignore_collect`,
and (d) the completion-cache fingerprint. Documented as: **"`tests` defines where test discovery
happens."**

### 2.3 Suite-less runs: `--tests` (comma-separated) and `-m` alone

Implements the 2026-06-30 spec §5 (generalize `run_suite` → shared core + `run_selection`;
`invoke_without_command=True` wiring; selectors compose with the suite subcommand) with these
**amendments**:

- **`--tests` takes one comma-separated value of exact test names** —
  `otto test --tests test_login,test_logout` — matching otto's existing list-option convention
  (`param_synth.py` comma parsing; `OTTO_LAB`). Not a `-k` pattern.
- **Names resolve via the existing one-shot collection pass** (hardened in Phase 1) to exact
  nodeids before the run: a bare name matches every collected test with that function name
  (parametrized variants included); the qualified form `TestClass::test_name` disambiguates.
  An unknown name is a loud error with did-you-mean suggestions (the established `Registry`
  pattern), never a silent no-match run.
- **`-m EXPR` with no suite subcommand triggers a selection run** over all discovered test dirs
  (new behavior; previously an error). Bare `otto test` with no selectors still shows help — no
  accidental run-everything on a live bed.
- **Multi-repo selection runs execute one inner pytest session per repo**, sequentially, each with
  its own `--confcutdir=<repo root>` (one confcutdir cannot span disjoint repo roots, and a shared
  parent would re-admit otto's own conftest for the in-tree repos). JUnit results are written
  per-repo (results stem + repo name) when more than one repo participates; the combined summary
  prints per-repo lines; exit code is the worst of the sessions. The common single-repo case is
  exactly one session — indistinguishable from today's behavior. (The 2026-06-30 spec targeted all
  repos in one session and did not address confcutdir; superseded.)
- Stability (`-i`/`-d`/`--threshold`), `--cov*`, `--monitor*`, `--results` apply unchanged — they
  already live on the parent callback and flow through the shared core.

### 2.4 Per-suite `Options` in selection runs: default-construct, fail loud

Amends the 2026-06-30 spec §5.4 (which ran selector-reached suites with `opts_instance=None`).
`None` makes a test that requests `suite_options` crash confusingly. Instead,
`OttoOptionsPlugin.suite_options` becomes request-aware (class-scoped, resolving
`request.cls`):

- Single-suite path (`otto test <SuiteName> --flags`): the CLI-built instance for that class —
  exact current behavior.
- Selection run reaching a suite with an `Options` class: **default-construct it once per class**.
  If construction fails (required fields), the affected tests fail with an actionable message:
  *"suite `TestX` has required options — run `otto test TestX ...` to pass them"*. Other selected
  tests still run.
- Suites/plain functions without `Options`: `None`, as today (nothing requests it).

### 2.5 Out of scope / deferred

- Tab completion of `--tests` values (cache would need collected test names; when built, the
  completer completes the segment after the last comma — commas are not shell word breaks, so the
  standard prefix-preserving technique works).
- `-k` passthrough (exact names + did-you-mean cover the stated need with better errors).
- Multi-suite lifecycle validation beyond the fixture tests in §4 (ordering guarantees across
  suites sharing hosts are observed, not redesigned, here).

## 3. Error handling

- Selection resolving to zero tests (after did-you-mean passes, e.g. a marker matching nothing):
  clean non-zero exit with a clear message, no traceback (2026-06-30 spec §5.5, unchanged).
- `Options` default-construction failure: per-test failure with the suite-naming hint (§2.4);
  never aborts the whole selection.
- Collection failure during name resolution: surfaced per-repo, loud (Phase 1's fix C carries
  over; a broken repo must not silently blank the selection).

## 4. Testing

TDD throughout; real subprocess/e2e paths over mocks (the 2026-06-30 spec §1c lesson: mocked
collection hid three live bugs).

1. **conftest hierarchy (new):** fixture repo with fixtures defined at repo root, `tests/`, and
   suite-dir levels; a suite test consumes all three. Guards the confcutdir change. A companion
   test asserts otto's own `tests/conftest.py` is still *not* loaded for in-tree repo1 suites.
2. **Auto-registration unit tests:** `Test*` subclass registers (no decorator); non-`Test*` base
   does not; cross-file name collision still loud; `Options` captured; completion cache sees the
   suite.
3. **Decorator deletion sweep:** repo-wide grep gate — zero `register_suite` call sites in
   src/tests/docs (the `SUITES` registry module itself remains).
4. **Selection runs (from the 2026-06-30 spec §5.5, now with amended semantics):** two-suite
   fixture repo sharing a marker — `-m shared` runs both suites, each class's setup/teardown fires
   once; `--tests a,b` (comma form) runs a cross-suite handful; unknown name → did-you-mean error;
   qualified `Class::name` disambiguates; stability and `--cov` function on a selection run.
5. **Options in selection runs:** suite with defaulted `Options` gets defaults; suite with a
   required option fails only its own tests, with the hint message.
6. **Multi-repo:** two fixture repos, one selection spanning both → two sessions, per-repo junit,
   worst exit code.

Gates: `make coverage` per task; full gate (`coverage` + `nox` + `typecheck` + `docs`) before
hand-off; a typecheck round budgeted after src edits (`ty` runs only at nox typecheck). Single
`-n auto` passes only.

## 5. Docs

`docs/guide/test.md` (selection syntax, options rule), `docs/guide/repo-setup.md` (the `tests` key
= discovery scope), `OttoSuite` docstring (decorator-less example — the conftest promise finally
true), `docs/getting-started.md` (rewritten around `otto init`, companion spec).

## 6. Risks & mitigations

- **Multi-suite session semantics** — biggest unknown, inherited from the 2026-06-30 spec §5.3;
  structurally supported (class-scoped lifecycle, plural `sut_test_dirs`) but gated behind the
  two-suite fixture tests before claiming it works.
- **`suite_options` scope change** (session → class) — requesters are unaffected (narrower scope
  satisfies wider requests); covered by existing suite e2e.
- **confcutdir widening admits unexpected user conftests** (e.g. repo1's `sys.path` shim) — that
  is the *feature*; the e2e suite proves the in-tree repos stay green.
- **`__init_subclass__` fires for ad-hoc `Test*` subclasses in otto's own unit tests** — same
  behavior the decorator had when applied; tests that relied on *not* registering must not name
  classes `Test*` (audit during the sweep).

## 7. Delivery

- Stage-only; **no self-commit**. Paste-able commit messages on completion.
- Suggested commits: (1) confcutdir → repo root + conftest tests; (2) auto-registration +
  decorator deletion + sweep; (3) selection runs (`--tests`/`-m`-alone) + options amendment +
  fixture tests. Each independently green.
- Lands **before** the `otto init` spec's implementation (its suite template is decorator-less).
