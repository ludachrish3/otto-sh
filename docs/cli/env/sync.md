# otto env sync

Bring this workspace's orchestration environment up to date.

```text
otto env sync [--otto VERSION] [-- INSTALLER_ARGS...]
```

| Option | Description |
| ------ | ----------- |
| `--otto VERSION` | Install this otto release into the environment, not the otto that is running. See [Installing a different otto](#installing-a-different-otto) |
| `-- ...` | Everything after `--` is passed to uv verbatim |

```console
$ otto env sync
synced ~/.otto/134b91c0-project1-project4/env
  installed (editable): project4
  skipped, no pyproject.toml: project1
  last sync:   otto %OTTO_VERSION%, uv 0.13.6

Activate it with:
  source ~/.otto/134b91c0-project1-project4/env/bin/activate
```

## It never destroys anything

`sync` re-runs the installs into the environment you already have. It does not
remove it, and it does not remove anything from it. If you need a clean build,
that is {doc}`create` with `--force`.

`sync` is the verb otto's own messages suggest — when {doc}`show` reports a
project as stale, when a dependency preflight refuses a run — and following it is
always safe.

## A missing environment is created, not refused

```console
$ otto env sync
created ~/.otto/134b91c0-project1-project4/env
```

`sync` with no environment does exactly what {doc}`create` does.

(installing-a-different-otto)=

## Installing a different otto

By default the environment gets the otto that ran `sync`. `--otto` names
another release, and uv finds and installs it. {doc}`create` takes the same
option, with the same meaning:

```console
$ otto env sync --otto 0.19.0
$ otto env sync --otto '>=0.19,<0.20'
```

- A version, such as `0.19.0`, means exactly that release.
- A specifier, such as `'>=0.19,<0.20'`, lets uv pick the newest release that
  matches. Quote it, so the shell does not read `<` and `>`.
- Anything else is refused before the environment is touched. That includes
  `===`, a URL, a path, and a name such as `latest`.

The projects are installed as usual; only the otto step changes. The release
comes from wherever uv looks, so the `--` arguments apply to it too: an
air-gapped sync needs that release's wheel behind `-- --find-links`. If uv
cannot find a matching release, its own message is shown and the sync fails.

Use it when a project needs an otto other than the one you run, for example
after checking out a branch that pins an older release. Then activate the
environment and run that otto from it. This is a convenience for that one
step: installing and upgrading otto itself is still done with uv or pip
({doc}`../../installation`).

Run it from an otto outside the environment, as every sync is: `sync`
{ref}`refuses to update the environment it is running from <env-self-rebuild>`.
The next `otto env sync` without `--otto` puts the running otto back.

## It records the otto it installed and the uv it ran

After a successful sync, the `last sync:` line names the otto now in the
environment (the one you ran, or the release `--otto` installed; `unknown` if
the environment could not be asked after an `--otto` install) and
{ref}`the uv installed with otto <which-uv-runs-what>`, and {doc}`show` reports
the same line until the next create or sync. An environment whose metadata
{doc}`show` cannot read needs nothing special: `sync` runs and writes it afresh.

`sync` refuses to run from inside the environment it would update, exactly as
{ref}`create does <env-self-rebuild>`.

## When to run it

- After a project's `pyproject.toml` changes — {doc}`show` flags this as `stale`.
- After adding or removing a project from `OTTOPATH`. Note that this changes
  the workspace, and therefore the environment: a different project set is a
  different workspace home, so the first `sync` there builds rather than
  updates.
- After pulling changes that add a dependency.
