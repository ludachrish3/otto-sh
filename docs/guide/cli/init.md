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
| `--schemas` | `False` | Scaffold (or refresh, if present) the schemas area: `.otto/schemas` + editor wiring + generated snippets |
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
`otto --install-completion` **and** `source ~/.bash_completions/otto.sh`
(the first writes the completion script, the second activates it in the shell
you are in — neither does the other's job), `otto --lab example_lab
--list-hosts`, `otto test --list-suites`, and — with the lab the printed
lines leave out, and which `otto test` and `otto run` require — `otto --lab
example_lab test TestExample`, `otto --lab example_lab test --tests
test_example_function`, and `otto --lab example_lab run smoke`.

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
entries, each host entry against its host spec, and the links.  There is no
second validator to drift from the first.  It applies the in-source duplicate
rules **per source** too, so a lab declared twice within one source, or one
element `(name, id)` carried by two of that source's files, is reported here
exactly as it would fail at load.  Anything it finds is a *problem*: it lands
in the summary table and the run exits 1.

Two findings are advisory instead, printed in a yellow `Warnings` block that
never changes the exit code:

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

Alongside the schemas, `otto init --schemas` writes
`.vscode/otto.code-snippets` — generated `lab.json` skeletons for a `labs`
entry, an element, a cred, and each registered host type.  See
{doc}`schema/editors`.

For a full first-repo walkthrough, see {doc}`../../getting-started/index`; for the
one-time team decisions around it, the
{ref}`team-setup-checklist <team-setup-checklist>`.
