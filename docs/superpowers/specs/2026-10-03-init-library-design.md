# `otto init`: the doctor and the scaffolder become a library

**Status:** approved in chat section by section (Chris, 2026-10-03).
**Issues:** fixes #497, #498, #499, #500; refs #525 (thin-CLI series, item 6).
**Principle:** `docs/architecture/principles.md`, "Input rules live in the
library entry point". Worked examples: the item 2 spec
(`2026-09-29-run-options-own-their-rules-design.md`), the item 4 spec
(`2026-10-01-cov-verbs-own-their-rules-design.md`) and the item 5 spec
(`2026-10-02-monitor-one-library-design.md`).

## 1. Intent

`otto init` checks a repo's setup (the *doctor*) and writes what is missing
(the *scaffolder*). Both live entirely in `src/otto/cli/init.py` (1224
lines): six areas (settings, schemas, lab, tests, instructions, kmodcov), each
a detect / validate / scaffold triple, plus the lab and inventory warnings and
the inventory label. A Python caller cannot run the doctor or the scaffolder
without going through the CLI, and the CLI copies have drifted from the code
they shadow:

- **#497. A repo can pass `otto init` and then fail to load.** The doctor
  validates settings with `SettingsModel.model_validate(data)` alone (no
  `sut_dir` context), a partial copy of `Repo.parse_settings`. The loader also
  runs `compile_lab_sources` (a json `[[lab.sources]]` entry with no `paths`,
  unknown keys, duplicate labels), compiles the project-scope regex, and
  registers OS profiles, where a typo'd `defaults` key is refused.
  `_lab_file_groups` catches the `compile_lab_sources` error and returns
  `[]`, claiming the settings area reports it. It does not.
- **#498. An orphaned schema file fails, and its remedy never clears it.**
  `_validate_schemas` flags a `*.schema.json` the installed otto no longer
  emits as `orphaned`, with the remedy "re-run `otto init --schemas` or
  `otto schema export`". Neither writer deletes files. `otto schema export`
  also has its own copy of the schema writer.
- **#499. Detect and validate disagree on init modules.** Detection accepts
  only a package directory and validation also accepts `mod.py`, so
  `init = ["foo"]` with `pylib/foo.py` reads as missing and `otto init --all`
  scaffolds a duplicate. Both hand-roll import resolution, so dotted names and
  namespace packages fail in both, while the loader uses `importlib`.
- **#500. A scaffolder overwrites and edits user files.** `_scaffold_tests`
  writes `tests/conftest.py` and `tests/test_example.py` unconditionally, so a
  hand-written conftest is lost. `_scaffold_kmodcov` appends to an existing
  `settings.toml`. `_scaffold_editor_wiring` prints from inside a scaffolder.

The fix moves the doctor and the scaffolder into a library package,
`otto.init`, and moves the pieces two callers share to the code that already
owns them, so the doctor runs the loader's own functions. The CLI keeps the
flags, the prompts and the rendering.

## 2. Rulings (Chris, 2026-10-03)

1. **Approach 1:** a new `otto.init` package holds the area engine; shared
   pieces move to their owners (`otto.config.repo`, `otto.models.jsonschema`,
   `otto.host.os_profile`). Rejected: spreading each area across its
   subsystem (what `otto init` does would be scattered over six packages),
   and moving `cli/init.py` wholesale (keeps the duplication the series
   removes).
2. **Orphaned schemas: the writer prunes.** `.otto/schemas` is otto-owned, so
   refreshing it deletes every `*.schema.json` otto no longer emits; the
   doctor keeps `orphaned` as a failure, and its stated remedy now clears it.
   In any other `--out` directory only orphans carrying otto's
   `x-otto-version` stamp are deleted, so a user's own schema files there are
   never touched.
3. **One write policy; user files are never edited.** Otto-owned files are
   refreshed; user-owned files are created only when absent. `--kmodcov` no
   longer appends to `settings.toml`: it returns a notice carrying the exact
   commented `[[dev_tools]]` snippet. A `settings.toml` otto creates in the
   same run includes the block.
