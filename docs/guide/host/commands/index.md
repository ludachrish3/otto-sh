# Core commands

The four built-in `otto host` verbs: run shell commands, move files in and out,
and open an interactive shell. (For capability verbs like `power` or `ls`, see
{doc}`Host capabilities <../capabilities>`.)

## Running commands

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
the same output you see inside instructions and test suites.

## Uploading files

Transfer local files to a remote host with `put`:

```bash
otto --lab my_lab host router1 put firmware.bin /tmp/
```

Multiple source files are supported:

```bash
otto --lab my_lab host router1 put config.yaml license.key /opt/app/
```

File transfers default to SCP. To use a different backend (SFTP, FTP, or the
custom netcat backend), see {doc}`Connection control <../connections>` for the per-invocation
`--transfer` override and {doc}`netcat` for the netcat backend.

## Downloading files

Retrieve files from a remote host with `get`:

```bash
otto --lab my_lab host router1 get /var/log/syslog ./logs/
```

Multiple remote paths are supported:

```bash
otto --lab my_lab host router1 get /var/log/syslog /var/log/auth.log ./logs/
```

## Interactive login

Open a fully interactive shell on a remote host with `login`:

```bash
otto --lab my_lab host router1 login
```

Stdin and stdout are bridged to the remote terminal in raw mode, so full-screen
TUIs (`vi`, `top`, `less`) work the same as under a native `ssh` or `telnet`
client.  While the session runs, every remote byte is also appended to the
invocation's `session.log` so the transcript is preserved alongside the normal
`otto host run` output.

**Ending the session.**  Exit the remote shell normally (`exit`, `logout`, or
`Ctrl+D`) or press `Ctrl+]` — the classic `telnet(1)` escape byte — to disconnect
locally without waiting on the remote.  The escape hatch exists because `Ctrl+C`
is forwarded to the remote so remote commands can be interrupted the usual way.

**Terminal resize.**  Local `SIGWINCH` is forwarded to the remote PTY on both SSH
(via `window-change` channel request) and telnet (via NAWS subnegotiation), so
remote TUIs reflow on resize.  For telnet, NAWS is enabled automatically for the
`login` command only — non-interactive `run`/`put`/`get` calls keep the historical
fixed column width.

**Hops.**  `login` honors `--hop` and the `hop` field in `hosts.json`, so an
interactive session can tunnel through jump hosts just like the other
subcommands (see {doc}`Connection control <../connections>`):

```bash
otto --lab my_lab host --hop jumpbox router1 login
```

```{toctree}
:hidden:

netcat
```
