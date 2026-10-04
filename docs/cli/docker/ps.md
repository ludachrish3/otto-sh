# otto docker ps

Print `docker ps` from every docker-capable host in the lab, exactly as
docker printed it.

```text
otto docker ps [-a|--all] [--parent HOST]
```

| Option | Description |
| ------ | ----------- |
| `-a`, `--all` | Show every container, not only the running ones (docker's `-a`) |
| `--parent HOST` | Ask one docker-capable host (default: every one; see {ref}`Which host <docker-which-host>`) |

otto adds one line per host, `== <host-id> ==`, and changes nothing inside
docker's output: every id is as long as docker printed it, every column
where docker put it. A host whose daemon could not answer prints docker's
error under its header; the command goes on to the next host and exits 1 at
the end. Library: {func}`~otto.docker.observe.list_containers`.

An illustrative listing — your daemon's columns and rows are what you will see:

```text
$ otto docker ps
== test3 ==
CONTAINER ID   IMAGE              COMMAND           CREATED         STATUS         PORTS   NAMES
3f1c2a9b7d10   repo1-api:latest   "python -m api"   2 minutes ago   Up 2 minutes           unix-repo1-e2e-1a2b3c4d-api-1

== test1 ==
CONTAINER ID   IMAGE     COMMAND   CREATED   STATUS    PORTS     NAMES
```

`ps` reports what is running. To see every container id the lab *declares*,
up or not, use the top-level `--list-hosts`. Any other docker command runs
through the passthrough: `otto host test3 exec "docker ps --format '{{.ID}}'"`.
