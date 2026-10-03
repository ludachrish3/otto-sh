# otto init

`otto init` scaffolds a new otto repo — or validates and fills in an existing
one. It is the first command you run in a fresh checkout, and the one you run
again after upgrading otto to refresh the generated editor schemas.

```{raw} html
:file: ../_static/generated/termynal/help-init.html
```

## Synopsis

```text
otto init [--all | --schemas | --lab | --tests | --instructions] [--name NAME]
          [--version X.Y.Z] [--path DIR] [--kmodcov [--kmodcov-dir DIR]]
```

`otto init` is **lab-free**: it needs no `--lab` and no `OTTO_SUT_DIRS`, and
it never creates an output directory.

| Option | Default | Description |
| ------ | ------- | ----------- |
| `--all` | `False` | Scaffold every missing area without prompting |
| `--schemas` | `False` | Scaffold (or refresh, if present) the schemas area: `.otto/schemas` + editor wiring + generated snippets |
| `--lab` | `False` | Scaffold the lab area (`lab_data/lab.json` + `inventory.json` + `creds.json` + README) |
| `--tests` | `False` | Scaffold the tests area (example tests + conftest), plus the instructions area when it is missing; never overwrites an existing file |
| `--instructions` | `False` | Scaffold the instructions area (the init module; `pylib/<name>_instructions/` in a fresh repo) |
| `--kmodcov` | `False` | Scaffold (or refresh) the kmodcov area: vendor the `otto_kmodcov` library at `--kmodcov-dir` (default `third_party/otto_kmodcov`), write a consumer starter beside it, and print the `[[dev_tools]]` snippet to paste. Never scaffolded by `--all` or the prompts |
| `--kmodcov-dir DIR` | `third_party/otto_kmodcov` | Where `--kmodcov` vendors the library (repo-relative) |
| `--name NAME` | directory name | Product name for `settings.toml` |
| `--version X.Y.Z` | `0.1.0` | Product version for `settings.toml` |
| `--path DIR` | current dir | Repo root to operate on (must already exist) |

With no flags, `otto init` runs interactively: it asks every question first
(which missing areas to scaffold, and `--name`/`--version` only when
`.otto/settings.toml` itself is missing) and writes nothing until the last
answer is in. `--all` scaffolds every missing area with no prompts. Passing
one or more of `--lab`/`--tests`/`--instructions` scaffolds those areas when
missing, plus their prerequisites (below). `settings` is a prerequisite of every other area, so it is scaffolded
whenever any other area is, and `otto init` says so. `--kmodcov` is
**opt-in**: it is never scaffolded by `--all` and never offered by the
interactive prompt, only by its own flag.

## What gets written, and what is never touched

Every file goes through one write policy. Otto-owned files are regenerated
from the installed otto, so they are refreshed; user-owned files are created
once and never edited again.

| Owner | Files | When the file exists |
| ----- | ----- | -------------------- |
| otto | `.otto/schemas/*.schema.json`, `.vscode/otto.code-snippets`, the vendored `otto_kmodcov` library | refreshed |
| you | `.otto/settings.toml`; `lab_data/{lab,inventory,creds}.json` and `lab_data/README.md`; `tests/test_example.py` and `tests/conftest.py`; the init module; `.vscode/settings.json` and `.vscode/extensions.json`; the kmodcov consumer starter | kept, byte for byte |

Each file in the run is listed as `created`, `refreshed` or `kept`; refreshing
`.otto/schemas` also lists each orphaned `*.schema.json` it deleted as
`pruned` — see {doc}`schema/export` for exactly what is pruned. So `--tests`
never overwrites an example test you edited, and nothing `otto init` does ever
edits a file you own.

## Verdicts and exit codes

An area that already exists is validated with the same ingestion code otto
uses elsewhere, and does not modify it, except that an explicit
`--schemas` or `--kmodcov` refreshes (and, for schemas, prunes) the otto-owned
files. The summary table gives each area one status:

