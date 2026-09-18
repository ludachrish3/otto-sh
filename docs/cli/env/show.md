# otto env show

Report this workspace's orchestration environment and what is in it.

```text
otto env show
```

```console
$ otto env show
environment: ~/.otto/134b91c0-repo1-repo4/env
  backend:     uv
  otto:        0.8.6
╭───────┬───────────────────┬───────────┬───────────────────────────────────╮
│ repo  │ distribution      │ installed │ state                             │
├───────┼───────────────────┼───────────┼───────────────────────────────────┤
│ repo1 │ —                 │ —         │ no pyproject (libs ride sys.path) │
│ repo4 │ otto-sample-repo4 │ yes       │ current                           │
╰───────┴───────────────────┴───────────┴───────────────────────────────────╯
```

| Column | Meaning |
| ------ | ------- |
| `repo` | The repo's otto name, from its `settings.toml` |
| `distribution` | Its `[project] name`, or `—` when it has no `pyproject.toml` |
| `installed` | Whether that distribution is actually present in the environment |
| `state` | `current`, `stale`, or why the repo is not installable |

## It never fails on a broken environment

```console
$ otto env show
no environment for this workspace (~/.otto/134b91c0-repo1-repo4/env)
build one with:
  otto env create
```

Exit code 0. `show` is the verb you reach for when something looks wrong, and a
diagnostic that fails when things are broken is the one you needed most. An
environment whose metadata is unreadable degrades the same way — it says so and
names `otto env create --force`, rather than taking the command down over a
field it could have skipped.

## `stale` is an mtime comparison

A repo reads as `stale` when its `pyproject.toml` is newer than the
environment's metadata file: the environment was built before that repo's
requirements last changed.

```console
$ otto env show
│ repo4 │ otto-sample-repo4 │ yes       │ stale — run `otto env sync`       │
```

This is deliberately cheap — no imports, no installer call, nothing that can
hang on a slow index. It answers "might this environment be out of date?", not
"is it definitely wrong": touching a `pyproject.toml` without changing its
requirements will also flag it, and `otto env sync` is a quick no-op in that
case.

## `installed` asks the environment itself

The check runs one query inside the environment's own interpreter, because that
is the only answer that stays true across uv and pip layouts. If the
environment's interpreter cannot answer, the column reads `?` rather than
guessing.

A repo showing `no` after a successful build usually means its install failed
and the failure was reported at the time — see the resolver output in
{doc}`index`.
