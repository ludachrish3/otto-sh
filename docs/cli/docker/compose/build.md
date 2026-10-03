# otto docker compose build

Build the images a deployment of a use-case would use, on the hosts it would use.

```text
otto docker compose build [USE_CASE [IMAGE]...] [--on HOST] [--provide CAP=REPO]...
                          [--no-cache] [--pull] [--build-arg KEY=VALUE]...
```

| Option | Description |
| ------ | ----------- |
| `USE_CASE` (argument) | The use-case (default: the only one declared; several is an error) |
| `IMAGE...` (argument) | Declared image names to build, over the use-case's winners (default: all); requires an explicit `USE_CASE` |
| `--on HOST` | Collapse every fragment onto this lab host, as {doc}`up` does |
| `--provide CAP=REPO` | Break a provider tie for capability `CAP`. Repeatable |
| `--no-cache`, `--pull`, `--build-arg KEY=VALUE` | `docker build`'s own flags, passed to every image this builds; see [the flags table](../build.md#flags) |

There is no `--tag`: every image is tagged `<name>:latest`, the name the
compose file refers to, as `docker compose build` has no `--tag` either.

This runs the **same** provider competition and placement {doc}`up` runs and
builds only the winners' images, each on the host `up` would deploy it to. A
displaced mock's image is not built, and an `IMAGE` only a displaced repo
declares is refused. `compose build` followed by `compose up --no-build`
deploys exactly what was just built. Library:
{func}`~otto.docker.compose_build`; a test pins its placement against
`deploy`'s.

The output, refusals and exit code follow {doc}`../build`. Under `--dry-run`
the whole plan is printed (each host, its repos, their images, and the exact
`docker build` for each) and nothing is built.
