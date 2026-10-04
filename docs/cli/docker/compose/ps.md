# otto docker compose ps

Print `docker compose ps` for a use-case's project on each docker-capable
host, exactly as docker printed it.

```text
otto docker compose ps [USE_CASE] [-a|--all] [--parent HOST] [--provide CAP=REPO]...
```

| Option | Description |
| ------ | ----------- |
| `USE_CASE` (argument) | Use-case whose stacks to list (default: the only one declared; several is an error) |
| `-a`, `--all` | Show every container of the project, not only the running ones (docker's `-a`) |
| `--parent HOST` | Ask this docker-capable host only (default: every one; see {ref}`Which host <docker-which-host>`) |
| `--provide CAP=REPO` | Break a provider tie for capability `CAP`; resolved exactly as {doc}`up` resolves it. Repeatable |

`USE_CASE`, `--parent` and `--provide` pick the same compose project `up` and
{doc}`down` pick, so `ps` can never look at a different stack than the one
deployed. Where the plain {doc}`../ps` lists every container
a host's daemon holds, this lists only that project's.

otto adds one line per host, `== <host-id> ==`, and changes nothing inside
docker's output. A host whose daemon could not answer prints docker's error
under its header; the command goes on to the next host and exits 1 at the
end. Library: {func}`~otto.docker.compose_ps`.

An illustrative listing — your daemon's columns and rows are what you will see:

```text
$ otto docker compose ps integration
== test3 ==
NAME                        IMAGE     COMMAND   SERVICE   CREATED         STATUS         PORTS
unix-integration-ci-api-1   app-api   "run"     api       2 minutes ago   Up 2 minutes
unix-integration-ci-db-1    app-db    "run"     db        2 minutes ago   Up 2 minutes   5432/tcp
```
