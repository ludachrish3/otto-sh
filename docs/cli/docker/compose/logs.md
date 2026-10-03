# otto docker compose logs

Print `docker compose logs` for a use-case's project on every host the
use-case is placed on, exactly as docker printed it.

```text
otto docker compose logs [USE_CASE [SERVICE]...] [--tail N] [--since WHEN] [-t|--timestamps]
                         [-f|--follow] [--on HOST] [--provide CAP=REPO]...
```

| Option | Description |
| ------ | ----------- |
| `USE_CASE` (argument) | Use-case whose logs to print (default: the only one declared; several is an error) |
| `SERVICE...` (argument) | Only these services' logs; requires an explicit `USE_CASE`. A host is asked only for the named services it runs |
| `--tail N` | Number of lines from the end of each log (docker's `--tail`) |
| `--since WHEN` | Logs since a timestamp or a relative time such as `10m` (docker's `--since`) |
| `-t`, `--timestamps` | Show timestamps (docker's `-t`) |
| `-f`, `--follow` | Follow the logs live; Ctrl-C ends it. Needs an SSH parent; `compose logs -f` follows one host, so name it with `--on` when the use-case is placed on several |
| `--on HOST` | Collapse every fragment onto this lab host; resolved exactly as {doc}`up` resolves it |
| `--provide CAP=REPO` | Break a provider tie for capability `CAP`; resolved exactly as {doc}`up` resolves it. Repeatable |

`USE_CASE`, `--on` and `--provide` pick the same hosts and the same compose
project `up` and {doc}`down` pick. Naming no service prints every service's
log, docker's own meaning. Naming one that no participating fragment declares
is refused with the services that are declared, exit 1.

otto adds one line per host, `== <host-id> ==`, and changes nothing inside
docker's output. A host whose daemon could not answer prints docker's error
under its header; the command goes on to the next host and exits 1 at the
end. Library: {func}`~otto.docker.compose_logs`.

`--follow` rides the same SSH login bridge as {doc}`../logs`, which also says how
a follow ends, what the PTY does to the stream, and which exit status it
reports. A use-case placed on several hosts is refused naming them, exit 2,
until `--on` picks one.

An illustrative transcript — your containers' lines are what you will see:

```text
$ otto docker compose logs integration api --tail 2
== test3 ==
unix-integration-ci-api-1  | starting
unix-integration-ci-api-1  | ready
```
