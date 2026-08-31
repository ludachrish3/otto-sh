# Settings file

Otto discovers your project through a `.otto/settings.toml` file at the
repository root.  This page explains every setting and what happens during
project initialization.

```{note}
The `.otto` name is shared by three directories holding three different
kinds of thing.  This page is about the **repo's** `.otto/` — source
config, committed with the code.  The xdir holds run outputs, and
`~/.otto` — otto's user-level home — holds derived state otto can rebuild.
See [The workspace home](../cli/index.md#the-workspace-home).
```

## The settings file

Create `.otto/settings.toml` in your repo root:

```toml
name = "my_project"
version = "1.0.0"

libs  = ["pylib"]
tests = ["tests"]
init  = ["my_instructions", "my_shared_options"]

# Where otto's hosts come from, read in order — later sources override
# earlier ones one element (or one labs table entry) at a time.
[[lab.sources]]
backend = "json"
paths = ["../lab_data"]

# Optional: product preferences applied to every host this repo touches.
# Selector = Python regex matched against the host id; ".*" = all hosts.
# Values win over lab.json; CLI --term/--transfer win over everything.
[host_preferences.".*"]
ssh_options = { connect_timeout = 5.0, keepalive_interval = 30 }
```

### Path resolution

Every path that otto itself interprets is expanded with `~` (your home
directory), and if it is still relative it resolves against **the repo
root** — the directory containing `.otto/`.  Absolute paths are used as
written.  (`ssh_options` values, merged `host_preferences` fields, and the
remote `[coverage] gcda_remote_dir` are opaque to otto — they are handled
by whatever consumes them, not by otto's settings layer.)

```toml
tests    = ["tests"]                # <repo>/tests
libs     = ["../shared/pylib"]      # <repo>/../shared/pylib
tls_cert = "~/.otto/tls/cert.pem"   # [monitor] table: $HOME/.otto/tls/cert.pem
```

This file is committed and shared by everyone working on the repo, so a
path is never interpreted relative to the directory you happen to run
`otto` from.  Use `~` when you deliberately want a per-user location,
such as TLS material.

When several repos are active at once (`OTTO_SUT_DIRS`), each
`settings.toml` resolves against its own repo root — the same text means
the right thing in every repo.

#### Removed: `${sut_dir}`

Earlier versions expanded `${sut_dir}` to the repo root.  It is gone —
a relative path already resolves there, so the prefix was redundant.  It
is no longer special in any way: a settings file still containing it
gets a directory literally named `${sut_dir}`.

Drop the prefix: `"${sut_dir}/tests"` becomes `"tests"`, and
`"${sut_dir}/../shared"` becomes `"../shared"`.

Values otto hands to a backend without interpreting them (a custom
backend's kwargs in a `[[lab.sources]]` entry, `[reservations.<backend>]`,
`ssh_options`) must now be absolute.  Custom lab and reservation backends
both receive `repo_dir` and can anchor their own paths.

One exception worth knowing: `[reservations.json] path` is read by otto
itself, not passed through, so a relative value there still resolves
against the repo root — see {doc}`../cli/reservation/index`.

### Field reference

name
: **Required.** Product or repository name.  Displayed in CLI panels and log
  output.

version
: **Required.** Semantic version string (e.g. `"1.0.0"`).

\[\[lab.sources\]\]
: Ordered array of **host-data source** declarations — where otto's hosts come
  from.  Each entry names a registered `backend` (`"json"` ships with otto), an
  optional `name` labelling it in warnings and errors, and that backend's own
  keys inline — for `json` that is `paths`, a non-empty list of directories
  (searched for `lab.json`), `.json` files, or globs.  Every declared source is
  read, in order, and a later one overrides an earlier one one element (or one
  `labs` table entry) at a time.  A repo may declare none; the `[lab]` table
  holds nothing but `sources`.  See {doc}`host-sources` for the full treatment.

libs
: List of Python package directories to add to `sys.path` at startup.
  This is where you put your instruction modules, shared options, and helper
  libraries.  Defaults to `[]`.

tests
: Defines where test discovery happens, in two different senses.  Every
  `test_*.py` at the **top level** of a listed directory is imported at
  startup, which auto-registers any `Test`-prefixed `OttoSuite` subclass as
  an `otto test` subcommand — that scan is *not* recursive, because these
  files are executed on every otto command (list a subdirectory too if you
  keep suites there).  Selection runs (`otto test --tests NAME[,NAME...]` or
  `otto test -m EXPRESSION` with no suite name) hand the same directories to
  pytest, one session per repo, and pytest recurses as usual — so a plain
  `test_*` function in a subdirectory runs without being imported here.
  Defaults to `[]`.  See {doc}`../cli/test/index` for the reasoning.

init
: List of Python module names (dot-separated) to import at startup.  Use
  this to register instructions (`@instruction()`) and shared option classes.
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

\[project\]
: Optional table declaring this repo's **fleet of interest** — `lab_patterns`
  (the labs it applies to) and `host_patterns` (the hosts it targets inside
  them).  Both are Python regexes matched with `re.fullmatch`; `host_patterns`
  defaults to `[".*"]` and `lab_patterns` has no default.  It bounds every
  fleet walk the repo drives and gates its product/dev-tool providers, and it
  becomes **required** the moment the repo registers one of those providers.
  See {ref}`project-scope` in {doc}`lab-config` for the full schema and
  {doc}`../cli/run/defaults` for what it does to a walk.

\[os_profiles\]
: Optional table of named OS-profile bundles.  Each `[os_profiles.<name>]`
  sub-table must contain a `base` key naming a registered host class
  (e.g. `"unix"`, `"zephyr"`, or a class registered by an `init` module)
  and may contain any default field values to bundle with that profile.
  Profiles are registered into the global OS-profile registry so lab-data
  entries can select them by `os_type` name.  See {doc}`os-profiles` for
  the full treatment.

\[coverage\]
: Optional table configuring gcov collection: `gcda_remote_dir` (required
  once the table exists — where `.gcda` files live on remote hosts), an
  optional `hosts` regex scoping collection to a subset of the lab, the
  `[coverage.tiers.<name>]` tier declarations, `[coverage.tickets]`, and
  `[coverage.exclusions]`. See {doc}`../cli/cov/index` for the schema and
  {doc}`../cli/cov/tiers` for the tier model.

\[reservations\]
: Optional table enabling the **reservation gate** — otto refuses to start
  live-lab commands against resources the current user doesn't hold. `backend`
  names a registered scheduler source (`"none"` — the default — disables the
  gate; `"json"` reads a reservation file). See {doc}`../cli/reservation/index` for backends,
  the file format, and the `--as-user` / `-R` break-glass overrides.

\[inventory\]
: Optional table selecting the **host inventory** — the tool-agnostic layer a
  host entry reads its machine facts from.  Its usual home is the user-level
  file described below; a table here is a per-project **override**, and when
  more than one active repo declares one, the tables must be identical or
  bootstrap fails naming both.  Three keys are otto's own: `backend` (a
  registered name — `"json"` and `"netbox"` ship with otto), `creds_file` (a
  JSON file of credentials keyed by inventory key, read by otto rather than by
  the backend) and `cache_ttl` (`"0"`, or `<n>m` / `<n>h` / `<n>d`; default
  `"24h"`).  Every other key belongs to the selected backend: `path`
  (required) and `supplies` for `json`; `url`, `token_env`, `verify`,
  `filter`, `ip_source`, `custom_fields`, `extra_custom_fields` and `timeout`
  (seconds per request, default `30`) for
  `netbox`.  A relative `path` or `creds_file` anchors to the repo root, like
  every other settings path.  See {doc}`inventory` for the full treatment.

