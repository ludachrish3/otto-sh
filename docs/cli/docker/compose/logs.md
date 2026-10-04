# otto docker compose logs

Print `docker compose logs` for a use-case's project on the parent, exactly as
docker printed it.

```text
otto docker compose logs [USE_CASE [SERVICE]...] [--tail N] [--since WHEN] [-t|--timestamps]
                         [-f|--follow] [--parent HOST] [--provide CAP=REPO]...
```

| Option | Description |
| ------ | ----------- |
| `USE_CASE` (argument) | Use-case whose logs to print (default: the only one declared; several is an error) |
| `SERVICE...` (argument) | Only these services' logs; requires an explicit `USE_CASE` |
| `--tail N` | Number of lines from the end of each log (docker's `--tail`) |
| `--since WHEN` | Logs since a timestamp or a relative time such as `10m` (docker's `--since`) |
| `-t`, `--timestamps` | Show timestamps (docker's `-t`) |
| `-f`, `--follow` | Follow the logs live; Ctrl-C ends it. Needs an SSH parent |
| `--parent HOST` | The docker-capable lab host to read the logs from (default: {ref}`Which host <docker-which-host>`) |
| `--provide CAP=REPO` | Break a provider tie for capability `CAP`; resolved exactly as {doc}`up` resolves it. Repeatable |

`USE_CASE`, `--parent` and `--provide` pick the same compose project `up` and
{doc}`down` pick on that host. Naming no service prints every service's
log, docker's own meaning. Naming one that no participating fragment declares
is refused with the services that are declared, exit 1.

otto adds one line, `== <host-id> ==`, and changes nothing inside docker's
output. A parent whose daemon could not answer prints docker's error under
its header and the command exits 1. Library: {func}`~otto.docker.compose_logs`.

`--follow` rides the same SSH login bridge as {doc}`../logs`, which also says how
a follow ends, what the PTY does to the stream, and which exit status it
reports. A follow streams from one host, which is why this verb reads one
parent and never the whole lab.

An illustrative transcript — your containers' lines are what you will see:

```text
$ otto docker compose logs integration api --tail 2
== test3 ==
unix-integration-ci-api-1  | starting
unix-integration-ci-api-1  | ready
```
