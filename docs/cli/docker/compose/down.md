# otto docker compose down

Tear a use-case's stack down and unregister its container hosts.

```text
otto docker compose down [USE_CASE [SERVICE]...] [--parent HOST] [--provide CAP=REPO]...
```

| Option | Description |
| ------ | ----------- |
| `USE_CASE` (argument) | Use-case to tear down (default: the only one declared; several is an error) |
| `SERVICE...` (argument) | Tear down only these services; requires an explicit `USE_CASE` |
| `--parent HOST` | The docker-capable lab host to tear down on (default: {ref}`Which host <docker-which-host>`) |
| `--provide CAP=REPO` | Break a provider tie for capability `CAP`. Repeatable |

The outcome is one line, `test3: integration torn down` or
`test3: FAILED — <command>: <output>`, and the exit code is 1 when the
teardown failed. Container hosts are unregistered either way. Library:
{func}`~otto.docker.teardown`, which returns a
{class}`~otto.docker.reports.TeardownReport`.

`--parent` and `--provide` are resolved exactly as {doc}`up` resolves them, so a
teardown can never address a different project than the deployment it is
undoing. Naming services stops and removes just those, leaving the rest of the
stack and its network standing.

Like `up`, `down` has no `--repo`: narrowing is by use-case and service
({doc}`../use-cases`).

The container host ids stay synthesized after `down` — they are derived from
the lab declaration, not from what is running — so completion keeps offering
them and the next access auto-starts the stack again.
