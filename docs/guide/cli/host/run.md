# otto host run

Execute one or more commands on a remote host with `run`:

```bash
otto --lab my_lab host router1 run "uname -a"
```

Multiple commands run in order.  If any command fails, `otto host run` exits with
a non-zero status:

```bash
otto --lab my_lab host router1 run "cd /tmp" "ls -la"
```

The host's built-in logging displays each command and its output as it runs --
the same output you see inside instructions and test suites. Calling `run` from
Python can narrow that per command; see
[Log modes](../../../library/writing-instructions.md#log-modes).

## `run` options

```text
otto host <HOST_ID> run [OPTIONS] COMMANDS...
```

| Option | Default | Description |
| ------ | ------- | ----------- |
| `COMMANDS...` | — | One or more shell commands (space-separated, each quoted as needed) |
| `--sudo / --no-sudo` | `--no-sudo` | Run every command through `sudo` |
| `--timeout SECS` | `30.0` | Cumulative timeout in seconds across all commands. Must be `>= 0`; pass `inf` for a deliberately unbounded command |
| `--user NAME` | none | Run as this user. Containers only (`docker exec -u`); every other host family refuses. The container's persistent channel binds its user when it opens, so a later `run` naming a different user refuses |

On a unix host `run --user` refuses by design: `run` drives the *persistent*
session, and that session's identity belongs to `as_user` — see
{doc}`capabilities/privilege`. The stateless verbs take a user directly
instead: `put`/`get` accept `--user` ({doc}`put`), and `exec` accepts `user=`
from Python on an `ssh`-term host.
