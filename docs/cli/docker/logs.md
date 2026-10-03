# otto docker logs

Print one container's `docker logs`, exactly as docker printed it.

```text
otto docker logs CONTAINER [--tail N] [--since T] [-t|--timestamps] [-f|--follow] [--on HOST]
```

`CONTAINER` is read one of two ways. A **lab container host id**
(`test3.integration.web`, as `otto host --list-hosts` shows it): otto finds the
container on that host's docker daemon by its compose project and service
labels, running or stopped, so a container that has exited still gives up its
logs. With **`--on HOST`**, `CONTAINER` is a docker container name or id and
goes to `docker logs` on that host verbatim; a name the daemon does not know is
docker's error, printed as docker printed it, and otto exits with docker's own
status (1 for that one).

| Option | Description |
| ------ | ----------- |
| `CONTAINER` (argument) | A container host id, or with `--on` a docker container name or id |
| `--tail N` | Number of lines from the end of the log (docker's `--tail`) |
| `--since T` | Logs since a timestamp or a relative time such as `10m` (docker's `--since`) |
| `-t`, `--timestamps` | Show timestamps (docker's `-t`) |
| `-f`, `--follow` | Follow the log live; Ctrl-C ends it. Needs an SSH parent (a telnet-reached host is refused, exit 2) |
| `--on HOST` | The docker-capable host `CONTAINER` is a docker name or id on |

Nothing is added to docker's output: one container on one host needs no
`== <host-id> ==` header, so the lines are docker's alone. A container host id
that names no container on its parent (the stack was never brought up, or was
removed) is refused, naming both. A `CONTAINER` that is neither a container
host id nor accompanied by `--on` is refused with the container host ids the
lab declares. Library: {func}`~otto.docker.container_logs`.

An illustrative transcript — your container's lines are what you will see:

```text
# by container host id
$ otto docker logs test3.integration.web --tail 2
listening on :8080
ready

# by docker name, on a named host
$ otto docker logs unix-integration-1a2b3c4d-web-1 --on test3 --since 10m -t
2026-10-03T12:00:01.000000000Z listening on :8080
```

`--follow` runs the same `docker logs -f` in the foreground on the host's SSH
connection, the way {doc}`../host/login` bridges a terminal: lines arrive as
docker writes them and the transcript lands in `session.log`. It prints no
report afterwards, and `-n` stops before it like every docker verb. A follow
ends when docker's process ends, on Ctrl-C from a terminal, on end of input on
stdin (so `</dev/null` or a non-interactive runner returns at once), or on
Ctrl+]. It runs on a PTY, so docker's stderr is merged into the stream and
lines end in CRLF. otto exits with what docker reports: its own status when
it ended on its own (a container docker does not know prints docker's error
and exits 1), and `128` plus the signal when a signal ended it, so Ctrl-C ends
with 130 as in any shell. Only Ctrl+] and end of input on stdin end with 0.
For any other `docker logs` flag, use the passthrough:
`otto host test3 exec "docker logs --details <container>"`. For a whole
use-case's logs at once, see {doc}`compose/logs`.
