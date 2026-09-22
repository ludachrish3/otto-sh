# otto host exec

Run one command on a host with `exec`:

```bash
otto --lab my_lab host router1 exec "uname -a"
```

The command is one argument: quote it. Steps join the way the host's shell
joins them — `&&` on a POSIX shell:

```bash
otto --lab my_lab host router1 exec "cd /tmp && ls -la"
```

The host's built-in logging shows the command and its output as it runs.
`exec` exits with the command's own status (see {ref}`host-exit-codes`).

## Running as another user

```bash
otto --lab my_lab host router1 exec "whoami" --user root
```

`--user` names any login the host's creds declare — one with its own
password, or one reached through proxy hops (`via`). What each host family
does with it is declared in {doc}`families`; the Python side of the same
rule is {ref}`host-exec-as`.

## Elevating

```bash
otto --lab my_lab host router1 exec "systemctl restart foo" --sudo
```

`--sudo` elevates through whatever the host resolved (`sudo` or `su`); on a
container it runs the command as root. Families that cannot elevate refuse
and say so.

## `exec` options

```text
otto host <HOST_ID> exec [OPTIONS] COMMAND
```

| Option | Default | Description |
| ------ | ------- | ----------- |
| `COMMAND` | — | One shell command, quoted as one argument |
| `--timeout SECS` | `30.0` | Seconds before the command is abandoned. Must be `>= 0`; `inf` for a deliberately unbounded command |
| `--sudo / --no-sudo` | `--no-sudo` | Elevate |
| `--user NAME` | none | Run as this user; see {doc}`families` |

A second positional argument is a usage error — `exec` takes one command.

## Upgrading from 0.15.0 (Python callers)

The Python `exec`'s positional order changed with this verb: it is now
`exec(cmd, expects, timeout, log, sudo, user)` — `run`'s order minus the
command sequence. A 0.15.0 call of the form `host.exec(cmd, 10.0)` binds the
timeout to `expects` and now raises `TypeError` naming the parameter; pass
`timeout=` by keyword.
