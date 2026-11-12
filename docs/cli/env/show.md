# otto env show

Report this workspace's orchestration environment and what is in it.

```text
otto env show
```

```console
$ otto env show
environment: ~/.otto/134b91c0-project1-project4/env
  last sync:   otto %OTTO_VERSION%, uv 0.13.6
╭──────────┬──────────────────────┬───────────┬────────────────────────────────────────╮
│ project  │ distribution         │ installed │ state                                  │
├──────────┼──────────────────────┼───────────┼────────────────────────────────────────┤
│ project1 │ —                    │ —         │ no pyproject.toml (libs ride sys.path) │
│ project4 │ otto-sample-project4 │ yes       │ current                                │
╰──────────┴──────────────────────┴───────────┴────────────────────────────────────────╯
```

| Column | Meaning |
| ------ | ------- |
| `project` | The project's otto name, from its `settings.toml` |
| `distribution` | Its `[project] name`, or `—` when it has no `pyproject.toml` |
| `installed` | Whether that distribution is actually present in the environment. A {ref}`virtual project <env-what-goes-in>` has no distribution, so it reads `deps met` or `deps missing` instead |
| `state` | `current`, `stale`, or why the project is skipped (see {ref}`env-what-goes-in`) |

The `last sync:` line names the otto and the
{ref}`uv otto runs <which-uv-runs-what>` as of the last successful
{doc}`create` or {doc}`sync`; the `installed` column is checked live.

## It never fails on a broken environment

```console
$ otto env show
no environment for this workspace (~/.otto/134b91c0-project1-project4/env)
build one with:
  otto env create
```

Exit code 0. An environment whose metadata is unreadable degrades the same
way — it says so and names `otto env sync`, then `otto env create --force` for an
environment that is actually broken. An environment built by an earlier otto
reads this way too, and {doc}`sync` writes the record afresh without a rebuild.

## `stale` is an mtime comparison

A project reads as `stale` when its `pyproject.toml` is newer than the
environment's metadata file: the environment was built before that project's
requirements last changed.

```console
$ otto env show
│ project4 │ otto-sample-project4 │ yes       │ stale — run `otto env sync`            │
```

The check is cheap — no imports, no installer call, nothing that can hang on
a slow index. It answers "might this environment be out of date?", not
"is it definitely wrong": touching a `pyproject.toml` without changing its
requirements will also flag it, and `otto env sync` is a quick no-op in that
case.

## `installed` asks the environment itself

The check runs one query inside the environment's own interpreter. If the
environment's interpreter cannot answer, the column reads `?`.

For a virtual project, the same query reports the environment's site
directories and its own marker values (`python_version` and the rest, from the
Python uv picked for it, not the one otto runs on), and the
{ref}`dependency preflight <env-dependency-preflight>` judges the project's
dependencies against them, so `show` and the preflight cannot disagree. A
virtual project with dynamic dependencies cannot be judged and reads `?`.

A project showing `no` after a successful build usually means its install failed
and the failure was reported at the time — see the resolver output in
{doc}`index`.
