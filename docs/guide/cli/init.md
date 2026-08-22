# otto init

`otto init` scaffolds a new otto repo — or validates and fills in an existing
one. It is the first command you run in a fresh checkout, and the one you run
again after upgrading otto to refresh the generated editor schemas.

```{raw} html
:file: ../../_static/generated/termynal/help-init.html
```

## Synopsis

```text
otto init [--all | --schemas | --lab | --tests | --instructions] [--name NAME]
          [--version X.Y.Z] [--path DIR]
```

`otto init` is **lab-free**: it needs no `--lab` and no `OTTO_SUT_DIRS`, and
it never creates an output directory.

| Option | Default | Description |
| ------ | ------- | ----------- |
| `--all` | `False` | Scaffold every missing area without prompting |
| `--schemas` | `False` | Scaffold (or refresh, if present) the schemas area: `.otto/schemas` + editor wiring |
| `--lab` | `False` | Scaffold the lab area (`lab_data/lab.json` + README) |
| `--tests` | `False` | Scaffold the tests area (example suite + conftest) |
| `--instructions` | `False` | Scaffold the instructions area (`pylib/<name>_instructions/`) |
| `--name NAME` | directory name | Product name for `settings.toml` |
| `--version X.Y.Z` | `0.1.0` | Product version for `settings.toml` |
| `--path DIR` | current dir | Repo root to operate on (must already exist) |

With no flags, `otto init` runs interactively: it prompts to confirm each
missing area (prompting for `--name`/`--version` only when
`.otto/settings.toml` itself is missing). `--all` scaffolds every missing
area with no prompts. Passing one or more of `--lab`/`--tests`/
`--instructions` scaffolds exactly those areas, plus `settings` automatically
whenever it's missing — every other area depends on it.

Areas that already exist are never modified — except the otto-owned schemas
area, which `otto init --schemas` refreshes (e.g. after upgrading otto).
Instead, `otto init` validates them with the same ingestion code otto uses
elsewhere and reports each one `✓` or `✗` in a summary table; the command
exits with code 1 if any existing area fails validation. The name used for areas scaffolded on a later run is
read from the existing `settings.toml`'s `name` field, falling back to the
directory name.

Every run also prints a "Next steps" list of the commands to run next —
`export OTTO_SUT_DIRS=...` (skipped if the repo is already listed there),
`otto --install-completion`, `otto --lab example_lab --list-hosts`,
`otto test --list-suites`, `otto test TestExample`, `otto test --tests
test_example_function`, and `otto run smoke`.

## What it scaffolds: lab files

Each directory a json source's `paths` names holds a `lab.json` file
describing the hosts at that location (a `paths` entry may also name a `.json`
file directly).  The full per-host schema — every field, the
connection-option tables, repo-level host defaults, and how labs merge — lives
in {doc}`../configuration/lab-config`.

Everything `otto init` writes is read back by the same loader every other
command uses — see {doc}`../configuration/settings` for the settings schema and
what happens to it at startup, and {doc}`../configuration/lab-config` for the
per-host schema behind `lab.json`.

For a full first-repo walkthrough, see {doc}`../../getting-started`; for the
one-time team decisions around it, the
{ref}`team-setup-checklist <team-setup-checklist>`.
