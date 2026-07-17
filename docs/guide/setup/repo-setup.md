# Repository Setup

Otto discovers your project through a `.otto/settings.toml` file at the
repository root.  This page explains every setting and what happens during
project initialization.

## `otto init --help`

```{raw} html
:file: ../../_static/generated/termynal/help-init.html
```

## The settings file

Create `.otto/settings.toml` in your repo root:

```toml
name = "my_project"
version = "1.0.0"

labs  = ["${sut_dir}/../lab_data"]
libs  = ["${sut_dir}/pylib"]
tests = ["${sut_dir}/tests"]
init  = ["my_instructions", "my_shared_options"]

# Optional: product preferences applied to every host this repo touches.
# Selector = Python regex matched against the host id; ".*" = all hosts.
# Values win over lab.json; CLI --term/--transfer win over everything.
[host_preferences.".*"]
ssh_options = { connect_timeout = 5.0, keepalive_interval = 30 }
```

### Variable expansion

`${sut_dir}` is replaced with the absolute path to the repo root at load
time.  Use it to keep paths relative and portable.  Expansion runs
inside every settings table, including string values nested under
`[host_preferences]`.

### Field reference

name
: **Required.** Product or repository name.  Displayed in CLI panels and log
  output.

version
: **Required.** Semantic version string (e.g. `"1.0.0"`).

labs
: List of directory paths to search for lab JSON files.  When you pass
  `--lab my_lab`, otto looks in these directories for a file matching that
  name.  Defaults to `[]`.

\[lab\]
: Optional table selecting the **host-source backend** — where otto's hosts come
  from. `backend` names a registered source (defaults to `"json"`, which reads
  `lab.json` from the `labs` directories); a `[lab.<name>]` sub-table holds
  that backend's keyword arguments. See {doc}`host-database` for the full
  treatment.

libs
: List of Python package directories to add to `sys.path` at startup.
  This is where you put your instruction modules, shared options, and helper
  libraries.  Defaults to `[]`.

tests
: Defines where test discovery happens: a list of directories scanned for
  `test_*.py` files.  Each matching file is imported at startup, which
  auto-registers any `Test`-prefixed `OttoSuite` subclass as an `otto test`
  subcommand and makes its plain `test_*` functions collectible by pytest.
  Selection runs (`otto test --tests NAME[,NAME...]` or `otto test -m
  EXPRESSION` with no suite name) collect from these same directories, one
  pytest session per repo.  Defaults to `[]`.

init
: List of Python module names (dot-separated) to import at startup.  Use
  this to register instructions (`@command()`) and shared option classes.
  These modules must be importable from one of the `libs` directories.
  Defaults to `[]`.

\[host_preferences\]
: Optional table of product-wide selector-scoped preferences.  Each
  sub-table key is a Python regex matched against host ids; inner keys
  are `term`, `transfer` (ordered backend lists) or `*_options` tables
  (per-key option values that win over `lab.json`).  See
  {ref}`host-preferences` in {doc}`lab-config` for the full schema and
  precedence rules.

  > **Migration note:** `[host_defaults]` was removed; its option
  > tables move under `[host_preferences."<selector>".<opt>]`.

\[os_profiles\]
: Optional table of named OS-profile bundles.  Each `[os_profiles.<name>]`
  sub-table must contain a `base` key naming a registered host class
  (e.g. `"unix"`, `"zephyr"`, or a class registered by an `init` module)
  and may contain any default field values to bundle with that profile.
  Profiles are registered into the global OS-profile registry so lab-data
  entries can select them by `os_type` name.  See {doc}`../hosts/os-profiles` for
  the full treatment.

\[reservations\]
: Optional table enabling the **reservation gate** — otto refuses to start
  live-lab commands against resources the current user doesn't hold. `backend`
  names a registered scheduler source (`"none"` — the default — disables the
  gate; `"json"` reads a reservation file). See {doc}`../reservations` for backends,
  the file format, and the `--as-user` / `-R` break-glass overrides.

## What happens at startup

When you run any `otto` command, the following initialization sequence
occurs:

1. **Environment parsing** -- Otto reads `OTTO_SUT_DIRS` to find repo root
   directories.

2. **Repo discovery** -- For each path in `OTTO_SUT_DIRS`, otto creates a
   `Repo` object and reads its `.otto/settings.toml`.

3. **Apply settings** -- For each repo, otto:
   - Adds `libs` directories to `sys.path`
   - Imports modules listed in `init` (this registers instructions)
   - Auto-imports all `test_*.py` files from `tests` directories (this
     registers suites)

4. **Lab loading** -- Otto builds the host source via `build_lab_repository`
   (selected by `[lab] backend`, defaulting to the built-in `json` source over
   the merged `labs` search paths) and loads the lab(s) named by `--lab` or
   `OTTO_LAB`. Multiple labs are merged, combining their hosts — name them with
   `+` (`--lab lab_a+lab_b`). The host source
   is pluggable — see {doc}`host-database`.