4. **An omitted `init` is legitimate.** `init` omitted or `[]` means the repo
   has no init modules. The doctor reports it without failing, and `--all`
   and interactive runs do not offer to scaffold the instructions area. A
   notice appears only when otto writes an init module nothing will import
   (an explicit `--instructions`, or `--tests` pulling instructions in as its
   prerequisite, while the settings declare no `init`).
5. **`init` stays; `entry_points()` is not used for repos.** otto's `init` is
   `importlib.import_module` over a declared list, wrapped in dependency
   order, per-repo attribution (`registering_repo`) and per-module
   containment. Entry points need installed distribution metadata, which
   #555's `package = false` repos never have; they are environment-wide where
   otto's active set is per invocation; and they are unordered. An
   `otto.plugins` group for installable third-party plugins is filed as
   #587; `libs` possibly becoming redundant for editable repos is noted on
   #555.
6. **The Next steps panel is printed last** (§3.8), with completion guidance
   split into "this shell" and "future shells", the latter split into
   `~/.bashrc` and `~/.profile`.

## 3. Shape

### 3.1 Errors

`otto.init.errors.InitInputError(FieldError)`: an input refusal naming its
field. Its messages carry no CLI flag spelling.

| `field` | Refusal |
|---|---|
| `root` | the repo root is not a directory |
| `kmodcov_dir` | absolute, escapes the root through `..`, or collapses onto the root (`.`, `""`); today's `_kmodcov_dir_path` rule, moved verbatim with its message reworded to name no flag |
| `areas` | an area name not in `AREA_NAMES` |

### 3.2 `otto.host.os_profile`: a pure check

`check_os_profile(name, base, defaults=None, *, login_prompt=None,
password_prompt=None) -> None` raises the `ValueError`s
`register_os_profile` raises today (unknown base class, unknown default
field, invalid prompt regex) and touches no registry.
`register_os_profile` calls it first, then registers; its signature and
behaviour are unchanged.

### 3.3 `otto.config.repo`: settings compile and init-module lookup

- **`compile_settings(data, sut_dir) -> CompiledSettings`.** Pure. Runs
  `SettingsModel.model_validate(data, context={"sut_dir": sut_dir})`,
  `compile_lab_sources`, `ProjectScopeConfig.from_spec`,
  `parse_dependency_entry` over both dependency lists, the products' and
  dev tools' `to_runtime`, the docker and monitor `to_runtime`, and
  `check_os_profile` on every `[os_profiles.*]`. Raises on the first error.
  `CompiledSettings` holds every value `parse_settings` assigns today.
- **`Repo.parse_settings`** becomes: read the TOML, `compile_settings`,
  assign, then register the OS profiles. The global registration is the only
  step left outside the pure function.
- **`validate_settings(root) -> list[str]`.** Reads
  `.otto/settings.toml`, compiles, and returns `[]` or one problem line
  naming the file: the TOML parse error, the pydantic error (rendered with
  `compact_validation_error`, never `str(ValidationError)`), or the compile
  error, prefixed with its section the way `_register_os_profiles` prefixes
  `[os_profiles.<name>]`.
- **`find_init_module(name, libs) -> ModuleSpec | None`.** Resolves *name*
  with `importlib.machinery.PathFinder` over `[*libs, *sys.path]`, the order
  the loader effectively uses (bootstrap appends `libs` to `sys.path`, then
  `importlib.import_module`). A dotted name is walked through each parent
  spec's `submodule_search_locations`. Executes no code. Finds a package, a
  single-file module, a dotted name and a namespace package.

### 3.4 `otto.models.jsonschema`: one writer and one drift check

- **`write_schemas(out, *, builtins_only=False) -> SchemaWrite`.** Writes
  every document `build_schemas(builtins_only=)` returns, then prunes orphans
  (ruling 2): in a directory that is `.otto/schemas` every non-emitted
  `*.schema.json`; elsewhere only non-emitted files whose parsed JSON carries
  `x-otto-version`. Returns `written` and `pruned` path lists.
  `--builtins-only` writes fewer files (no custom host types), so pruning
  removes the custom types' schemas: the writer writes exactly what it was
  asked for, and the docs say so.
