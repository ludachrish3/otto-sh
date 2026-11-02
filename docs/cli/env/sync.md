# otto env sync

Bring this workspace's orchestration environment up to date.

```text
otto env sync [-- INSTALLER_ARGS...]
```

| Option | Description |
| ------ | ----------- |
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

## It records the otto and uv it ran

After a successful sync, the `last sync:` line names the otto you ran and
{ref}`the uv installed with it <which-uv-runs-what>`, and {doc}`show` reports
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
