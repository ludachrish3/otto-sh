# otto env create

Build this workspace's orchestration environment from scratch.

```text
otto env create [--backend uv|pip] [--force] [-- INSTALLER_ARGS...]
```

| Option | Description |
| ------ | ----------- |
| `--backend` | `uv` or `pip`. Outranks `[env] backend` in `settings.toml` |
| `--force` | Remove an existing environment and rebuild it |
| `-- ...` | Everything after `--` is passed to the installer verbatim |

```console
$ otto env create
created ~/.otto/134b91c0-repo1-repo4/env
  installed (editable): repo4
  skipped, no pyproject.toml: repo1
  backend: uv

Activate it with:
  source ~/.otto/134b91c0-repo1-repo4/env/bin/activate
```

## It refuses an environment that already exists

```console
$ otto env create
error: an environment already exists at ~/.otto/134b91c0-repo1-repo4/env — pass
--force to remove and rebuild it, or run `otto env sync` to update it in place
```

Exit code 1. The refusal names both escapes because they mean different things:
`--force` throws the environment away and builds a new one, while
{doc}`sync` updates the one you have.

## `--force` is the recovery story

A wedged environment — a half-finished install, a backend you want to leave
behind — is what `--force` is for. It removes the directory outright rather
than installing over the top, so nothing from the previous build survives.

That includes the recorded backend, and deliberately: the metadata file lives
*inside* the venv, so removing the venv removes it. A rebuild therefore cannot
inherit the backend you were trying to escape. Stored beside the environment it
would have outlived the rebuild and quietly pinned the old choice.

```console
$ otto env create --force --backend pip
created ~/.otto/134b91c0-repo1-repo4/env
  installed (editable): repo4
  skipped, no pyproject.toml: repo1
  backend: pip
```

## `--dry-run` builds nothing

`otto env create -n` validates and exits 0 without creating a virtualenv, the
same way `otto init -n` scaffolds nothing. `env` is a lab-free command, and the
dry-run seam stops lab-free commands before the command body runs — see
{doc}`../dry-run`.

## What it installs

Each discovered repo with a `pyproject.toml`, editable; then otto itself. Repos
without a `pyproject.toml` are named in the output and passed over — they are
not installable, and their `libs` reach `sys.path` at bootstrap regardless.

See {doc}`index` for the backend order, the `--` passthrough, and how resolver
failures are reported.