- **`schema_drift(out) -> list[str]`.** Today's `_validate_schemas` and
  `_drift_problem`, moved: missing, stale (naming both version stamps),
  unparsable, orphaned, each with the remedy "re-run `otto init --schemas` or
  `otto schema export`".

### 3.5 `otto.init`: the package

A new top-level tach module, lazy like every otto package (`_LAZY_ATTRS` +
`TYPE_CHECKING` + `__all__`). Its docstring separates it from settings'
`init` key (the init modules). Public names:

| Name | What it is |
|---|---|
| `InitConfig(root, name, version, kmodcov_dir="third_party/otto_kmodcov")` | frozen; `__post_init__` refuses per §3.1; keeps `module_base` and `init_module` |
| `InitConfig.for_repo(root, *, name="", version="0.1.0", kmodcov_dir=...)` | today's name default: the existing settings' `name`, else the directory name |
| `AREA_NAMES` | `["settings", "schemas", "lab", "tests", "instructions", "kmodcov"]` |
| `detect_areas(root) -> list[str]` | the areas present |
| `scaffold_candidates(root, *, requested, all_areas) -> list[str]` | §3.7 |
| `scaffold(config, areas) -> ScaffoldReport` | §3.7 |
| `check_repo(root) -> DoctorReport` | §3.6 |
| `InitInputError` | §3.1 |

Modules: `errors.py`, `areas.py` (the area registry: detect / validate /
scaffold functions moved from the CLI), `doctor.py` (lab parsing, lab and
inventory validation and warnings, the kmodcov checks, the per-run inventory
cache), `write_policy.py`, and `templates.py` (today's
`otto.cli.init_templates`, moved: the library writes them).

### 3.6 The doctor: `check_repo(root) -> DoctorReport`

`DoctorReport(verdicts, warnings, inventory_label, creds_label)` with `ok`
(no verdict failed). `AreaVerdict(name, state, problems, detail)`, where
`state` is `ok`, `failed`, `absent` or `blocked`; `problems` is non-empty
only for `failed`, and `detail` is the one-line explanation an `absent` or
`blocked` row shows.

- **settings:** `validate_settings(root)` (§3.3), so the doctor runs the
  loader's own compile (#497).
- **schemas:** `schema_drift(root / ".otto" / "schemas")` (§3.4).
- **lab:** `_lab_file_groups` no longer swallows the compile error. When the
  settings do not compile, the lab verdict is `blocked` with the detail
  "settings did not compile", so the real error is shown once, under
  settings. Otherwise today's checks, moved unchanged: the runtime loader's
  section, entry and duplicate rules per source; host entries resolved
  against the inventory and validated with `validate_host_dict`; links with
  `LinkSpec`; one problem for a broken inventory declaration; problems
  rendered with `compact_validation_error`.
