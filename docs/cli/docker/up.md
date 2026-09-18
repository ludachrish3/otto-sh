# otto docker up

Deploy a use-case: one merged compose stack per resolved host, with every
resulting container registered as a lab host.

```text
otto docker up [USE_CASE [SERVICE]...] [--on HOST] [--no-build]
               [--provide CAP=REPO]... [--env K=V]... [--env-file PATH]...
```

| Option | Description |
| ------ | ----------- |
| `USE_CASE` (argument) | Use-case to deploy (default: the only one declared; several is an error) |
| `SERVICE...` (argument) | Deploy only these services; requires an explicit `USE_CASE` |
| `--on HOST` | Collapse every fragment of the deployment onto this lab host |
| `--no-build` | Skip the implicit build step before `compose up` |
| `--provide CAP=REPO` | Break a provider tie for capability `CAP`. Repeatable |
| `--env K=V` | Extra env var; wins over every channel. Repeatable |
| `--env-file PATH` | Local `KEY=VALUE` file, read client-side, merged under `--env`. Repeatable |

There is no `--repo`: a merged deployment is not per-repo, so narrowing is by
use-case name (and `build` keeps `--repo` for its own per-repo meaning).

`up` builds every participating repo's declared images first unless
`--no-build` is given, and it is convergent — re-running a broader deployment
adds to a live stack, and `--remove-orphans` reaps what a provider swap left
behind.

{doc}`use-cases` is the workflow home: which fragments take part, where each
one lands, how the env mapping is assembled, and what a `--dry-run` preview
shows. Once the stack is running, its services are addressable as
`<parent>.<usecase>.<service>` — see
[Container hosts](index.md#container-hosts).