5. **Context creation** -- The global `OttoContext` is created with the
   loaded repos and lab and installed via `set_context()`, making hosts
   available to the zero-argument accessors (`get_host`, `all_hosts`) in
   all commands.

See {doc}`../../architecture/subsystems/bootstrap` for how this sequence
composes multiple repos into one process and contains per-file failures.

## Defining shared options

Most repos want a common set of CLI flags — device type, lab environment, and so
on — on every `otto run` instruction and `otto test` suite. Define them once as a
shared **options class** in any module named in your `init` setting (a `libs`
directory like `pylib/` is a common home, but any importable module works), then
inherit it from each suite and instruction. Options are a first-class part of
project definition: declared here at setup, they thread through instruction
execution and test runs.

Use the `@options` decorator — otto's name for a pydantic dataclass — so the
flags are validated. See {doc}`../run/options` for the full treatment, and
`otto.examples.options` for a copyable example.

`otto init` scaffolds exactly this shape: a `pylib/<name>_options.py` with a
repo-wide `RepoOptions` that the example suite and instruction both inherit.
Listing the options module in `init` — as the hand-written example under
"The settings file" above does with `my_shared_options` — is optional: it
only needs to be importable from a `libs` dir, since suites and instructions
import it directly rather than relying on startup registration. `otto
init`'s scaffold leaves it out of `init` for exactly that reason.

## Multiple repos

Otto supports multiple repos simultaneously.  Set `OTTO_SUT_DIRS` to a
comma-separated list:

```bash
export OTTO_SUT_DIRS=/path/to/repo1,/path/to/repo2
```

Each repo has its own settings, libs, tests, and lab search paths.  They
are all merged at startup -- instructions and suites from every repo appear
in the CLI, and lab search paths from all repos are combined.

## Lab files

Each directory listed under `labs` holds a `lab.json` file describing the
hosts at that location.  The full per-host schema — every field, the
connection-option tables, repo-level host defaults, and how labs merge — lives
in {doc}`lab-config`.

(team-setup-checklist)=
## Team setup checklist

Most of otto's configuration is a **one-time, team-level** decision. New
contributors then just clone and run. Work through this map once when adopting
otto for a team:

1. **Run `otto init`** — scaffolds `.otto/settings.toml` (`name`, `version`, and
   the `labs` / `libs` / `tests` / `init` paths — this page, above) with every
   optional section present but commented out, the generated editor schemas
   (`.otto/schemas/` + `.vscode` wiring, see {doc}`editor-schemas`), an example
   lab host, and a shared `RepoOptions` class inherited by both an example test
   suite and an example instructions module — so `otto test TestExample` and
   `otto run smoke` share a `--message` flag out of the box. `otto init --all`
   scaffolds everything with no prompts; bare `otto init` asks per missing
   area; `otto init --schemas` also *refreshes* the generated schemas after an
   otto upgrade. See {doc}`../../getting-started` and {doc}`../cli-reference`.
2. **Choose a host source** — the built-in `json` source (commit `lab.json`
   under a `labs` directory) is the default; point `[lab] backend` at a CMDB or
   inventory API if you have one. See {doc}`host-database`.
3. **Decide on reservation gating** — leave it off (`backend = "none"`, the
   default) for sandbox labs, or wire `[reservations]` to your scheduler so otto
   refuses to clobber a held rack. Tell the team about the `--as-user` and
   `-R` / `--skip-reservation-check` break-glass overrides *before* they need
   them. See {doc}`../reservations`.
4. **Register shared code** — put instruction/option modules under `libs` and
   list them in `init`; auto-import test suites from `tests`. See {doc}`../run/index` and
   {doc}`../test`.
5. **Set per-product preferences** — optional `[host_preferences]` /
   `[os_profiles]` (this page, above, and {doc}`lab-config` / {doc}`../hosts/os-profiles`).
6. **Enable tab completion** — see {doc}`../../getting-started`.
7. **(Optional) Add TLS to the dashboard** — plain HTTP plus a per-run
   access key is the default and needs no setup; add TLS only if a lab
   needs it. A team owner creates a CA once and distributes trust to
   viewers; each machine that runs `otto monitor` then gets its own leaf
   cert, and the repo's `settings.toml` gains a `[monitor]` table pointing
   at it. See {doc}`../monitor`'s [Securing the
   dashboard](../monitor.md#securing-the-dashboard) section for the steps.

Each backend choice is verifiable: otto ships conformance helpers
(`otto.testing.assert_lab_repository_conforms` /
`assert_reservation_backend_conforms`) so a custom host source or reservation
backend can be checked against otto's contract in your own test suite.
