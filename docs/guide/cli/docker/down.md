# otto docker down

Tear down the compose stacks for the selected repos.

```text
otto docker down [--repo NAME] [--on HOST]
```

| Option | Description |
| ------ | ----------- |
| `--repo NAME` | Restrict to a single repo by name |
| `--on HOST` | Lab host id to operate on (default: all docker-capable hosts) |

The container host ids stay synthesized after `down` — they are derived from
the lab declaration, not from what is running — so completion keeps offering
them and the next access auto-starts the stack again.
