# otto env sync

Bring this workspace's orchestration environment up to date.

```text
otto env sync [--backend uv|pip] [-- INSTALLER_ARGS...]
```

| Option | Description |
| ------ | ----------- |
| `--backend` | `uv` or `pip`. Outranks `[env] backend` in `settings.toml` |
| `-- ...` | Everything after `--` is passed to the installer verbatim |

```console
$ otto env sync
synced ~/.otto/134b91c0-repo1-repo4/env
  installed (editable): repo4
  skipped, no pyproject.toml: repo1
  backend: uv

Activate it with:
  source ~/.otto/134b91c0-repo1-repo4/env/bin/activate
```

## It never destroys anything

`sync` re-runs the installs into the environment you already have. It does not
remove it, and it does not remove anything from it. If you need a clean build,
that is {doc}`create` with `--force`.

This matters because `sync` is the verb otto's own messages suggest — when
{doc}`show` reports a repo as stale, when a dependency preflight refuses a run.
A verb that error messages name has to stay safe to follow.

## A missing environment is created, not refused

```console
$ otto env sync
created ~/.otto/134b91c0-repo1-repo4/env
```

Refusing here would be the worst possible answer to "your environment is out of
date": the operator followed the advice and got a second error. So `sync` with
no environment does exactly what {doc}`create` does.

## It keeps the environment's backend

An environment built by uv keeps being filled by uv. The backend was recorded
when it was built, and `sync` re-uses it unless `--backend` or `[env] backend`
says otherwise — an environment half-built by two installers is not a state
worth being able to reach by accident.

## When to run it

- After a repo's `pyproject.toml` changes — {doc}`show` flags this as `stale`.
- After adding or removing a repo from `OTTO_SUT_DIRS`. Note that this changes
  the workspace, and therefore the environment: a different repo set is a
  different workspace home, so the first `sync` there builds rather than
  updates.
- After pulling changes that add a dependency.
