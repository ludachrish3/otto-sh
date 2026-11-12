# otto env

`otto env` builds and maintains the **orchestration environment** — the one
virtualenv a multi-project run happens in.

Otto is a single process on a single interpreter. When several projects are active
at once (`OTTOPATH`), their glue code all imports into *that* interpreter,
so each project's own virtualenv never participates at runtime. The orchestration
environment is the one that must satisfy all of them at once.

```{raw} html
:file: ../../_static/generated/termynal/help-env.html
```

## Synopsis

```text
otto env create [--force] [-- INSTALLER_ARGS...]
otto env sync   [-- INSTALLER_ARGS...]
otto env show
```

| Subcommand | Description |
| ---------- | ----------- |
| [`create`](create.md) | Build the environment from scratch; refuses an existing one unless `--force` |
| [`sync`](sync.md) | Bring it up to date, building it if absent. Never destroys anything |
| [`show`](show.md) | Report where it is, how it was built, and what is in it |

Both `create` and `sync` are **lab-free** — they need no `--lab` — and they act
on the **discovered** project set rather than the active one: an environment
belongs to a workspace, so the labs a command loads do not change what goes
into it (see {doc}`../projects`).

## The three environments

There are three, and only the second is otto's business:

1. **A project's own venv** — for single-project development. Each project manages it
   with its own tools from its own `pyproject.toml`. otto does not touch it.
2. **The orchestration venv** — where multi-project runs happen. One per user
   per workspace, and it must be a **superset**: it satisfies the glue imports
   of every active project at once. This is what `otto env` builds.
3. **Anything else otto happens to run from** — legal for single-project work,
   discouraged for multi-project work.

If you already use a [uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/)
across your member projects, you have the same thing by another route: one
lockfile, one environment, no otto involvement needed. `otto env` exists for
the common case where the projects are independent checkouts that were never
designed to share a lockfile.

## Where it lives

Under the workspace home — `~/.otto/<hash8>-<slug>/env` — so one environment
serves every invocation against the same projects, from wherever you run them.
See [The workspace home](../index.md#the-workspace-home) for the layout and for
what `OTTO_HOME` moves.

```console
$ otto env create
created ~/.otto/134b91c0-project1-project4/env
  installed (editable): project4
  skipped, no pyproject.toml: project1
  last sync:   otto %OTTO_VERSION%, uv 0.13.6

Activate it with:
  source ~/.otto/134b91c0-project1-project4/env/bin/activate
```

(env-what-goes-in)=

## What goes in

- **Every discovered project that has a `pyproject.toml`**, in one of two ways.
  Which way is uv's own rule, so a project means the same thing here as to
  `uv sync` run inside it: `[tool.uv] package` decides when it is set, and
  otherwise a project is a package exactly when it declares a `[build-system]`.
  - **A package** is built and installed *editable*, so your live checkout
    stays live.
  - **A virtual project** is never built. uv reads its `pyproject.toml` as a
    requirements file, so its `[project] dependencies` go in, each resolved
    through the project's own `[tool.uv.sources]` (a path, editable or git
    source that `uv add` wrote). Its own code is not installed and needs no
    install: its `libs` reach `sys.path` at bootstrap. This is the layout the
    [recommended setup](../../installation.md#with-uv-recommended) writes, so
    that setup needs no build configuration in a workspace either. Its console
    scripts (`[project.scripts]`) are not installed, since only a build
    installs them; declare a `[build-system]` if you need them. Its
    `[dependency-groups]` (`uv add --dev`) are not installed either: they are
    the project's own development tools, not what its glue imports.
- **Projects with nothing to install are skipped**, with a notice naming each
  and why: `no pyproject.toml`, or a virtual project's `no [project] table`.
  That is not an error: their `libs` ride `sys.path` at bootstrap. A workspace
  made entirely of such projects still gets an environment, because otto
  itself goes into it.
- **Two virtual-project declarations are refused**
  (`otto.env.EnvUninstallableProjectError`). Reading a requirements file, uv
  would otherwise build the project or install it on the wrong Python:
  - `dynamic = ["dependencies"]` or `dynamic = ["optional-dependencies"]`.
    uv can only compute either by building the project, which a virtual
    project never gets. If the project is virtual because of
    `[tool.uv] package = false`, drop that (and declare a `[build-system]` if it
    has none); if it simply has no `[build-system]`, declare one. Or list the
    fields statically in `[project]`. This is refused before anything is removed or
    built, so an existing environment, even under `create --force`, is left
    standing.
  - a `requires-python` that the environment's Python does not satisfy. uv
    checks it when it installs a package, but not when it reads a requirements
    file, so otto checks it instead, comparing the interpreter's release
    (`3.15.0` on a `3.15.0a5`, as pip does). It needs the new venv's
    interpreter, so it runs after the venv is created and before any project
    is installed: like an installer failure on `create`, it leaves that new,
    empty venv behind, and with `--force` the old one is already gone. uv
    picks the environment's Python when it creates it;
    `UV_PYTHON=<version> otto env create --force` rebuilds it on another.
- **otto**, at whatever version you are running — and *the way* you are running
  it. If your otto is an editable install from a checkout, the environment gets
  that same checkout; if it came from a wheel, the environment pins that
  version.

(env-dependency-preflight)=

## The dependency preflight

You do not have to remember to run `sync`. Before any ordinary command does its
work, otto checks each project's declared Python requirements against the
interpreter it is running on, and stops if an active project's are unsatisfied:

```console
$ otto --lab unix run use-beetroot
error: project 'project4' requires 'otto-fixture-beetroot == 0.1.0+otto.fixture' — not satisfied in this environment (found: none)
  fix: otto env sync
  or:  uv pip install 'otto-fixture-beetroot == 0.1.0+otto.fixture'
```

`found: none` distinguishes *absent* from *too old* (`found: 2.12`), which are
different problems with different fixes. The second line always names
`env sync`; the third is for operators who manage the environment by hand.

A project that is **not part of this run** warns instead, and the run continues:

```console
$ otto -E project4 --lab unix run test-instruction
warning: project 'project4' requires 'otto-fixture-beetroot == 0.1.0+otto.fixture' — not satisfied in this environment (found: none), but project4 is inactive for this run — continuing without it
```

The check is metadata only — `importlib.metadata` lookups and requirement
evaluation, no network and no imports. It reads the **installed**
distribution's metadata when the project is installed here (exact, and the only
thing that can answer for `dynamic = ["dependencies"]`) and the project's
`pyproject.toml` otherwise. Base dependencies only, direct dependencies only:
an extra's requirements are not checked, and transitive consistency is left to
the installer.

