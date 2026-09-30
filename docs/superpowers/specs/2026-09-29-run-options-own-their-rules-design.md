# Run options own their rules; the coverage report has one owner — design

**Date:** 2026-09-29
**Status:** approved 2026-09-29; amended during planning (seed wording, meta key, report return type)
**Scope:** item 2 of the thin-CLI layering series (audit of 2026-09-28)
**Fixes:** #505, #506, #507

## 1. Intent

otto's design principle: the CLI layer does input validation, tab
completion and lifecycle orchestration, then hands off to a library
(otto's own or a third party) that owns the logic, so a Python caller gets
the same behaviour without the CLI.

`otto test` is the verb the audit found closest to that principle, with
one gap: the rules that turn its coverage and monitor flags into a run
live only in the CLI. The consequences the audit measured:

- `otto.run_tests(RunOptions(cov_report_dir=p))` produces no report;
  `cov_tickets_json=p` alone writes nothing; `cov_dir=p` leaves `cov` in
  auto mode; `monitor_hosts="x.*"` alone does not monitor; `cov=False`
  with a destination is not refused. The four implication rules and the
  contradiction live in `otto.cli.test._prepare_run_options` and the
  parse-time `_check_selection`; `RunOptions` applies none of them. (#505)
- The `--cov-tickets-json` fail-fast for an unconfigured
  `[coverage.tickets]` runs only in the CLI. A script runs every test,
  gets one warning in a log nobody reads, exit 0, and no file. (#505)
- The seed-without-random-order rule exists twice: a parse-time
  `BadParameter` in the CLI and a `ValueError` deep in the pytest-session
  builder.
- The empty-directory gate (`otto.coverage.config.prepare_empty_dir`) is
  called from three places with three flag spellings; on the library path
  it tells the caller to "pass --overwrite-cov_dir", a flag that does not
  exist, and the tier picker says "pass --tier NAME" from library code.
  (#506)
- `otto cov report --dir` writes into a non-empty directory that
  `otto test --cov-report-dir` would refuse, so stale files from a previous
  report survive beside new ones. Nothing owns the gate. (#507)
- The in-run report in `otto.suite.run._post_run_coverage` resolves its
  inputs (coverage repo, tiers, thresholds, ticket spec, overrides,
  exclusion rules) with a block whose comment says it mirrors
  `otto.cli.cov._resolve_cov_settings`. Two copies, one in a library
  module and one in the CLI.

The principle this item establishes for the rest of the series, stated
once here and carried into `docs/architecture/principles.md`:

> Every rule about a verb's inputs lives in the library entry point that
> takes them: in the class's `__post_init__` (or validators) when the
> inputs are an options class, and as leading validation when they are
> parameters. I/O preflights live in a library `prepare_*` function with a
> check-only mode. The CLI parses, completes, constructs, calls and
> translates errors, and holds no rule of its own.

The corollary that keeps this from becoming "a dataclass per verb": the
container follows the existing library API. A bundle that is stored or
passed around with cross-field rules is a class (`RunOptions`, the
`@otto.options` classes); a function with a handful of arguments keeps
keyword arguments and validates first. Type conversion (string to `Path`,
tri-state booleans) stays click's job at parse time.

## 2. What this deletes

| Today | After |
|---|---|
| `otto.cli.test._prepare_run_options` (implications, two directory gates, tickets fail-fast, four `BadParameter`s) | gone; the CLI constructs `RunOptions(**flags)` |
| `_check_selection`'s `--seed` and `--no-cov` branches | gone; `RunOptions.__post_init__` raises, translated at parse time |
| the seed `ValueError` in `otto.suite.run._pytest_session` | gone; same rule in `__post_init__` |
| `otto.suite.run._pre_run_cov_dir_check` | gone; `prepare_run` |
| the `prepare_empty_dir` call inside `_post_run_coverage` | gone; `run_coverage_report(overwrite=)` |
| `otto.coverage.config.check_empty_dir` / `prepare_empty_dir` (flag-named `ValueError`) | `check_destination` / `prepare_destination` raising `DestinationError` |
| `otto.cli.cov._resolve_cov_settings` and the mirrored block in `_post_run_coverage` | `otto.coverage.resolve_report_inputs` |
| `report.__cli_output_dir__ = False` on `otto cov report` | gone; `report` gets the per-invocation output directory like every verb |
| `otto cov report --dir` default `./cov_report` | default `<output_dir>/cov_report`; new `--overwrite-dir` |
| "pass --tier NAME" in `otto.coverage.tiers` | "choose one with tier=NAME"; the CLI substitutes the flag |

## 3. `RunOptions` invariants

`RunOptions` stays a frozen dataclass in `otto.suite.run` with the same
fields and defaults. It gains a `__post_init__` applying four rules, in
this order, and nothing else:

1. **Report destinations imply a report.** `cov_report_dir` or
   `cov_tickets_json` set ⇒ `cov_report = True`.
2. **Any coverage destination implies coverage.** `cov_dir` set, or
   `cov_report` true after rule 1 ⇒ `cov = True` when `cov` is `None`.
   When `cov` is explicitly `False` and any of `cov_dir`, `cov_report`,
   `cov_report_dir`, `cov_tickets_json` is set, raise
   `OptionsValidationError("cov=False cannot be combined with cov_dir,
   cov_report, cov_report_dir or cov_tickets_json, which all imply
   coverage")`.
3. **Monitor destinations imply monitoring.** `monitor_output` or
   `monitor_hosts` set ⇒ `monitor = True`.
4. **A seed needs random order.** `seed` set with `random_order=False`
   raises `OptionsValidationError("seed cannot be combined with
   random_order=False: a seed with nothing to seed")`, named after what
   the caller wrote so the CLI spelling reads "--seed cannot be combined
   with --no-random".

Pure by contract: no I/O, no repo config. The only import is
`otto.params.OptionsValidationError`, taken inside the hook so
`import otto.suite.run` costs what it costs today. Assignments go through
`object.__setattr__` because the class is frozen. `dataclasses.replace`
re-runs the hook, so a copy cannot lose an invariant; the one place that
matters is `resolve_coverage`, which must never produce
`replace(opts, cov=False)` for an instance with a destination. It does not
today (a destination forces `cov=True`; a forced-on request resolves on or
raises `CoverageConfigError` / `EmptySelectionError` /
`CoverageNotInstrumentedError`), and a test pins it.

Not a class rule, deliberately: the names-or-markers requirement (names
are an argument of `run_tests`, which already raises; the CLI keeps its
parse-time `UsageError` for exit 2), the `[coverage.tickets]` fail-fast
and the directory gates (§4), and any new rule such as `overwrite_cov_dir`
without `cov_dir`.

Each field's docstring states the implication it takes part in; the CLI
flag help says the same in one line ("implies --cov-report") and the
`otto test` reference page links to the class rather than restating.

## 4. `prepare_run`: the I/O preflight

One new public function in `otto.suite.run`:

```python
def prepare_run(opts: RunOptions, *, dry_run: bool = False) -> None:
    """Refuse a run that cannot save its files, before any host is touched."""
```

In order:

1. **`cov_dir` gate.** When set: `check_destination` under `dry_run`,
   else `prepare_destination`, with `overwrite=opts.overwrite_cov_dir`,
   `field="cov_dir"`, `remedy_field="overwrite_cov_dir"`.
2. **`cov_report_dir` gate.** Same shape with `overwrite_cov_report_dir`.
3. **`[coverage.tickets]` fail-fast.** When `cov_tickets_json` is set and
   `load_ticket_spec(get_cov_config(get_repos()))` is `None`, raise
   `OptionsValidationError("cov_tickets_json requires [coverage.tickets]
   to be configured")`. The "git walk matched no commits" case stays a
   post-run warning: it is not knowable before the run.

Every import inside the function is lazy, as the checks it replaces were.
The default destinations `<output_dir>/cov` and `<output_dir>/cov_report`
are not gated: `output_dir` was created moments earlier by the CLI
preamble or `resolve_output_dir`, and that creation is the writability
check.

`run_tests` calls `prepare_run(opts)` after `bind_verb_options` and
before `resolve_coverage`, so the failure order for every caller is: bad
option value → bad destination or config → instrumentation → hosts. The
CLI leaf calls `prepare_run(opts, dry_run=True)` on the `--dry-run` path
before printing the preview and never reaches `run_tests`; on the real
path it calls nothing, `run_tests` does.

## 5. The destination gate and `DestinationError`

`otto.coverage.config` replaces `check_empty_dir` / `prepare_empty_dir`
with a two-mode gate whose contract is "this path can receive the run's
files":

```python
def check_destination(path: Path, *, overwrite: bool, field: str, remedy_field: str) -> None
def prepare_destination(path: Path, *, overwrite: bool, field: str, remedy_field: str) -> None
```

- **Prepare mode** (a real run): refuse a path that exists and is not a
  directory; create it (`mkdir(parents=True, exist_ok=True)`); if it is
  non-empty, refuse unless `overwrite`, in which case clear it as today
  (symlinked directories are unlinked, not walked); then prove
  writability by creating and removing a probe file inside it. The probe
  is the only check honest on NFS and under ACLs.
- **Check mode** (`--dry-run`): touch nothing. An existing path must be a
  directory, empty or `overwrite`, and writable and searchable by this
  user (`os.access`); a missing path's nearest existing ancestor must be
  writable and searchable. Best effort by design, and the docstring says
  so; the real run's probe is the authority.

Both raise `DestinationError(OttoError, ValueError)`, defined beside the
gate, with attributes `path`, `field`, `remedy_field` and `kind` (one of
`not_a_directory`, `not_writable`, `not_empty`). Messages read in library
terms:

- `cov_report_dir target /x is not empty; set overwrite_cov_report_dir=True to clear it.`
- `cov_dir target /x cannot be written: Permission denied.`
- `output_dir target /x is not a directory.`

The error joins the named-error taxonomy (`otto.errors` docstring counts
and the `CASES` table).

## 6. Library errors and their CLI spelling

Two library errors reach the CLI from this work: `OptionsValidationError`
(a rule between fields, or the tickets configuration) and
`DestinationError` (a path). Both name fields. `otto.cli.invoke.usage_error_from`
stays the one translation site and gains an optional spelling map:

```python
def usage_error_from(
    exc: "OptionsValidationError | DestinationError", *, flags: "Mapping[str, str] | None" = None
) -> typer.BadParameter
```

With `flags` (field name → flag, e.g. `{"cov_dir": "--cov-dir",
"overwrite_cov_dir": "--overwrite-cov-dir", "cov": "--no-cov"}`), the
message's field names are replaced by their flags and the remedy phrase
"set X=True" becomes "pass --x"; `param_hint` is the flag of `exc.field`
when the error carries one. `otto.cli.test` builds the map once from the
`_run_flags` signature (`_flag_name`), so a renamed flag cannot drift from
the message. The user sees exactly today's wording:
`--cov-report-dir target /x is not empty; pass --overwrite-cov-report-dir to clear it.`
Without `flags` the message is passed through unchanged, as item 1 left it.

`otto.coverage.tiers` says "choose one with tier=NAME"; `otto cov` passes
`flags={"tier": "--tier"}` and the user sees "pass --tier NAME" as before.

## 7. One owner for the coverage report

### 7a. `resolve_report_inputs`

```python
@dataclasses.dataclass(frozen=True)
class ReportInputs:
    repo_root: Path | None
    tier_configs: list[TierConfig] | None
    exclusion_rules: list[ExclusionRule]
    thresholds: Thresholds | None
    ticket_spec: TicketSpec | None
    overrides: OverrideConfig | None

def resolve_report_inputs(repos: list[Repo]) -> ReportInputs
```

in `otto.coverage` (a new module `otto.coverage.report_inputs`, exported
lazily from the package), moving the body of `otto.cli.cov._resolve_cov_settings`
verbatim: first repo with a `[coverage]` table via `get_cov_repo`, the
git-less fallback (`None, None, [], None, None, None`) when none is
configured, the same `CoverageConfigError` / `OverrideConfigError`
propagation (both `ValueError`s) for malformed rules or override files.
`_post_run_coverage`'s mirrored block is deleted; both callers call this.

### 7b. `run_coverage_report` owns its destination

```python
async def run_coverage_report(
    cov_dirs: list[Path],
    output_dir: Path,
    inputs: ReportInputs,
    *,
    project_name: str = "Coverage Report",
    tier_specs: list[TierSpec] | None = None,
    overwrite: bool = False,
) -> CoverageStore | None
```

Its first action is `prepare_destination(output_dir, overwrite=overwrite,
field="output_dir", remedy_field="overwrite")`. The six loose keyword
arguments it takes today collapse into `inputs`. The renderer's
`exist_ok` / `dirs_exist_ok` stay: the gate has decided by then.

### 7c. Callers

- `run_tests`'s in-run report: `collect_coverage`, then
  `resolve_report_inputs(repos)`, then `run_coverage_report(..., overwrite=opts.overwrite_cov_report_dir)`.
  For an explicit `cov_report_dir` the directory was prepared by
  `prepare_run`, so the gate passes; the default is inside the fresh
  `output_dir`. The never-fail-a-successful-run swallow around the report
  stays exactly as it is.
- `otto cov report`: loses `__cli_output_dir__ = False`, so the preamble
  creates the invocation's output directory like every other verb.
  `--dir` defaults to `None`, resolved to `<output_dir>/cov_report`; an
  explicit `--dir` is gated with the new `--overwrite-dir` (default off).
  The command is a short sequence of library calls: resolve the
  directory, `resolve_report_inputs`, `run_coverage_report`, print the
  path, translate errors with `usage_error_from(e, flags={"output_dir":
  "--dir", "overwrite": "--overwrite-dir", "tier": "--tier"})`.
- Tests that render into a reused directory pass `overwrite=True`; the
  plan enumerates them from a grep.

Overwrite is off by default on every path: `RunOptions.overwrite_cov_dir`
and `overwrite_cov_report_dir`, `run_coverage_report(overwrite=False)`,
`--overwrite-dir`, `--overwrite-cov-dir`, `--overwrite-cov-report-dir`.

## 8. The CLI after this item

`otto test`:

- **Parse time** (`_OttoTestCommand.parse_args` → `_check_selection`):
  keeps the names-or-`-m` `UsageError`; then constructs
  `RunOptions(**run fields)` inside `try` and translates
  `OptionsValidationError` with `usage_error_from(e, flags=RUN_FLAGS)`.
  That is where `--no-cov` and `--seed` contradictions exit 2 before any
  lab loads. The instance is stored in `ctx.meta[RUN_OPTIONS_KEY]`
  (`otto.suite.run`'s existing, so far unused, `"otto_test_run_options"`),
  so the leaf never rebuilds it. The
  `RunOptions` import stays function-local so `otto test --help` keeps its
  import budget.
- **Leaf** (`_run`): reads the instance from `ctx.meta`; binds the verb
  options; under `--dry-run` calls `prepare_run(opts, dry_run=True)` and
  prints the preview; otherwise calls `run_tests`. Both branches translate
  `OptionsValidationError` and `DestinationError` through
  `usage_error_from(e, flags=RUN_FLAGS)`. Nothing else.

`otto cov report` is §7c. `otto cov get` and `clean` are untouched (item 4).

## 9. Testing

All unit, no bed.

- **`RunOptions`** (`tests/unit/suite/test_run_options.py`, new): one
  table, a row per rule (input fields → effective fields or error text);
  frozen; `dataclasses.replace` re-applies the rules; the `resolve_coverage`
  invariant (a destination is never resolved to `cov=False`; patch the
  instrumentation scan and assert).
- **`prepare_run`** (`tests/unit/suite/test_prepare_run.py`, new): each
  gate in both modes (non-empty refuses with the field-named message;
  `overwrite_*` clears; `dry_run` touches nothing; an unwritable target
  is refused, using a `chmod 0o500` directory and skipped as root); the
  tickets fail-fast with and without a `[coverage.tickets]` table; and the
  ordering test that a bad `cov_report_dir` is refused before the
  instrumentation scan is reached (patch `detect_for_lab`, assert not
  called).
- **Gate and `DestinationError`** (`tests/unit/cov/test_config.py`,
  extended): every `kind` in both modes, attributes, the probe (a
  directory whose entries are readable but which is not writable), and a
  `CASES` row in `tests/unit/test_error_base.py`.
- **`resolve_report_inputs` and `run_coverage_report(overwrite=)`**
  (`tests/unit/cov/test_report_inputs.py`, new; existing reporter tests
  extended): with and without a `[coverage]` table; the gate refuses a
  non-empty `output_dir` unless `overwrite`; the git-less fallback (every
  field `None`, empty rules) is what a tree without `[coverage]` gets.
- **CLI parity** (`tests/unit/cli/test_test_command.py`,
  `tests/unit/cli/test_test.py`): the same rule table drives
  `otto test <flags> --dry-run`: a contradiction row exits 2 with the
  rule's message in flag spelling; every other row's `ctx.meta` instance
  equals `RunOptions(**fields)`. The leaf passes parsed flags through
  unchanged (no rule in the CLI: a test asserting the constructor kwargs
  are the parsed params). `--dry-run` with a non-empty `--cov-report-dir`
  exits 2 and leaves it untouched; the real path with the same flags
  exits 2 before any collection (patch `run_tests`'s session entry).
  `usage_error_from`'s spelling map. `otto cov report`: default
  `<output_dir>/cov_report`, refuses a non-empty `--dir`, honors
  `--overwrite-dir`, and the opt-out marker test flips.
- **Migrated, not deleted:** the existing tests that pin
  `--overwrite-cov_dir` wording, `_prepare_run_options`,
  `_pre_run_cov_dir_check`, `_check_selection`'s seed branch and the
  `cov report` opt-out. The plan enumerates them from a grep.

## 10. Documentation

- `docs/architecture/principles.md`: the rule from §1 as a principle with
  the class-versus-parameters corollary and the `prepare_*` convention.
- `docs/cookbook/python-library.md`: "Preflight" replaces the `cov_dir`
  overwrite-guard subsection and describes `prepare_run` as what
  `run_tests` runs first and what a script can call before a long run; the
  `RunOptions` paragraph says the class is the CLI's options: what it
  implies for one it implies for the other.
- `docs/cli/test/index.md` and `docs/cli/cov/during-tests.md` state each
  implication once, linking the field docstrings; `docs/cli/cov/report.md`
  documents the new default directory and `--overwrite-dir`, and drops the
  "leaves no trace" rationale.
- The coverage subsystem's "where the code lives" names
  `resolve_report_inputs`, the destination gate and `DestinationError`;
  the execution subsystem's names `prepare_run`.
- `make api-snapshot`: `otto.suite.run.prepare_run`,
  `otto.coverage.resolve_report_inputs`, `ReportInputs`,
  `DestinationError` as the documented import lines pick them up.

## 11. Gates, boundaries, budgets

- `make docs` with `-W` clean; `make check-api-snapshot`.
- tach: no new direction. `otto.suite` → `otto.coverage` stays lazy and
  already declared; `otto.suite` → `otto.params` is function-scope inside
  `__post_init__`; `otto.cli.test` and `otto.cli.cov` reach down only.
  `otto.coverage.report_inputs` depends on `otto.config` and the coverage
  submodules it moves the imports of. Never `tach sync`.
- Import budgets are measured, never regenerated: `import otto.suite.run`
  and `otto test --help` must not move.
- ast-grep: the existing `typer-exit-outside-cli` rule (which bans
  `typer.BadParameter` outside `otto.cli`) is the guard that
  `otto.coverage` and `otto.suite` stay typer-free in error paths.
- Full gate once, at the end, bed coordinated with peer sessions first.

## 12. Migration

No compatibility, per the repo rule. `check_empty_dir` / `prepare_empty_dir`
are replaced, not aliased. `otto cov report` creates an output directory
and its `--dir` default changes, so the squash carries `!`. Scripts that
read `./cov_report` by habit pass `--dir ./cov_report --overwrite-dir` or
read the printed path.

## 13. Out of scope

- `otto cov get` / `clean` library entry points (item 4).
- Options metadata leaving typer (item 8, #513).
- The monitor and TLS behaviour of `--monitor` (item 5, #503, #504).
- The one-walk restructuring of `run_project_instruction` noted in item 1's
  hand-back.
