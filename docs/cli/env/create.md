# otto env create

Build this workspace's orchestration environment from scratch.

```text
otto env create [--force] [-- INSTALLER_ARGS...]
```

| Option | Description |
| ------ | ----------- |
| `--force` | Remove an existing environment and rebuild it |
| `-- ...` | Everything after `--` is passed to uv verbatim |

```console
$ otto env create
created ~/.otto/134b91c0-project1-project4/env
  installed (editable): project4
  skipped, no pyproject.toml: project1
  last sync:   otto %OTTO_VERSION%, uv 0.13.6

Activate it with:
  source ~/.otto/134b91c0-project1-project4/env/bin/activate
```

## It refuses an environment that already exists

```console
$ otto env create
error: an environment already exists at ~/.otto/134b91c0-project1-project4/env — pass
--force to remove and rebuild it, or run `otto env sync` to update it in place
```

Exit code 1. The refusal names both escapes: `--force` throws the environment
away and builds a new one, while {doc}`sync` updates the one you have.

## `--force` is the recovery story

A wedged environment, such as a half-finished install, is what `--force` is for.
It removes the directory outright rather than installing over the top, so
nothing from the previous build survives. That includes the metadata file, which
lives *inside* the venv: the `last sync:` line afterwards describes the new build
only.

```console
$ otto env create --force
created ~/.otto/134b91c0-project1-project4/env
  installed (editable): project4
  skipped, no pyproject.toml: project1
  last sync:   otto %OTTO_VERSION%, uv 0.13.6
```

(env-self-rebuild)=

## It refuses to replace the environment otto is running from

`create` runs {ref}`the uv installed with otto <which-uv-runs-what>`. If that otto
is installed *inside* the environment being built, as it is once you activate the
environment and run otto from it, `--force` would delete the very otto and uv it
is about to run. So `create`, with or without `--force`, refuses before deleting or
installing anything:

```console
$ source ~/.otto/134b91c0-project1-project4/env/bin/activate
$ otto env create --force
error: otto is running from inside the environment it would replace
(~/.otto/134b91c0-project1-project4/env) — run otto from an installation outside
that env (for example, the project's own .venv)
```

Exit code 1. Run the command with an otto installed somewhere else, for example
your project's own `.venv/` from the
[recommended setup](../../installation.md#with-uv-recommended).
{doc}`sync` refuses the same way, for the same reason. The error is
`otto.env.EnvSelfRebuildError`.

## `--dry-run` builds nothing

`otto env create -n` validates and exits 0 without creating a virtualenv, the
same way `otto init -n` scaffolds nothing. `env` is a lab-free command, and the
dry-run seam stops lab-free commands before the command body runs — see
{doc}`../dry-run`.

## What it installs

Each discovered project with a `pyproject.toml`, editable; then otto itself. Projects
without a `pyproject.toml` are named in the output and passed over — they are
not installable, and their `libs` reach `sys.path` at bootstrap regardless.

See {doc}`index` for the uv it runs, the `--` passthrough, and how resolver
failures are reported.
