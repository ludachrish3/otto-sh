# otto docker ps

List the containers running on the lab's docker-capable hosts.

```text
otto docker ps [--on HOST]
```

| Option | Description |
| ------ | ----------- |
| `--on HOST` | Lab host id to query (default: all docker-capable hosts) |

`ps` reports what is actually running. To see every container id the lab
*declares*, whether or not it is up, use the top-level `--list-hosts`.
