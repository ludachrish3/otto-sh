# The coverage verbs own their rules: one public entry point per verb

**Status:** approved in chat section by section (Chris, 2026-10-01).
**Issues:** fixes #536; refs #525 (thin-CLI series, item 4).
**Principle:** `docs/architecture/principles.md`, "Input rules live in the
library entry point". Worked examples: the item 2 spec
(`2026-09-29-run-options-own-their-rules-design.md`) and the item 3 spec
(`2026-09-30-docker-verbs-align-with-docker-design.md`).

## 1. Intent

`otto cov get` and `otto cov clean` are whole pipelines in
`src/otto/cli/cov.py`, not thin leaves. A Python caller of the library gets
different behaviour from the CLI:

- **`get`.** These rules live only in the CLI's `_do_get`:
  - the manual-kind tier requires a ticket
  - the "nothing is instrumented" refusal
  - the git preflight before any fetch
  - output-directory resolution
  - tester identity resolution
  - the write of manual captures into the repo's committed store
  - the "no captures produced" refusal
  - the `--clean` scoped to the hosts actually fetched

  A caller of `collect_coverage` with a manual tier gets none of them, and
  its captures never reach the committed store. The tier error is spelled by
  rewriting text (`spell_flags(str(e), {"tier=NAME": "--tier NAME"})`),
  which is the pattern item 2 replaced with field-named errors.