\[logging.levels\]
: Optional table setting the per-library noise floor — which third-party
  loggers' records enter otto's funnel at all.  Otto quiets a few known-noisy
  libraries by default; entries here override or extend that default per
  logger name.  See {ref}`[logging.levels] <logging-levels>` below.

(logging-levels)=

### `[logging.levels]`

otto captures every logger in the process by propagation to the ROOT
logger — nothing needs registering.  `[logging.levels]` is the one knob
that shapes *what enters* that funnel: a logger name mapped to the minimum
level otto admits from it.  Otto ships its own per-library defaults (a few
known-noisy dependencies quieted at `WARNING`); entries here override or
extend them.

```toml
[logging.levels]
asyncssh = "DEBUG"          # unmute one of otto's defaults for an SSH debugging session
noisy_vendor_sdk = "ERROR"  # quiet a library of your own
```

- **Enters vs. shows.** This table decides what enters otto's funnel; the
  sinks decide what shows. `--log-level` still governs the console and
  `console.log`, and `verbose.log`'s own floor — so `asyncssh = "DEBUG"`
  only reaches the console once `--log-level DEBUG` is also given.
- **Defaults are overridable, not exhaustive.** A name not listed here
  inherits root and is captured normally; the table only quiets or
  un-quiets specific loggers. Otto's own defaults are
  {data}`otto.logger.management.DEFAULT_LIBRARY_LEVELS`.
