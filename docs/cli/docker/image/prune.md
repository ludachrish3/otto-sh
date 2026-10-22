# otto docker image prune

Run `docker image prune` on every docker-capable host in the lab, and print
what docker printed on each.

```text
otto docker image prune -f|--force [-a|--all] [--filter FILTER]... [--parent HOST]
```

| Option | Description |
| ------ | ----------- |
| `-f`, `--force` | Required: docker asks for confirmation without it, and otto never prompts |
| `-a`, `--all` | Remove every image no container uses, not only the dangling ones (docker's `-a`) |
| `--filter FILTER` | docker's filter, passed through as typed: `until=24h`, `label=KEY=VALUE`, `label!=KEY`. Repeatable |
| `--parent HOST` | Run on one docker-capable host (default: every one; see {ref}`Which host <docker-which-host>`) |

Without `-f`, the verb exits 2, says `-f` is needed, and nothing runs on
any host. `docker image prune` would stop to ask, and otto never prompts, so
`-f` is how you say yes. Each `--filter` value goes to docker as typed,
quoted for the host's shell. docker judges whether the value is valid.

Without `--parent`, the prune runs on **every** docker-capable host of the lab.
Name one host to prune one daemon only. otto adds one `== <host-id> ==` line per
host and changes nothing in docker's lines. A failing host prints docker's
error (or otto's, for a host otto could not reach) under its header, the
command goes on to the next host, and exits 1 at the end. Afterwards otto
asks each host it reached for its images again, for TAB. Library:
{func}`~otto.docker.image_verbs.prune_images`.

```text
$ otto docker image prune -f --filter until=24h --parent test3
== test3 ==
Deleted Images:
deleted: sha256:3f1c2a9b7d10…

Total reclaimed space: 148MB
```

## Dry run

Under `--dry-run` (see {doc}`../../dry-run`) the verb removes nothing and
prints the exact command it would run on each host, one `on <host>:
docker image prune -f …` line per host, then "No image was removed."
`-f` is still required, so a dry run without it is refused the same way.
