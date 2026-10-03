# otto docker compose up

Deploy a use-case: one merged compose stack per resolved host, with every
resulting container registered as a lab host.

```text
otto docker compose up [USE_CASE [SERVICE]...] [--on HOST] [--build] [--force-recreate]
                       [--pull POLICY] [--provide CAP=REPO]... [--env K=V]...
                       [--env-file PATH]...
```

| Option | Description |
| ------ | ----------- |
| `USE_CASE` (argument) | Use-case to deploy (default: the only one declared; several is an error) |
| `SERVICE...` (argument) | Deploy only these services; requires an explicit `USE_CASE` |
| `--on HOST` | Collapse every fragment of the deployment onto this lab host |
| `--build` | Build the participating repos' declared `[[docker.images]]` first (otto's build; docker's own `--build` is not passed) |
| `--force-recreate` | Recreate containers even if their configuration is unchanged |
| `--pull POLICY` | docker's `--pull` policy, passed through unchanged |
| `--provide CAP=REPO` | Break a provider tie for capability `CAP`. Repeatable |
| `--env K=V` | Extra env var; wins over every channel. Repeatable |
| `--env-file PATH` | Local `KEY=VALUE` file, read client-side, merged under `--env`. Repeatable |

There is no `--repo`: a merged deployment is not per-repo, so narrowing is by
use-case name (`--repo` belongs to the image-level {doc}`../build`).

`up` is `docker compose up -d`: otto builds nothing on its own, so the `up` is
docker's, with docker's own build and pull policy, and a service whose image is
missing fails with docker's own error. `--build` builds every participating
repo's declared `[[docker.images]]` first (the build itself is {doc}`build`'s;
it is otto's build, and docker's own `--build` is not passed). `--force-recreate`
and `--pull` are handed to docker unchanged, and the value of `--pull` is
docker's to judge: a policy docker rejects is docker's error. `up` is
convergent — re-running a broader deployment adds to a live stack, and
`--remove-orphans` reaps what a provider swap left behind.

{doc}`../use-cases` is the workflow home: which fragments take part, where each
one lands, how the env mapping is assembled, and what a `--dry-run` preview
shows. Once the stack is running, its services are addressable as
`<parent>.<usecase>.<service>` — see
[Container hosts](../index.md#container-hosts).
