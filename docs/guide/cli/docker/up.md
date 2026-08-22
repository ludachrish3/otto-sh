# otto docker up

Bring up the compose stacks for the selected repos and register their
containers as lab hosts.

```text
otto docker up [--repo NAME] [--on HOST] [--no-build]
```

| Option | Description |
| ------ | ----------- |
| `--repo NAME` | Restrict to a single repo by name |
| `--on HOST` | Lab host id to operate on (default: all docker-capable hosts) |
| `--no-build` | Skip the implicit build step before `compose up` |

`up` builds first unless `--no-build` is given. Once the stack is running its
services are addressable as `<parent>.<project>.<service>` — see
[Container hosts](index.md#container-hosts).
