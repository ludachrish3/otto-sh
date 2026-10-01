# otto docker compose build

Build the images a deployment of a use-case would use, on the hosts it would use.

```text
otto docker compose build [USE_CASE [IMAGE]...] [--on HOST] [--provide CAP=REPO]... [--rebuild]
```

| Option | Description |
| ------ | ----------- |
| `USE_CASE` (argument) | The use-case (default: the only one declared; several is an error) |
| `IMAGE...` (argument) | Declared image names to build, over the use-case's winners (default: all); requires an explicit `USE_CASE` |
| `--on HOST` | Collapse every fragment onto this lab host, as {doc}`up` does |
| `--provide CAP=REPO` | Break a provider tie for capability `CAP`. Repeatable |
| `--rebuild` | Force a rebuild even when a context-hash tag already exists |

This runs the **same** provider competition and placement {doc}`up` runs and
builds only the winners' images, each on the host `up` would deploy it to. A
displaced mock's image is not built, and an `IMAGE` only a displaced repo
declares is refused. `compose build` followed by `compose up --no-build`
deploys exactly what was just built. Library:
{func}`~otto.docker.compose_build`; a test pins its placement against
`deploy`'s.

The output and exit code follow {doc}`../build`. Under `--dry-run` the whole
plan is printed (each host, its repos, their images) and nothing is built.
