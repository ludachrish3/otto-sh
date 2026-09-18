# otto docker down

Tear a use-case's stacks down and unregister their container hosts.

```text
otto docker down [USE_CASE [SERVICE]...] [--on HOST] [--provide CAP=REPO]...
```

| Option | Description |
| ------ | ----------- |
| `USE_CASE` (argument) | Use-case to tear down (default: the only one declared; several is an error) |
| `SERVICE...` (argument) | Tear down only these services; requires an explicit `USE_CASE` |
| `--on HOST` | Collapse every fragment of the deployment onto this lab host |
| `--provide CAP=REPO` | Break a provider tie for capability `CAP`. Repeatable |

`--on` and `--provide` are resolved exactly as {doc}`up` resolves them, so a
teardown can never address a different project than the deployment it is
undoing. Naming services stops and removes just those, leaving the rest of the
stack and its network standing.

Like `up`, `down` has no `--repo` — see {doc}`use-cases` for why narrowing is
by use-case.

The container host ids stay synthesized after `down` — they are derived from
the lab declaration, not from what is running — so completion keeps offering
them and the next access auto-starts the stack again.