- **Logger names are free-form; levels are not.** An unrecognized logger
  name validates fine — pre-quieting a library before it's even installed
  is legitimate. An unrecognized level name is a validation error.
- **`otto` and `otto.*` are rejected.** otto's own verbosity is
  `--log-level`'s job, not this table's.
- **Multi-repo tables merge.** When more than one active repo sets
  `[logging.levels]`, the tables union; two repos setting the *same*
  logger to *different* levels is a validation error naming both repos and
  the logger. The same level twice is fine.

The `capture` key that used to live under `[logging]` — a per-repo
allowlist this table replaces — is gone: capture is automatic now that
otto configures the root logger, so a `settings.toml` still carrying it
fails validation, naming the file and pointing here.

### User-level settings

One file sits outside every repo: `~/.otto/settings.toml`, otto's **user-level**
settings.  It holds what is true of the person and the machine rather than of a
project — today that is exactly one table, `[inventory]`, because a machine is a
machine regardless of which repo you are working in:

```toml
[inventory]
backend = "json"
path = "~/lab/inventory.json"
creds_file = "~/.otto/creds.json"
```

Otto neither creates nor scaffolds this file; write it by hand when you adopt
an inventory.  It lives under otto's home, so `OTTO_HOME` relocates it along
with everything else there, and a relative path inside it anchors to that
directory (`~/.otto`) rather than to any repo.  A repo-only table pasted into
it is an error naming the key — this file is not a second `settings.toml`.

A project's own `[inventory]` table overrides it, which is the seam for a repo
that has moved to a different inventory ahead of the rest.  See
{doc}`inventory`.

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
   - Auto-imports each `test_*.py` at the top level of a `tests` directory
     (this registers suites; it does not recurse — see above)

4. **Lab loading** -- Otto builds the host source via `build_lab_sources`,
   concatenating every repo's `[[lab.sources]]` entries in `OTTO_SUT_DIRS`
   order, and loads the lab(s) named by `--lab` or `OTTO_LAB`. Every declared
   source is live: a later one overrides an earlier one wholesale per record
   (an element, or a `labs` table entry), with a warning naming both.
   Multiple labs are merged, combining their hosts — name them with `+`
   (`--lab lab_a+lab_b`). The sources are pluggable — see
   {doc}`host-sources`.

5. **Context creation** -- The global `OttoContext` is created with the
   loaded repos and lab and installed via `set_context()`, making hosts
   available to the zero-argument accessors (`get_host`, `all_hosts`) in
   all commands.

See {doc}`../../architecture/subsystems/bootstrap` for how this sequence
composes multiple repos into one process and contains per-file failures.

## Multiple repos

Otto supports multiple repos simultaneously.  Set `OTTO_SUT_DIRS` to a
comma-separated list:

```bash
export OTTO_SUT_DIRS=/path/to/repo1,/path/to/repo2
```

Each repo has its own settings, libs, tests, and host-data sources.  They
are all merged at startup -- instructions and suites from every repo appear
in the CLI, and every repo's `[[lab.sources]]` entries are concatenated in
`OTTO_SUT_DIRS` order.

### Declaring dependencies between repos

When one repo's libs or tests build on another's, declare it in
`.otto/settings.toml`:

```toml
[dependencies]
required = ["vantage >= 2.1, < 3", "common-libs"]
optional = ["metrics >= 1.4"]
```

