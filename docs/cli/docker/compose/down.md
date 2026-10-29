# otto docker compose down

Tear a use-case's stack down and unregister its container hosts.

```text
otto docker compose down [USE_CASE [SERVICE]...] [--parent HOST] [--provide CAP=PROJECT]...
```

| Option | Description |
| ------ | ----------- |
| `USE_CASE` (argument) | Use-case to tear down (default: the only one declared; several is an error) |
| `SERVICE...` (argument) | Tear down only these services; requires an explicit `USE_CASE` |
| `--parent HOST` | The docker-capable lab host to tear down on (default: {ref}`Which host <docker-which-host>`) |
| `--provide CAP=PROJECT` | Break a provider tie for capability `CAP`. Repeatable |

The outcome is one line, `test3: integration torn down` or
`test3: FAILED — <command>: <output>`, and the exit code is 1 when the
teardown failed. Container hosts are unregistered whether the `down`
succeeded or failed. The files the deployment staged on the parent stay,
because the next `down` reads them back; `otto run cleanup` removes them (see
[What `cleanup` takes off the lab](../../run/defaults.md#what-cleanup-takes-off-the-lab)).
When otto cannot read back the files the deployment staged, or cannot trust
where they are ({ref}`docker-staging`), `down` stops nothing, exits with an
error naming the staging directory, and leaves the hosts registered (see
"What `down` removes" in {doc}`../use-cases`). Library:
{func}`~otto.docker.teardown`, which returns a
{class}`~otto.docker.reports.TeardownReport`.

`--parent` and `--provide` are resolved exactly as {doc}`up` resolves them, so a
teardown can never address a different compose project than the deployment it is
undoing. Naming services stops and removes just those, leaving the rest of the
stack and its network standing. Without service names, `down` removes only the
services the deployment's own compose files declare; what that does and does
not reach, and the fallback when those files are gone, is under "What `down`
removes" in {doc}`../use-cases`.

Like `up`, `down` has no `--project`: narrowing is by use-case and service
({doc}`../use-cases`).

The container host ids stay synthesized after `down` — they are derived from
the lab declaration, not from what is running — so completion keeps offering
them, and the next access fails until the stack is brought up again.