| Status | Meaning |
| ------ | ------- |
| `✓` | present and loadable |
| `✗` | present, and something the loader would refuse (the detail says what) |
| `scaffolded` | written in this run, and the result validates |
| `not present` | nothing there; the detail says what is missing |
| `blocked` | cannot be judged yet: `lab` when the settings do not compile, `instructions` when the settings are not valid TOML or `init` is not a list of strings — the settings row already shows that error once |

| Exit code | Meaning |
| --------- | ------- |
| `0` | no area failed |
| `1` | at least one area is `✗` |
| `2` | a bad flag value: `--path` is not a directory, or `--kmodcov-dir` is not strictly inside the repo (`.` and an empty value are refused too) |

The name used for areas scaffolded on a later run is read from the existing
`settings.toml`'s `name` field, falling back to the directory name.

## What it scaffolds: tests and instructions

The init module is the Python module `settings.toml`'s `init` key names (see
{doc}`../configuration/settings`). How `otto init` treats it:

- **`init` omitted or `[]` means the repo has no init modules.** That is
  legitimate: the instructions area reads `not present`, and `--all` does not
  offer it. Asking for it explicitly (`--instructions`, or `--tests`, whose
  example tests import `RepoOptions` from it) writes the module under the
  first `libs` directory (`pylib/` when `libs` is empty) and prints the
  `init = [...]` line to set in `settings.toml`, plus the `libs` line when
  `libs` is empty; `otto init` never edits the file for you.
- **Every declared `init` entry must resolve, and every `libs` directory must exist**, or the area is `✗` with one
  problem per unresolved module, because the loader would stop on the missing
  import. Scaffolding runs only when no entry resolves, and writes the first
  entry under the first `libs` directory (a dotted name as nested packages). A
  module that already resolves is never overwritten or shadowed; it is
  reported `kept`.
- **Resolution is importlib's.** A single-file module, a dotted name, a
  namespace package and a module reachable only through `sys.path` all count.
- **A settings file written from the template brings its init module.** The
  template declares `init = ["<name>_instructions"]`, so writing it also writes
  that module, and every repo `otto init` creates loads — including
  `otto init --lab` on a fresh checkout, which writes
  `pylib/<name>_instructions/` too.

The files:

- **`pylib/<name>_instructions/__init__.py`** (in a fresh repo) declares `RepoOptions` with
  `@otto.options(verbs=["run", "test"])`, which puts its `--message` flag on
  `otto test` and on every `otto run` command, and a `smoke` instruction that
  reads it.
- **`tests/test_example.py`** holds the test class `TestExample` and the test
  function `test_example_function`. The tests import `RepoOptions` from the
  init module and read it with `ctx.options(RepoOptions)`. When the repo
  already has an init module, they import from the first one `init` names, and
  `otto init` prints what that module must declare for them to run:
  `@otto.options(verbs=["run", "test"]) class RepoOptions` with a
  `message: str` field.
- **`tests/conftest.py`** holds a fixture every test under `tests/` can use.

{doc}`../cookbook/authoring/options-classes` covers options classes, and
{doc}`../cookbook/authoring/writing-tests` covers tests.

## What it scaffolds: lab files

Each directory a json source's `paths` names holds a `lab.json` file
describing the equipment at that location (a `paths` entry may also name a
`.json` file directly, or a glob).  The full schema — the `labs` table, the
element entry, every host field, the connection-option tables and the link
entry — lives in {doc}`../configuration/lab-config`.

Everything `otto init` writes is read back by the same loader every other
command uses — see {doc}`../configuration/settings` for the settings schema and
what happens to it at startup, and {doc}`../configuration/lab-config` for the
`lab.json` schema.

## The lab doctor

Validating an existing lab area is more than checking that a `lab.json` is
there.  `otto init` runs every file the repo's json sources name through the
loader's own parsers — the section shape, the `labs` table, the element
entries, each host entry against its host spec, and the links.  It applies
the in-source duplicate rules **per source** too, so a lab declared twice
within one source, or one element name repeated (compared by slug) across two
of that source's files, is reported here exactly as it would fail at load.
Anything it finds is a *problem*: it lands in the summary table and the run
exits 1. Settings that do not compile fail the settings row and leave the lab
area `blocked`.