Entries are a project name (matched against other repos' `name` fields,
case- and punctuation-insensitively) optionally followed by comma-ANDed
version constraints using `==`, `!=`, `>=`, `<=`, `>`, `<`. Versions may be
shortened (`< 3` means `< 3.0.0`). A version's extra tag (`1.2.3-rc1`) is
never compared — `1.2.3-rc1` satisfies `>= 1.2.3` — and constraints may not
carry one.

At startup, otto validates the declarations after discovering every repo in
`OTTO_SUT_DIRS`:

- A **required** dependency that is missing or version-incompatible fails
  that repo loudly (its instructions and tests do not load; other repos are
  unaffected). Repos that require a failed repo are skipped too, with the
  root cause named.
- An **optional** dependency that is absent is fine. Present but
  incompatible prints a startup warning (the feature stays disabled) and
  never blocks commands.
- otto also checks that a compatible version is *possible* at all: if two
  repos' required constraints on the same project can never both hold
  (`>= 2` vs `< 2`), every participant gets an error naming all the
  constraints — no version hunt can fix a contradictory declaration set.
- Registration order follows the dependency graph (a dependency's libs and
  init modules load before its dependents'). Repos with no declared
  dependencies keep `OTTO_SUT_DIRS` order exactly.

Inspect the outcome at runtime via `otto.config.get_repos()` — each repo
carries a `dependencies` list with per-dependency status and the provider's
version.

### `[env]` — which installer builds the orchestration venv

```toml
[env]
backend = "uv"   # or "pip"; omit to auto-detect
```

This repo's standing choice of installer for the environment
{doc}`../cli/env/index` builds. It is a **preference, not a requirement**:
`--backend` on the command line outranks it, because the operator at the
terminal knows things the file does not — that uv is not installed on this
particular host, say. Omit it to auto-detect: uv when it is on `PATH`,
otherwise the standard library's `venv` plus pip.

Because a workspace is several repos, two of them can declare different values.
That is a hard error naming both rather than a silent pick — an installer you
did not choose is not a thing to bind quietly.

### `[dependencies]` names repos; `pyproject.toml` names packages

The two are easy to confuse and never overlap:

| Where | What it declares |
| ----- | ---------------- |
| `[dependencies]` in `.otto/settings.toml` | Other **repos** in `OTTO_SUT_DIRS` this one needs |
| `[project.dependencies]` in `pyproject.toml` | **Python packages** this repo imports |

`otto env` reads the second: it installs each repo from its `pyproject.toml`,
so that is where a Python requirement belongs. A repo with no `pyproject.toml`
declares no Python dependencies to otto at all — it is skipped by `otto env`,
and its `libs` reach `sys.path` at bootstrap exactly as before.

The two are also read at different moments, which is the clearest way to keep
them apart:

| Where | Read by | To decide |
| ----- | ------- | --------- |
| `[dependencies]` | the dependency pass at bootstrap | the ORDER repos load in, and which are skipped |
| `[project.dependencies]` | the preflight, before every ordinary command | whether this environment can satisfy the repo at all |

So a missing *repo* is a load-order problem and a missing *package* is an
environment problem, and each is reported by the thing that owns it — see
{doc}`../cli/env/index`, which also documents what the preflight does not
cover.

(team-setup-checklist)=

## Team setup checklist

Most of otto's configuration is a **one-time, team-level** decision. New
contributors then just clone and run. Work through this map once when adopting
otto for a team:

1. **Run `otto init`** — scaffolds `.otto/settings.toml` (`name`, `version`, a
   json `[[lab.sources]]` entry, and the `libs` / `tests` / `init` paths — this
   page, above) with every optional section present but commented out, the
   generated editor schemas
   (`.otto/schemas/` + `.vscode` wiring, see {doc}`../cli/schema/editors`), an example
   lab host, and a shared `RepoOptions` class inherited by both an example test
   suite and an example instructions module — so `otto test TestExample` and
   `otto run smoke` share a `--message` flag out of the box. `otto init --all`
   scaffolds everything with no prompts; bare `otto init` asks per missing
   area; `otto init --schemas` also *refreshes* the generated schemas after an
   otto upgrade. See {doc}`../../installation` and {doc}`../cli/index`.
2. **Choose your host sources** — `otto init` scaffolds one `[[lab.sources]]`
   entry on the built-in `json` backend (commit `lab.json` under `lab_data/`).
   Add an entry for a CMDB or inventory API if you have one, and mind the
   order: sources are read in order and a later one overrides an earlier one
   one element (or one `labs` table entry) at a time, which is how a repo
   layers its own equipment over a global database. See {doc}`host-sources`.
3. **Decide on reservation gating** — leave it off (`backend = "none"`, the
   default) for sandbox labs, or wire `[reservations]` to your scheduler so otto
   refuses to clobber a held rack. Tell the team about the `--as-user` and
   `-R` / `--skip-reservation-check` break-glass overrides *before* they need
   them. See {doc}`../cli/reservation/index`.
4. **Register shared code** — put instruction/option modules under `libs` and
   list them in `init`; auto-import test suites from `tests`. See {doc}`../cli/run/index` and
   {doc}`../cli/test/index`.
5. **Set per-product preferences** — optional `[host_preferences]` /
   `[os_profiles]` (this page, above, and {doc}`lab-config` / {doc}`os-profiles`).
6. **Enable tab completion** — see {doc}`../../getting-started/index`.
7. **(Optional) Add TLS to the dashboard** — plain HTTP plus a per-run
   access key is the default and needs no setup; add TLS only if a lab
   needs it. A team owner creates a CA once and distributes trust to
   viewers; each machine that runs `otto monitor` then gets its own leaf
   cert, and the repo's `settings.toml` gains a `[monitor]` table pointing
   at it. See {doc}`../cli/monitor/index`'s [Securing the
   dashboard](../cli/monitor/serving.md#serving-the-dashboard) section for the steps.

Each backend choice is verifiable: otto ships conformance helpers
(`otto.testing.assert_lab_repository_conforms` /
`assert_reservation_backend_conforms`) so a custom host source or reservation
backend can be checked against otto's contract in your own test suite.

## Docker images and compose stacks

```toml
[docker]
registry_url = "docker.io"   # optional; default. Non-default registries
                              # get prefixed onto image tags.

[[docker.images]]
name = "api"                              # short logical name
dockerfile = "docker/api.Dockerfile"
context = "docker"

[[docker.images]]
name = "db"
dockerfile = "docker/db.Dockerfile"
context = "docker"
build_args = { VERSION = "1.2.3" }       # optional; influences hash
target = "prod"                          # optional multi-stage target

[[docker.composes]]                      # a pure file inventory
name = "core"                            # referenceable handle; optional,
                                         # defaults to the file stem; unique
path = "docker/compose.yml"
services = ["api", "db"]                 # authoritative: names the container
                                         # hosts otto registers

[[docker.use_cases]]                     # a FRAGMENT; repeatable, and two
name = "integration"                     # fragments sharing a name join one
composes = ["core"]                      # use-case. Handles from above.
role = "edge"                            # placement role, matched against a
                                         # host's `roles` tags
placement = { edge = "test3" }           # optional committed pin; may be
                                         # lab-qualified ("unix:test3")
provides = "edge"                        # optional: candidate provider of a
priority = 10                            # capability, higher priority wins
env = { LOG_LEVEL = "debug", EDGE_ADDR = "${otto:role.edge.addr}" }
pass_env = ["EDGE_TAG"]                  # allowlist copied from your shell
```

`services` names the entries of *this* file's own `services:` block, and it is
authoritative: it is the list otto registers container hosts from, not a hint.
Its cardinality is unrelated to `[[docker.images]]`: one built image can back
several services, and a service may run a published image otto never builds.
(The per-repo `compose_up` primitive additionally compares the declared list
against `docker compose config --services` after bringing the stack up and
warns on drift; the use-case deploy path registers from the declaration
alone.)

Relative paths resolve against the repo root — see
[Path resolution](#path-resolution).

{doc}`../cli/docker/use-cases` is the home for what these keys *mean* — how
fragments compete and are placed, what `${otto:...}` resolves to and why that
syntax is valid only in this file, and how the env channels merge.
{doc}`../cli/docker/index` lists the commands that read this block.