- **`clean`** has two owners:
  - the library's `clean_remote_gcda`, which `otto test --cov --cov-clean`
    uses and which returns silently when nothing is configured or matched
  - the CLI's `_do_clean`, which refuses in those cases

  In both, a failed per-host reset is only logged, so `otto cov clean` exits
  0 when every host failed (the #494 defect class). The CLI's embedded-board
  guard (`_unix_only_pattern`) is dead: the library's per-host clean already
  skips embedded boards and the runner.
- **Embedded boards are never reset.** An LLEXT extension's counters live in
  its own memory and only restart when the extension is unloaded and
  reloaded, or the board reboots. Every dump in between includes everything
  since the last load. embedded-gcov already provides `__gcov_clear()` for
  exactly this.
- **`report`.** The leaf keeps a residual `is_dir` check on the run
  directories and the `--tier` spec rules (missing path, duplicate name).

This item gives every coverage verb one public library entry point. The CLI
leaf parses, calls one function, renders its report, and translates
field-named errors at the one `usage_error_from` site.

**Rulings (Chris, 2026-10-01):**

1. Each verb gets its own public entry point: `get_coverage` (new, composes
   the unchanged `collect_coverage` engine), `clean_coverage` (replaces
   `clean_remote_gcda`), `run_coverage_report` (absorbs the leaf's rules).
2. ANY inability to clear counters is a failure, never a warning, in every
   caller. That includes `otto test --cov`'s clean after collection, which
   fails a run whose tests all passed.
3. `otto test --cov --cov-clean` with no `[coverage]` section refuses (today
   it silently skips the clean).
4. Embedded boards get a real counter reset in this item, through a
   `reset_fn` that wraps embedded-gcov's `__gcov_clear()`.
5. The product kind `llext` is renamed `embedded` in this item (§3.5): it
   is already format-agnostic, and only one of its loaders is LLEXT.

## 2. What this deletes

From `src/otto/cli/cov.py`:

- `_do_get`, `_do_clean` and `_connect_cov_hosts`
- `_CovError`, `_GetError` and `_CleanError`
- `_unix_only_pattern`, the dead embedded guard
- `_resolve_tester` and `_capture_annotations`, which move into the library
  behind `get_coverage`
- the `report` leaf's `is_dir` loop, and the rules half of `_parse_tier_specs`
  (the NAME[=PATH] split stays: it is syntax)

From the library:

- `otto.coverage.collect.clean_remote_gcda`, renamed `clean_coverage` with a
  new return type (no alias: the project carries no backwards compatibility)
- `GcdaFetcher.clean_remote` and the module's `_clean_one_host`; clearing has
  exactly one implementation (§3.2)

## 3. Library entry points

### 3.1 `get_coverage`

A new module `otto.coverage.get`, exported lazily from `otto.coverage`:

```python
async def get_coverage(
    output_dir: Path | None = None,
    *,
    tier: str | None = None,
    ticket: str | None = None,
    note: str | None = None,
    tester_name: str | None = None,
    tester_email: str | None = None,
    clean: bool = False,
    repos: list[Repo] | None = None,
) -> GetReport
```

It runs in this order, so every cheap refusal comes before any host is
touched:

1. **Configuration.** No `[coverage]` section, or a malformed `hosts`
   selector, raises `CoverageConfigError` (the existing type). An empty host
   selection raises the existing `EmptySelectionError`.
2. **Tier.** `resolve_get_tier` raises `CoverageInputError(field="tier")`
   for an unknown or ambiguous name. The message no longer contains the
   literal `tier=NAME`; it says "name one of: a, b", so nothing is rewritten.
3. **Ticket.** A manual-kind tier with no ticket raises
   `CoverageInputError(field="ticket")`: `tier 'manual' is a manual-kind
   tier and requires a ticket`.
4. **Instrumentation.** `decide_coverage(True, detect(hosts), ...)` raises
   the existing `CoverageNotInstrumentedError` with its per-product verdicts.
5. **Repository preflight.** `head_commit(sut_dir)` raises the existing
   `GitUnavailableError` before the fetch.
6. **Destination.** `output_dir=None` uses the current context's
   per-invocation output directory. With neither, it raises
   `CoverageInputError(field="output_dir")`. The destination is
   `<output_dir>/cov`.
7. **Collection.** `collect_coverage(cov_dir, ..., tier=<resolved TierConfig>,
   clean_after_fetch=False)` (the engine is otherwise unchanged). No capture
   produced raises the existing `NoCoverageDataError`, naming every
   host:product searched.
8. **Manual store.** For a manual-kind tier, each capture is copied into the
   repo's committed store (`write_manual_capture`). Tester identity is
   resolved here (name defaults to the user, email to `git config
   user.email` in the SUT repo) and annotates manual-kind captures only.
9. **Clean.** With `clean=True`, `clean_coverage(repos, host_ids=<hosts that
   contributed a product>)` runs and its report is returned in `GetReport`.
   A failed reset does not raise here: the captures are already written, and
   the caller sees `report.ok is False`.

### 3.2 `clean_coverage`

In `otto.coverage.collect`, exported lazily from `otto.coverage`:

```python
async def clean_coverage(
    repos: list[Repo] | None = None,
    *,
    host_ids: list[str] | None = None,
) -> CleanReport
```

- **Hosts.** Every host the `[coverage].hosts` selector matches, containers
  included, except the runner itself (`LocalHost`). Embedded boards are
  walked like every other host. `host_ids` narrows the set (used by
  `get_coverage` and by `collect_coverage`'s post-fetch clean).
- **Products.** Every instrumented product on each host has its own
  `Product.reset_coverage(host)` called:
  - Unix and container products delete their `.gcda` files under their
    `cov_dir` (the existing default).
  - Kernel-module products write their sysfs reset (the existing override).
  - Embedded products call `reset_fn` (§3.4).
- **Concurrency.** Hosts are reset concurrently through
  `do_for_all_hosts`; products on one host run in order.
- **Refusals.**
  - No `[coverage]` section raises `CoverageConfigError`.
  - No coverage host matched at all (none but the runner) raises
    `NoCoverageHostsError` (a new `ValueError` subclass in
    `otto.coverage.errors`), with today's `_CleanError` text.
- **Failures.** A failed reset is a result in the report, never a log line.
  An exception raised by a host's reset (a dropped connection) is captured
  as a failed result for every product on that host. A product name that is
  not a safe path segment refuses before any command, as today.
- **Dry run.** A declined reset (`Status.NotRun`) is recorded as not run and
  counts neither as failed nor as cleared.
- **A missing `cov_dir` has nothing to clear** (amended after the final
  review, 2026-10-02; the Unix twin of §3.4's not-loaded rule). gcov creates
  the directory only when the product first exits, and the default
  `/tmp/<name>` is gone after a reboot. Both find-based resets (the default and
  the elevated one `kmod` and `docker_image` share) run one line,
  `! test -d <cov_dir> || find <cov_dir> -name '*.gcda' -type f -delete`, so
  an absent directory is a successful reset and a present one keeps `find`'s
  exit status: a delete that fails is still a failure. The elevated reset runs
  that line inside `sh -c`, because sudo would otherwise elevate only the
  first word.

`collect_coverage(clean_after_fetch=True)` (the `otto test --cov` path) calls
`clean_coverage(repos, host_ids=<hosts that contributed a product>)`, so
embedded boards collected over the console are now reset after collection
too, and returns the report in `CollectResult.clean`.

### 3.3 `run_coverage_report`

Its leading validation absorbs the leaf's rules, as field-named
`CoverageInputError`s raised before anything is read:

- `field="cov_dirs"`: a given coverage directory does not exist (`no
  coverage directory at <path>`).
- `field="tier_specs"`: a tier other than `system` with no path, or a
  duplicate tier name. The messages keep today's wording without the
  `--tier` flag text in them.

The CLI keeps splitting `--tier NAME[=PATH]` into pairs (syntax) and joining
`<run dir>/cov` (the run-directory layout the argument names).

### 3.4 The embedded kind's counter reset

`EmbeddedProduct` (§3.5) gains `reset_fn: str = "cov_reset"`, parsed and
validated exactly like `dump_fn` (non-empty string; listed in the kind's
valid parameter names). `reset_coverage(host)` is overridden to run the
host loader's call command for `reset_fn` (`llext call_fn <product>
<reset_fn>` on the `llext-hex` loader). On failure the result's message
names the fix:

```text
cov_ext: reset_fn 'cov_reset' failed: <loader output>. An extension resets its
counters by exporting `void cov_reset(void) { __gcov_clear(); }` (built with
GCOV_OPT_PROVIDE_CLEAR_COUNTERS); set reset_fn to use another name.
```

The exact loader output for an unexported symbol is pinned from the real
board in the end-to-end test, not guessed.

**Amended after the bed run (2026-10-01).** The real boards forced two rules:

- **A reset counts only when the board confirms it.** Zephyr's shell reports
  `llext call_fn` on an unknown function as a silent success (no output,
  retval 0). The only honest signal is embedded-gcov's own `gcov_clear` status
  line, printed by `__gcov_clear()` under `GCOV_OPT_PRINT_STATUS` (on by
  default). A successful call without it is a failed reset whose message names
  both causes: `reset_fn` is not exported, or status printing is disabled.
- **An extension that is not loaded has nothing to clear.** Its counters only
  exist while it is loaded, and a fresh load starts at zero. `otto test --cov`
  clears before the suite loads the extension, and the board answers
  `No such extension <name>`. The loader recognizes that exact line through a
  `BinaryLoader` hook, and the reset is a successful no-op. Any other error
  stays a failure.

Verifying a reset by dumping afterwards was rejected: it is slow, and the gcov
runtime compiled into the same unit counts its own functions, so the dump is
never all zeros.

### 3.5 The `embedded` product kind

The kind declared as `kind = "llext"` is renamed `kind = "embedded"`. It
was already format-agnostic in everything but its name:

- Everything it does to a board goes through the host's pluggable
  `BinaryLoader`; Zephyr's `llext-hex` is only the first registered loader,
  and a project can register its own for another RTOS.
- Its one requirement is a host with a loader; its own refusal already says
  "only embedded hosts with a `loader` can carry it".
- The dump it serves is embedded-gcov's console hexdump, and the new reset
  is embedded-gcov's `__gcov_clear()`; neither is Zephyr-specific.

The rename:

| Before | After |
| --- | --- |
| `kind = "llext"` | `kind = "embedded"` |
| `otto.host.llext_kind` | `otto.host.embedded_kind` |
| `LlextProduct`, `_llext_kind` | `EmbeddedProduct`, `_embedded_kind` |
| refusals saying "kind 'llext'" | "kind 'embedded'" |

- `kind = "llext"` is refused at settings load with a message naming the
  new kind (`kind 'llext' is now 'embedded'`), not silently accepted: the
  project carries no backwards compatibility.
- "LLEXT" and "llext" stay wherever they name Zephyr's extension format,
  the `llext-hex` loader or its shell commands (`llext load_hex`, `llext
  call_fn`). Only the product kind's name changes.
- Every in-tree declaration and reference moves: `tests/repo3/.otto/settings.toml`,
  the repo3 suite and the embedded end-to-end test, the kind's unit tests,
  the built-in-kinds registry guard, the collector's comments, the
  declared-products reference, the embedded instrumenting page, and the API
  page (`docs/api/host/llext_kind.rst` becomes `embedded_kind.rst`).

## 4. Reports

A new module `otto.coverage.reports`; every type is frozen and lazily
exported from `otto.coverage`.

```python
@dataclass(frozen=True)
class FailedReset:
    host: str
    product: str
    result: Result

@dataclass(frozen=True)
class CleanReport:
    hosts: dict[str, dict[str, Result]]   # host id -> product name -> its reset
    @property
    def failed(self) -> list[FailedReset]: ...   # in host, then product, order
    @property
    def ok(self) -> bool: ...                     # no failed resets

@dataclass(frozen=True)
class GetReport:
    cov_dir: Path
    tier: str
    captures: list[Path]           # never empty: no capture is a refusal
    manual_captures: list[Path]    # copies in the committed store (manual tiers)
    clean: CleanReport | None      # present when clean=True
    @property
    def ok(self) -> bool: ...      # clean is None or clean.ok
```

`CleanReport` deliberately does not reuse item 3's `HostReport` ("host →
list of commands"): the failure message must name the product, which that
shape cannot.

`CollectResult` gains `clean: CleanReport | None`.

## 5. Errors and their CLI spelling

- **`FieldError`**, a new small base in `otto.errors`: an `OttoError` with a
  `field: str | None` attribute, for an input refusal whose message may embed
  user text and must pass through byte-identical. `DockerBuildError` is
  rebased onto it.
- **`CoverageInputError(FieldError, ValueError)`** in
  `otto.coverage.errors`, with fields `tier`, `ticket`, `output_dir`
  (`get_coverage`) and `cov_dirs`, `tier_specs` (`run_coverage_report`).
- **`CoverageCleanError(OttoError, RuntimeError)`** in
  `otto.coverage.errors`, carrying the `CleanReport`; its message names
  every failed host, product and reason.
- **`usage_error_from`**: the `DockerBuildError` arm becomes a `FieldError`
  arm: the message passes through untouched and `param_hint` names the
  flag. The next item adds no branch.

CLI flag maps:

| Verb | Flags |
| --- | --- |
| `get` | `{"tier": "--tier", "ticket": "--ticket", "output_dir": "--output"}` |
| `report` | `{"cov_dirs": "OUTPUT_DIRS", "tier_specs": "--tier"}` plus the existing destination map |

Every other library refusal (`CoverageConfigError`, `EmptySelectionError`,
`CoverageNotInstrumentedError`, `GitUnavailableError`,
`NoCoverageDataError`, `NoCoverageHostsError`, the typed data errors) keeps
printing its message cleanly with exit 1, as today.

## 6. Callers

### 6.1 The CLI

- **`otto cov get`** calls `get_coverage` with exactly its parsed flags.
  - It prints one line per capture and a summary line
    (`Coverage captured: N product(s) -> <cov_dir>`).
  - On a failed clean it prints each failed reset and exits 1.
  - The not-instrumented refusal keeps rendering its verdict table.
- **`otto cov clean`** calls `clean_coverage()`.
  - It prints one line per host and product, `test1/myapp: counters
    cleared`, or the failure through `print_error`.
  - It exits 1 when the report is not ok.
  - A declined reset under `--dry-run` is printed as not run.
- **`otto cov report`** calls `run_coverage_report` with the parsed pairs and
  joined directories; its new refusals exit 2 through the translation site.

### 6.2 `otto test`, in `otto.suite.run`

- **Before the run.** `--cov --cov-clean` calls `clean_coverage(repos)`. A
  report that is not ok raises `CoverageCleanError` before any test session
  starts; a missing `[coverage]` section refuses.
- **After the run.** `--cov` keeps swallowing *collection* failures as
  warnings (the existing "never fail a successful run over collection"
  policy). When `CollectResult.clean` is not ok, it raises
  `CoverageCleanError` outside that block, so the run fails even when every
  test passed. The captures stay written.

## 7. Dry run

- `get_coverage` keeps today's behaviour: the fetch, dump and capture steps
  are declined by the host layer and `collect_coverage`'s refusals apply.
- `clean_coverage` records each declined reset as not run; a dry-run `otto
  cov clean` exits 0 and prints what it would have reset.

## 8. Testing

- **`get_coverage`.**
  - One test per refusal in §3.1's order, each proving no host was touched
    before it fired.
  - The field name on each `CoverageInputError`.
  - Manual-tier captures land in the committed store.
  - `clean=True` resets only the hosts actually fetched.
  - A failed clean still returns the written captures with `ok` false.
- **`clean_coverage`.**
  - Per-product results across Unix, container, kernel-module and embedded
    products.
  - Both refusals.
  - A host whose reset raises becomes failed results.
  - Declined resets under dry run.
  - `host_ids` narrowing.
  - The `CoverageCleanError` message names every failed host and product.
- **`otto test`.**
  - A failed pre-clean raises before any test session starts.
  - A failed clean after collection fails a run whose tests all passed.
  - A collection failure still only warns.
  - `--cov-clean` with no `[coverage]` section refuses.
- **The embedded reset.** It succeeds, it fails with the fix in the
  message, it is declined under dry run, and `reset_fn` is parsed and
  validated like `dump_fn`.
- **The kind rename.** `kind = "embedded"` builds an `EmbeddedProduct`;
  `kind = "llext"` refuses naming the new kind; the built-in-kinds registry
  guard lists `embedded`. A grep-based check confirms no `kind = "llext"`,
  `llext_kind` or `LlextProduct` survives outside `docs/superpowers/`.
- **The differential.** A new `tests/unit/cli/test_cov_differential.py`,
  shaped like `tests/unit/cli/test_test_differential.py`, with rows for
  `get`, `clean` and `report`:
  - every leaf hands its library function exactly the parsed flags
  - every `CoverageInputError` reaches the user in flag spelling, with the
    message byte-identical
  - the module docstring records the mutation that actually turned it red
- **End to end on the Zephyr bed.** In the embedded coverage test:
  1. exercise the product
  2. run `otto cov clean`
  3. dump the counters
  4. assert every product function's counters went to zero (the gcov runtime
     in the same unit counts itself, so not every counter can be zero)

  The test also pins the loader's output for an unexported `reset_fn` and for
  an extension that is not loaded.
- **The demo product.** `tests/repo3/product/src/cov_ext.c` exports
  `cov_reset` and the build defines `GCOV_OPT_PROVIDE_CLEAR_COUNTERS`.

## 9. Documentation

- `docs/cli/cov/get.md`, `docs/cli/cov/clean.md`, `docs/cli/cov/report.md`:
  the library entry point each verb calls, the new refusals, exit codes.
  `clean.md` loses its "embedded boards are not cleaned" paragraph.
- `docs/cli/cov/during-tests.md`: `--cov-clean` refuses without
  `[coverage]`; any failure to clear fails the run.
- `docs/cli/cov/instrumenting/embedded.md`: `reset_fn`, the one-line
  `cov_reset` export and the build define, and the `kind = "embedded"`
  declaration.
- `docs/configuration/declared-products-tools.md`: the `reset_fn` parameter
  and the kind's new name, with a sentence on why it is not tied to LLEXT.
- `docs/cookbook/python-library.md`: a coverage section (get, clean, report
  as library calls).
- `docs/api/coverage/`: pages for the new `get` and `reports` modules.

## 10. Gates

Per task: the touched tests plus `ruff check` and `ruff format --check` on
changed files. Once, on the final tree before hand-back: `make typecheck`,
`nox -s tests_hostless-3.14`, `make gate-fresh`, `make coverage` (Unix and
Zephyr beds, coordinated with busy peer sessions first). Import-budget
ceilings are never regenerated; new modules import their heavy dependencies
inside functions.

## 11. Migration

Breaking, one squash, `feat(coverage)!`:

- `otto.coverage.clean_remote_gcda` is `otto.coverage.clean_coverage` and
  returns a `CleanReport`.
- `GcdaFetcher.clean_remote` is gone.
- `otto test --cov --cov-clean` refuses without a `[coverage]` section.
- Any failure to clear counters fails the command, including a passing
  `otto test --cov` run.
- An embedded product that does not export `cov_reset` now fails `otto cov
  clean`, where it used to be skipped; the message gives the one-line fix.
- The product kind `llext` is `embedded`: change `kind = "llext"` to
  `kind = "embedded"` in `[[products]]`; `otto.host.llext_kind.LlextProduct`
  is `otto.host.embedded_kind.EmbeddedProduct`.
- `otto cov report`'s missing-directory and bad-`--tier` refusals are exit-2
  usage errors.

## 12. Out of scope

- The coverage report's rendering behaviour.
- The kmodcov export and check verbs (their own series).
- A collection failure after a passing `otto test --cov` run keeps only
  warning; changing that policy is not part of this item.
