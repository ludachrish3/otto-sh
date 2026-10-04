# otto docker logs

Print one container's `docker logs`, exactly as docker printed it.

```text
otto docker logs CONTAINER [--tail N] [--since T] [-t|--timestamps] [-f|--follow] [--parent HOST]
```

`CONTAINER` is a docker container name or id. It goes to `docker logs` on the
parent verbatim: a container that has exited still gives up its logs, and a
name the daemon does not know is docker's error, printed as docker printed it:
otto exits with docker's own status (1 for that one). The parent is chosen as
{ref}`Which host <docker-which-host>` describes.

| Option | Description |
| ------ | ----------- |
| `CONTAINER` (argument) | A docker container name or id on the parent |
| `--tail N` | Number of lines from the end of the log (docker's `--tail`) |
| `--since T` | Logs since a timestamp or a relative time such as `10m` (docker's `--since`) |
| `-t`, `--timestamps` | Show timestamps (docker's `-t`) |
| `-f`, `--follow` | Follow the log live; Ctrl-C ends it. Needs an SSH parent (a telnet-reached host is refused, exit 2) |
| `--parent HOST` | The docker-capable host `CONTAINER` is a docker name or id on (default: {ref}`Which host <docker-which-host>`) |

TAB offers the container names and ids a verb saw on the parent in the last 15
minutes ([Completion](index.md#completion)).

Nothing is added to docker's output: one container on one host needs no
`== <host-id> ==` header, so the lines are docker's alone. Library:
{func}`~otto.docker.container_logs`.

An illustrative transcript — your container's lines are what you will see:

```text
$ otto docker logs unix-integration-1a2b3c4d-web-1 --tail 2
listening on :8080
ready

$ otto docker logs unix-integration-1a2b3c4d-web-1 --parent test3 --since 10m -t
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
