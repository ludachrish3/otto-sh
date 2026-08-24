# otto env

`otto env` builds and maintains the **orchestration environment** — the one
virtualenv a multi-project run happens in.

Otto is a single process on a single interpreter. When several repos are active
at once (`OTTO_SUT_DIRS`), their glue code all imports into *that* interpreter,
so each repo's own virtualenv never participates at runtime. The orchestration
environment is the one that must satisfy all of them at once.

```{raw} html
:file: ../../../_static/generated/termynal/help-env.html
```

## Synopsis

```text
otto env create [--backend uv|pip] [--force] [-- INSTALLER_ARGS...]
otto env sync   [--backend uv|pip] [-- INSTALLER_ARGS...]
otto env show
```

| Subcommand | Description |
| ---------- | ----------- |
| [`create`](create.md) | Build the environment from scratch; refuses an existing one unless `--force` |
| [`sync`](sync.md) | Bring it up to date, building it if absent. Never destroys anything |
| [`show`](show.md) | Report where it is, how it was built, and what is in it |

Both `create` and `sync` are **lab-free** — they need no `--lab` — and they act
on the **discovered** repo set rather than the active one. An environment
belongs to a workspace, so which labs today's command happens to load must not
change what goes into it (see {doc}`../projects`).

## The three environments

Naming all three is the point; only the second is otto's business.

1. **A repo's own venv** — for single-repo development. Each repo manages it
   with its own tools from its own `pyproject.toml`. otto does not touch it.
2. **The orchestration venv** — where multi-project runs happen. One per user
   per workspace, and it must be a **superset**: it satisfies the glue imports
   of every active repo at once. This is what `otto env` builds.
3. **Anything else otto happens to run from** — legal for single-repo work,
   discouraged for multi-project work.

If you already use a [uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/)
across your member projects, you have the same thing by another route: one
lockfile, one environment, no otto involvement needed. `otto env` exists for
the common case where the repos are independent checkouts that were never
designed to share a lockfile.

## Where it lives

Under the workspace home — `~/.otto/<hash8>-<slug>/env` — so one environment
serves every invocation against the same repos, from wherever you run them.
See [The workspace home](../index.md#the-workspace-home) for the layout and for
what `OTTO_HOME` moves.

```console
$ otto env create
created ~/.otto/134b91c0-repo1-repo4/env
  installed (editable): repo4
  skipped, no pyproject.toml: repo1
  backend: uv

Activate it with:
  source ~/.otto/134b91c0-repo1-repo4/env/bin/activate
```

## What goes in

- **Every discovered repo that has a `pyproject.toml`**, installed *editable*,
  so your live checkouts stay live.
- **Repos without one are skipped**, with a notice naming them. That is not an
  error and never has been: their `libs` ride `sys.path` at bootstrap, which
  remains correct. A workspace made entirely of such repos still gets an
  environment, because otto itself goes into it.
- **otto**, at whatever version you are running — and *the way* you are running
  it. If your otto is an editable install from a checkout, the environment gets
  that same checkout; if it came from a wheel, the environment pins that
  version. A pipx-global otto would otherwise import against the wrong
  site-packages and the environment would be decoration.

## Backends

uv when it is on `PATH`, otherwise the standard library's `venv` plus the
environment's own pip. The fallback deliberately adds no dependency of its own.

The choice is resolved in this order, and each step exists for a different
reason:

| Source | Meaning |
| ------ | ------- |
| `--backend` | This invocation's decision |
| `[env] backend` in `settings.toml` | The repo's standing decision |
| Recorded in the environment | Keeps an existing environment self-consistent |
| Auto-detect | What happens when nobody has said anything |

An explicit backend that cannot be honoured is **refused, not downgraded**:
`--backend uv` on a host without uv is an error naming `--backend pip`, because
you asked for uv precisely to avoid the fallback.

Switching backends under an existing environment is a `create --force` matter,
not a silent migration — the recorded value lives *inside* the venv, so
`--force` takes it with the rebuild.

If two repos in one workspace declare different `[env] backend` values, that is
a hard error naming both. Silently picking one would bind an installer you did
not choose.

## Passing arguments to the installer

Everything after a literal `--` goes to uv or pip verbatim, appended last so it
can override anything otto chose:

```console
$ otto env sync -- --find-links ../wheels
```

Hermetic index pins are the whole reason this exists. Note that `--no-index`
applies to the *entire* install, including building any repo that needs a build
backend — so an air-gapped run has to supply those wheels too.

## Resolver failures

otto never resolves and never rewrites the resolver's message. A conflict comes
back in the installer's own words:

```console
$ otto env create
error: installing repos failed:
  × No solution found when resolving dependencies:
  ╰─▶ Because otto-fixture-beetroot was not found in the package registry and
      otto-sample-repo4==0.1.0 depends on otto-fixture-beetroot>=0.1, we can
      conclude that otto-sample-repo4==0.1.0 cannot be used.
```

At most one line is added, naming the two repos whose requirements collided,
and only when otto can actually attribute the failure. A guess there would be
worse than silence — it would send you to edit the wrong `pyproject.toml`.

```{toctree}
:hidden:

create
sync
show
```