- **tests:** today's light check (dirs exist, `test_*.py` present, `ast.parse`).
- **instructions:** `init` omitted or `[]` gives `absent` with the detail
  "no init modules declared", never `failed` (ruling 4).
  Otherwise every entry must resolve through `find_init_module` (#499); an
  unresolved one is a problem listing the directories searched. Detection is
  "the settings declare `init` and at least one entry resolves".
- **kmodcov:** today's checks, moved.
- **warnings:** the lab warnings, the inventory's stale-snapshot, orphan,
  orphan-creds and creds-mode warnings, and the kmodcov drift warnings, in
  today's order. Never failing.
- **labels:** `inventory_label` and `creds_label` when an inventory resolves
  (today's `_print_inventory_label`), `None` otherwise.

`check_repo` never prints and never raises for a repo's content; it raises
`InitInputError(field="root")` when *root* is not a directory.

### 3.7 The scaffolder

**Write policy (`write_policy.py`).** Every file goes through
`write_file(path, text, owner, *, mode=None) -> FileWrite(path, outcome)`;
`outcome` is `created`, `refreshed` or `kept`.

| Owner | Files | Existing file |
|---|---|---|
| otto | `.otto/schemas/*.schema.json` (plus pruning, §3.4), `.vscode/otto.code-snippets`, the exported kmodcov library | refreshed |
| user | `.otto/settings.toml`; `lab_data/{lab,inventory,creds}.json` (`creds.json` at `0o600`) and `lab_data/README.md`; `tests/{test_example,conftest}.py`; the init module; `.vscode/{settings,extensions}.json`; the kmodcov consumer starter | kept, never edited |

**Selection: `scaffold_candidates(root, *, requested, all_areas)`.** Today's
rules, moved: `all_areas` (and the interactive caller, which passes
`all_areas=True` to get the offer list) yields every missing area; a
requested area is a candidate only when missing, except `schemas` and
`kmodcov`, which refresh when present; `kmodcov` is opt-in (only its own
request); the instructions area is not offered when `init` is omitted or
`[]` unless explicitly requested (ruling 4). Prerequisites are added and
ordered: `settings` before every other area, `instructions` before `tests`.
An unknown area name raises `InitInputError(field="areas")`.

**Execution: `scaffold(config, areas) -> ScaffoldReport`.** Writes each
area's files in `AREA_NAMES` order and returns
`ScaffoldReport(areas, prerequisites, writes, notices)`. It never prints.
Notices:

- `settings.toml is the repo marker`, when settings were added as a
  prerequisite;
- the existing `.vscode/settings.json` was left untouched (the schema
  associations are in `docs/cli/schema/editors.md`);
- the kmodcov `[[dev_tools]]` snippet to paste, when the settings exist and
  declare no kmodcov entry (ruling 3);
- the instructions prerequisite note (the example tests import
  `RepoOptions` from module *M*; *M* must declare
  `@otto.options(verbs=["run", "test"]) class RepoOptions` with a
  `message: str` field), when tests are scaffolded and an instructions area
  already existed;
- the `init = ["<module>"]` line to add, when otto writes an init module the
  settings do not declare (ruling 4).

**Instructions area.** When the settings declare `init` and its first entry
does not resolve, scaffolding writes that module (a dotted name as nested
packages) under the first `libs` directory, not a module named after the
repo. Otherwise it writes `config.init_module` under `pylib/`, as today.

**Settings area.** The template is unchanged, except that it includes the
commented kmodcov `[[dev_tools]]` block when `kmodcov` is scaffolded in the
same call. The conventional `lab_data`, `tests` and `pylib` directories are
still created.

### 3.8 The `otto init` leaf

`init_command`'s flags are unchanged. Its body:

1. `config = InitConfig.for_repo(path, name=, version=, kmodcov_dir=)`.
2. `candidates = scaffold_candidates(config.root, requested=…,
   all_areas=…)` (interactive passes `all_areas=True`).
3. Interactive: prompt for the product name and version when settings are
   missing (rebuilding the config), then confirm each candidate. All prompts
   come before any write. Settings is a prerequisite of every area here too.
4. `report = scaffold(config, chosen)`; print each write (`created`,
   `refreshed`, `kept`) relative to the root, then each notice.
5. `doctor = check_repo(config.root)`; render the verdict table (an
   `absent` area reads "not present", `blocked` reads "blocked"), the
   inventory and creds labels, and the warnings, escaping rich markup as
   today.
6. Print the Next steps panel, the last thing `otto init` prints:

```
╭─ Next steps ─────────────────────────────────────────────────────────────────╮
│                                                                              │
│  1. Activate otto in this shell:                                             │
│                                                                              │
│          export OTTO_SUT_DIRS=/home/me/acme                                  │
│          otto --install-completion                                           │
│          source ~/.bash_completions/otto.sh                                  │
│                                                                              │
│  2. Activate otto in future shells:                                          │
│                                                                              │
│     ~/.bashrc                                                                │
│        `otto --install-completion` already added the `source` line to        │
│        ~/.bashrc, so completion needs nothing more there. Add only:          │
│                                                                              │
│          export OTTO_SUT_DIRS=/home/me/acme                                  │
│                                                                              │
│     ~/.profile  (if your login shell reads it instead of ~/.bashrc)          │
│        Add both lines yourself:                                              │
│                                                                              │
│          export OTTO_SUT_DIRS=/home/me/acme                                  │
│          source ~/.bash_completions/otto.sh                                  │
│                                                                              │
│        Never put `otto --install-completion` itself in a startup file: it    │
│        rewrites ~/.bashrc every time it runs.                                │
│                                                                              │
│  3. Try it:                                                                  │
│                                                                              │
│          otto --lab example_lab --list-hosts                                 │
│          otto test --list-tests                                              │
│          otto --lab example_lab test TestExample                             │
│          otto --lab example_lab test test_example_function                   │
│          otto --lab example_lab run smoke                                    │
│                                                                              │
╰──────────────────────────────────────────────────────────────────────────────╯
```

   Commands are green, the `~/.bashrc` / `~/.profile` sub-headings cyan, the
   warning yellow. When `OTTO_SUT_DIRS` already contains the root, every
   `export` line is omitted: step 1 shows the two completion commands, the
   `~/.bashrc` block says completion "needs nothing more there" with no "Add
   only" line, and the `~/.profile` block lists only the `source` line. The
   facts behind the wording: typer's bash installer writes
   `~/.bash_completions/otto.sh` and appends `source '<that path>'` to
   `~/.bashrc` (once), so only the current shell needs a manual `source`; and
   it rewrites `~/.bashrc` (adding a newline) every time it runs. When a
   command would not fit inside the box at the terminal's width (a long repo
   path), the same content prints unboxed under a "Next steps" rule, each
   command on one line the terminal wraps, so a copied command is never
   folded into a wrong value. The panel is CLI guidance about CLI commands and
   stays in the leaf.
7. Exit 1 when `not doctor.ok`.

One translation site: `except InitInputError as e: raise
usage_error_from(e, flags={"root": "--path", "kmodcov_dir": "--kmodcov-dir",
"areas": "<the area flag>"})`. Every library import is function-local, so
`otto.cli.init`'s import budget does not move.

### 3.9 The `otto schema export` leaf

`export` calls `write_schemas(out, builtins_only=)` and prints each written
file and each pruned one, then the summary line.

### 3.10 Layering

`otto.init` is a new `[[modules]]` entry in `tach.toml`. Its dependencies
are its real imports (expected: `otto.config`, `otto.models`, `otto.labs`,
`otto.inventory`, `otto.host`, `otto.kmodcov`, `otto.utils`, `otto.errors`);
`otto.cli` gains `otto.init`. Never `tach sync`. The `tach.toml` header
paragraph and the measured SCC are updated by hand if the new module joins
it. `otto.inventory.doctor` and `otto.inventory.creds` docstrings that name
`otto.cli.init` are re-pointed.

## 4. Testing

- **Moved by mechanism.** The doctor tests in
  `tests/unit/cli/test_init_validate.py` and the scaffold tests in
  `tests/unit/cli/test_init_scaffold.py` move (`git mv`) to a new
  `tests/unit/init/`, calling `check_repo` / `scaffold` directly rather than
  through `CliRunner`. `test_init_templates.py` follows the templates. The
  prompt tests (`test_init_prompts.py`, now asserting every prompt precedes
  any write) and `test_init_banner.py` stay in `tests/unit/cli/`.
  `tests/unit/models/test_jsonschema_validation.py` imports from `otto.init`.
  Every new test file's basename is unique across `tests/`.
- **New tests, each proven red by a recorded mutation:**
  - #497: a json `[[lab.sources]]` entry with no `paths` fails the settings
    area and the lab verdict is `blocked`; a typo'd `[os_profiles.x]
    defaults` key fails the settings area; neither run registers an OS
    profile (the registry is unchanged afterwards).
  - #498: an orphan in `.otto/schemas` is pruned by `otto init --schemas` and
    by `otto schema export`, after which the doctor's schemas verdict is
    `ok`; in a foreign `--out`, an unstamped `*.schema.json` survives and a
    stamped orphan is pruned.
  - #499: `pylib/foo.py`, a dotted `pkg.sub`, a namespace package and a
    module reachable only through `sys.path` all resolve; `--all` writes no
    duplicate beside `pylib/foo.py`.
  - #500: an existing `tests/conftest.py` and `tests/test_example.py` are
    `kept` byte-identical; `--kmodcov` leaves an existing `settings.toml`
    byte-identical and returns the snippet notice; a fresh settings
    scaffolded with kmodcov contains the block; no scaffold function writes
    to stdout (capsys empty).
  - Ruling 4: with `init` omitted or `[]`, `--all` does not offer
    instructions, the doctor's verdict is `absent` and `ok` holds; the
    `init = [...]` notice appears for an explicit `--instructions` and for
    the `--tests` prerequisite, and not otherwise. A declared but unresolved
    `init = ["foo"]` scaffolds `foo`, not `<repo>_instructions`.
  - Next steps: the panel is the last output; `export` lines appear only
    when the root is not in `OTTO_SUT_DIRS`; both shells' blocks are present.
- **The differential** `tests/unit/cli/test_init_differential.py`, shaped
  like `test_monitor_differential.py`: hand-off rows (each area flag,
  `--all`, `--kmodcov --kmodcov-dir`, `--name` / `--version`, `--path`) hand
  `scaffold_candidates` / `scaffold` / `check_repo` exactly the parsed
  values; refusal rows through the real `InitConfig` (`--kmodcov-dir`
  outside the repo, `--path` not a directory) reach the output as `Invalid
  value for <flag>` with exit 2; a failing `DoctorReport` exits 1; `otto
  schema export` rows for `--out` and `--builtins-only`. The docstring
  records the mutation that turned it red.
- **Invariant suites:** `tests/unit/test_error_base.py` (`InitInputError`;
  the taxonomy counts in `src/otto/errors.py`), `test_lazy_packages.py`,
  `test_import_contracts.py`, `tests/unit/import_budget/` (`otto.cli.init`,
  `otto.cli.schema`), `test_lane_invariants.py`,
  `test_tier_marker_invariants.py`, `tach check`, and the API snapshot
  (`make api-snapshot`).

## 5. Documentation

- `docs/cli/init.md`: the write policy, the instructions semantics, the
  `blocked` verdict, the exit codes, the Next steps panel; completion links
  to the getting-started page rather than restating it.
- `docs/getting-started/index.md`: the single home for completion setup.
  Remove the advice to add `otto --install-completion` to `~/.bashrc`;
  match the panel's this-shell / future-shells split.
- `docs/cli/schema/`: pruning, and `--builtins-only` writing fewer files.
- `docs/architecture/subsystems/bootstrap.md`: the owner becomes
  `otto.init`; "a repo that passes `otto init` loads" is now enforced by
  construction.
- `docs/configuration/settings.md`: an omitted `init` means no init modules.
- `docs/api/init/`: API pages for the new package.

## 6. BREAKING (migration recipe for the squash message)

- `otto.cli.init`'s `AREAS`, `Area`, `InitConfig` and the templates
  (`otto.cli.init_templates`) move to `otto.init`; `InitConfig` gains
  `root` and refuses bad input with `InitInputError`.
- Interactive `otto init` asks every question before writing anything, and
  settings is a prerequisite of every area.
- `otto init --kmodcov` no longer edits `settings.toml`; it prints the
  snippet to paste. `otto init --tests` never overwrites an existing file.
- `otto init --schemas` and `otto schema export` prune orphaned schemas.
- Doctor verdicts change: settings that do not compile fail the settings
  area (the lab area reads `blocked`); an omitted or empty `init` no longer
  counts as a missing instructions area; an init module found on `sys.path`
  passes.
- The Next steps block is the last output, as a panel.

## 7. Out of scope

- The kmodcov scaffold's content (its own series).
- An `otto.plugins` entry-point group (#587).
- `libs` redundancy for editable-installed repos (#555).
- Any other change to `otto init`'s user experience.