Some findings are advisory instead, printed in a yellow `Warnings` block that
never changes the exit code. Two are about lab shape:

- A **dead membership pattern** — `element 'x' labs pattern 'p' matches no
  declared lab`.  A shared lab file may legitimately serve projects that
  declare different labs, so this is advice, not breakage.
- **Two labs that share an element but declare disjoint resources**, when both
  declare at least one *and* no element- or host-level resource protects the
  shared element.  Reserving either would not contend with the other; the
  warning names the labs, the unprotected elements, and three remedies —
  declare a shared lab identifier, give the element (or each of its hosts) a
  `resources` entry, or make one lab a sub-lab of the other.  (A lab that
  reserves nothing is never half of such a pair.)

When an inventory is configured, its own doctor findings join the same
`Warnings` block — see the
[Warnings](../configuration/inventory.md#warnings) section of the inventory
guide.

Alongside the schemas, `otto init --schemas` writes
`.vscode/otto.code-snippets` — generated `lab.json` skeletons for a `labs`
entry, an element, a cred, and each registered host type.  See
{doc}`schema/editors`.

## kmodcov

`otto init --kmodcov` vendors the `otto_kmodcov` kernel-module coverage library
into the repo (`--kmodcov-dir`, default `third_party/otto_kmodcov`) and writes
a consumer starter beside it — two sentinel translation units
(`kmodcov_begin.c`, `kmodcov_end.c`), a `Kbuild.example` fragment, and a
`README.md`. Re-running `--kmodcov` refreshes the vendored library itself
(otto-owned) and keeps every starter file you already have.

It never edits `settings.toml`. When the settings declare no `kmodcov` dev tool
(commented or not), it prints the `[[dev_tools]]` snippet (kind `kmodcov`, `source` naming
that directory) for you to paste; a `settings.toml` created in the same run
already includes it, commented.

Validating an existing kmodcov area checks every declared `[[dev_tools]]` entry
of kind `kmodcov`: a `source` naming a directory with no library there at all
is a *problem* (the run exits 1), while a vendored copy that differs from the
installed otto's library is only a *warning* — advisory, like the lab
findings above, and naming the differing files and the `otto cov kmodcov
export` remedy.

See {doc}`cov/instrumenting/kernel-modules` for how the library attaches to a
module and how otto loads/removes it around a run.

For a full first-repo walkthrough, see {doc}`../getting-started/index`, then
{doc}`../getting-started/running-instructions` and
{doc}`../getting-started/running-tests`; for the
one-time team decisions around it, the
{ref}`team-setup-checklist <team-setup-checklist>`.

## Next steps

The last thing `otto init` prints is a "Next steps" panel: the
`export OTTO_SUT_DIRS=...` line (omitted when the repo is already listed
there), the tab-completion commands, and the commands to try first
(`otto --lab example_lab --list-hosts`, `otto test --list-tests`, and the
example test and instruction runs). When a command would not fit inside the
box at your terminal's width (a long repo path), the same steps print
unboxed under a "Next steps" rule, so every command stays on one line you
can copy. The panel repeats the completion
commands; what they do and where to put them lives in
{ref}`enabling-tab-completion`.

## As a library

The doctor and the scaffolder are the importable package `otto.init`
({func}`otto.init.doctor.check_repo`, {func}`otto.init.scaffolder.scaffold`): they return
reports and never print, so `otto init` is a thin renderer over them — see
{doc}`../api/init/index`.

```python
from pathlib import Path
from otto.init import InitConfig, check_repo, scaffold, scaffold_candidates

config = InitConfig.for_repo(Path("."), name="acme")
report = scaffold(config, scaffold_candidates(config.root, all_areas=True))
assert check_repo(config.root).ok
```