### Where it runs, and what that excludes

It runs **after the lab loads and before anything is contacted**, so it asks
the real activation question — whether *this* project is part of *this* run. A
project whose `host_patterns` match no host in the loaded lab is inactive, and
warns.

Four consequences worth knowing:

- **`otto env` itself is never gated.** It is lab-free, so it never reaches the
  check. The verb the message names always runs.
- **`--help` and completion never pay for it.** Neither reaches the point where
  the check happens.
- **Library callers are checked too.** `open_context` runs the same check
  ({func}`otto.session.check_dependencies`) once its context is installed, and
  refuses with {class}`~otto.session.DependencyRefusedError`. A script that
  skips `open_context` calls `check_dependencies` itself if it wants the check.
- **An eager import still fails at bootstrap.** If a project's `init` module
  imports a missing package at module scope, it fails while otto is registering
  the project — before the check can speak — and you get the generic "failed to
  load" report with the `ImportError`. Importing inside the function that needs
  it is what lets the preflight give you the better message.

Projects with no `pyproject.toml` declare no Python requirements to otto and are
always satisfied. A project whose dependencies are dynamic and which is not
installed here cannot be checked at all; otto says so in one line and checks
nothing rather than guessing.

## The uv it runs

`otto env` builds and fills the environment with the uv installed with otto.
How that uv relates to the uv you run yourself is explained under
{ref}`Which uv runs what <which-uv-runs-what>`. The `last sync:` line that
`create`, `sync` and {doc}`show` print names the otto and the uv that the last
successful create or sync ran.

Two failures come from this, before anything is built or changed:

- **otto cannot run its uv** (`otto.env.UvNotFoundError`): otto's installation
  is missing it, for example after an install that skipped otto's dependencies,
  or the uv it has fails to run or to report its version. The message names the
  cause; reinstall otto with its dependencies.
- **otto is running from inside the environment it would replace**
  (`otto.env.EnvSelfRebuildError`). See {ref}`env-self-rebuild`.

## Passing arguments to uv

Everything after a literal `--` goes verbatim to
{ref}`the uv otto runs <which-uv-runs-what>`, appended last so it can override
anything otto chose:

```console
$ otto env sync -- --find-links ../wheels
```

Use it for hermetic index pins. Note that `--no-index` applies to the
*entire* install, including building any package project — so an air-gapped run
has to supply its build backend's wheels too. A virtual project needs only the
wheels of its dependencies, since it is never built.

## Resolver failures

otto never resolves and never rewrites the resolver's message. A conflict comes
back in uv's own words:

```console
$ otto env create
error: installing projects failed:
  × No solution found when resolving dependencies:
  ╰─▶ Because otto-fixture-beetroot was not found in the package registry and
      you require otto-fixture-beetroot==0.1.0+otto.fixture, we can conclude
      that your requirements are unsatisfiable.
```

At most one line is added, naming the two projects whose requirements collided,
and only when otto can actually attribute the failure.

```{toctree}
:hidden:

create
sync
show
```
