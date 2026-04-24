# otto host

`otto host` provides direct access to host operations from the command line
-- running commands, uploading files, downloading files, and opening an
interactive shell -- without writing a test suite or instruction.

## Syntax

The host ID comes before the subcommand, so all host-level options apply
to every action:

```text
otto host <host_id> <command> [ARGS...] [OPTIONS]
```

## Running commands

Execute one or more commands on a remote host with `run`:

```bash
otto --lab my_lab host router1 run "uname -a"
```

Multiple commands run in order.  If any command fails, `otto host run`
exits with a non-zero status:

```bash
otto --lab my_lab host router1 run "cd /tmp" "ls -la"
```

The host's built-in logging displays each command and its output as it
runs -- the same output you see inside instructions and test suites.

## Uploading files

Transfer local files to a remote host with `put`:

```bash
otto --lab my_lab host router1 put firmware.bin /tmp/
```

Multiple source files are supported:

```bash
otto --lab my_lab host router1 put config.yaml license.key /opt/app/
```

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

Stdin and stdout are bridged to the remote terminal in raw mode, so
full-screen TUIs (`vi`, `top`, `less`) work the same as under a native
`ssh` or `telnet` client.  While the session runs, every remote byte is
also appended to the invocation's `otto.log` so the transcript is
preserved alongside the normal `otto host run` output.

**Ending the session.**  Exit the remote shell normally (`exit`,
`logout`, or `Ctrl+D`) or press `Ctrl+]` — the classic `telnet(1)`
escape byte — to disconnect locally without waiting on the remote.  The
escape hatch exists because `Ctrl+C` is forwarded to the remote so
remote commands can be interrupted the usual way.

**Terminal resize.**  Local `SIGWINCH` is forwarded to the remote PTY
on both SSH (via `window-change` channel request) and telnet (via NAWS
subnegotiation), so remote TUIs reflow on resize.  For telnet, NAWS is
enabled automatically for the `login` command only — non-interactive
`run`/`put`/`get` calls keep the historical fixed column width.

**Hops.**  `login` honors `--hop` and the `hop` field in `hosts.json`,
so an interactive session can tunnel through jump hosts just like the
other subcommands:

```bash
otto --lab my_lab host --hop jumpbox router1 login
```

## Reaching hosts through hops

To change the final hop before a target host, use `--hop` to choose an
intermediate SSH jump host:

```bash
otto --lab my_lab host --hop jumpbox target_seed run "uname -a"
```

The hop host must support SSH. The target host can use any terminal
protocol (SSH or telnet) -- otto tunnels the connection through the
hop automatically.

Hops can be chained: if the hop host itself has a `hop` configured,
otto builds a recursive tunnel chain
(`otto -> hop1 -> hop2 -> ... -> target`).  Circular references are
detected and rejected at connection time.

The `--hop` option works with all subcommands:

```bash
otto --lab my_lab host --hop jumpbox target_seed put firmware.bin /tmp/
otto --lab my_lab host --hop jumpbox target_seed get /var/log/syslog ./logs/
```

For persistent hop configuration, set the `hop` field in `hosts.json`:

```json
{
    "ip": "10.10.200.12",
    "ne": "target",
    "board": "seed",
    "hop": "jumpbox_seed",
    "creds": { "admin": "secret" }
}
```

### File transfer protocols through hops

All file transfer protocols work through SSH hops:

- **SCP** (PUT and GET) — native SSH tunnel, no port forwarding needed.
- **SFTP** (PUT and GET) — piggybacks on the tunneled SSH connection.
- **FTP** (PUT and GET) — control and PASV data ports are forwarded
  automatically through the tunnel.
- **Netcat** (PUT and GET) — both directions use SSH port forwarding.
  PUT connects otto to a remote ``nc -l`` listener that receives data.
  GET uses a reversed-listener approach: the remote runs
  ``nc -l <port> < <file>`` and otto connects through the port forward
  to read the data.

### Netcat port and listener strategies

Netcat transfers need two things on the remote host: a **free port** to
listen on, and a way to **verify the listener is ready** before sending
data.  Both use a configurable strategy that defaults to ``auto``.

**Port-finding strategies** (``nc_port_strategy``, default ``auto``):

| Strategy     | How it works                                                          |
|--------------|-----------------------------------------------------------------------|
| ``auto``     | Try each built-in strategy in order and cache the first success.      |
| ``ss``       | Parse ``ss -tln`` output to find unused ports.                        |
| ``netstat``  | Parse ``netstat -tln`` output (fallback for hosts without ss).        |
| ``python``   | Bind a socket to port 0 via a ``python``/``python3`` one-liner.       |
| ``proc``     | Read ``/proc/net/tcp`` directly (Linux-only, always available).       |
| ``custom``   | Run the command in ``nc_port_cmd``; must print a free port to stdout. |

The auto cascade order is: ss → netstat → python → proc.

**Listener-check strategies** (``nc_listener_check``, default ``auto``):

| Strategy     | How it works                                                                                  |
|--------------|-----------------------------------------------------------------------------------------------|
| ``auto``     | Probe for ss, then netstat, falling back to proc. Cache the result.                           |
| ``ss``       | Check for LISTEN via ``ss -tln sport = :<port>``.                                             |
| ``netstat``  | Grep ``netstat -tln`` for the port.                                                           |
| ``proc``     | Scan ``/proc/net/tcp`` for LISTEN state (Linux-only, always available).                       |
| ``custom``   | Run the command in ``nc_listener_cmd`` with ``{port}`` placeholder. Must exit 0 if listening. |

Override the strategy under ``nc_options`` in ``hosts.json`` when
auto-detection isn't appropriate for a particular host:

```json
{
    "ip": "10.10.200.12",
    "ne": "target",
    "board": "seed",
    "transfer": "nc",
    "nc_options": {
        "port_strategy": "proc",
        "listener_check": "proc"
    }
}
```

(connection-options)=

## Connection options

Every host can be configured with a dedicated options object per network
protocol.  The default-constructed options reproduce otto's historical
defaults exactly, so existing `hosts.json` entries keep working without
changes.  To tune a protocol, add the matching ``*_options`` object to
the host entry:

| Object            | Protocol                       |
|-------------------|--------------------------------|
| ``ssh_options``   | SSH sessions                   |
| ``telnet_options``| Telnet sessions                |
| ``sftp_options``  | SFTP transfers                 |
| ``scp_options``   | SCP transfers                  |
| ``ftp_options``   | FTP transfers (aioftp)         |
| ``nc_options``    | Netcat transfers               |

### SSH

Set non-standard port, enable strict host-key checking, and tune the
connect timeout:

```json
{
    "ip": "10.10.200.12",
    "ne": "target",
    "creds": { "admin": "secret" },
    "ssh_options": {
        "port": 2222,
        "known_hosts": "/home/user/.ssh/known_hosts",
        "connect_timeout": 5.0,
        "keepalive_interval": 30
    }
}
```

Anything supported by ``asyncssh.connect()`` but not surfaced as a
curated field is reachable via ``extra``, which is forwarded verbatim:

```json
{
    "ssh_options": {
        "extra": {
            "config": ["/etc/ssh/otto_ssh_config"],
            "proxy_command": "corkscrew proxy 8080 %h %p"
        }
    }
}
```

#### Port forwarding

Structured forwards are declarative and applied right after the
connection opens.  Each list element maps straight to an
``asyncssh.SSHClientConnection.forward_*_port`` call:

```json
{
    "ssh_options": {
        "local_forwards": [
            {"listen_host": "localhost", "listen_port": 8080,
             "dest_host": "web.internal", "dest_port": 80}
        ],
        "remote_forwards": [
            {"listen_host": "", "listen_port": 9000,
             "dest_host": "localhost", "dest_port": 22}
        ],
        "socks_forwards": [
            {"listen_host": "localhost", "listen_port": 1080}
        ]
    }
}
```

For forwards that aren't expressible in JSON (UNIX-socket forwards,
X11, custom subsystems), build the ``SshOptions`` in Python and supply
a ``post_connect`` async hook — see the
[connection options cookbook](../cookbook/connection-options.md).

### Telnet

```json
{
    "telnet_options": {
        "port": 2323,
        "cols": 200,
        "rows": 50,
        "echo_negotiation_timeout": 1.0
    }
}
```

Set ``auto_window_resize`` to ``true`` for interactive telnet sessions
to have otto install a SIGWINCH handler that sends NAWS updates on
every local terminal resize — remote TUIs (``vi``, ``top``, ``less``)
then reflow like they do under SSH.  It defaults to off so that
automated runs produce deterministic output.

### SFTP, SCP, FTP, Netcat

```json
{
    "sftp_options": { "env": { "LANG": "C" } },
    "scp_options":  { "block_size": 65536, "preserve": true },
    "ftp_options":  { "port": 2121, "ssl": true, "socket_timeout": 30 },
    "nc_options":   { "exec_name": "ncat", "port": 9500 }
}
```

Each class also carries an ``extra`` dict that is passed through to the
underlying library (``asyncssh``, ``telnetlib3``, ``aioftp``) for any
option that isn't surfaced as a curated field.

(per-host-toolchain)=

## Per-host toolchain

Each host can specify a **toolchain** that tells otto which ``gcov`` and
``lcov`` binaries to use for coverage report generation.  This is
essential when hosts run products built with different cross-compilers.

Add an optional ``toolchain`` object to the host entry in ``hosts.json``:

```json
{
    "ip": "10.10.200.12",
    "ne": "target",
    "board": "arm-board",
    "creds": { "admin": "secret" },
    "toolchain": {
        "sysroot": "/opt/arm-toolchain"
    }
}
```

Tool paths (``gcov``, ``lcov``) are resolved **relative to the sysroot**.
The defaults are ``usr/bin/gcov`` and ``usr/bin/lcov``, so setting just
``sysroot`` is sufficient when the toolchain follows the standard layout.

For non-standard layouts, override individual paths:

```json
{
    "toolchain": {
        "sysroot": "/opt/llvm-15",
        "gcov": "bin/llvm-gcov-wrapper.sh",
        "lcov": "bin/lcov"
    }
}
```

When no ``toolchain`` is specified, otto uses the system-installed tools
(``/usr/bin/gcov``, ``/usr/bin/lcov``).  Otto can also **auto-discover**
the toolchain from ``.gcno`` files produced during compilation -- see the
[coverage guide](coverage.md) for details.

## Overriding protocol for a single session

Use `--term` to override the terminal protocol and `--transfer` to override
the file transfer protocol for a single invocation without editing `hosts.json`:

```bash
otto --lab my_lab host --term telnet router1 run "show version"
otto --lab my_lab host --transfer sftp router1 put firmware.bin /tmp/
```

Both options can be combined:

```bash
otto --lab my_lab host --term telnet --transfer ftp router1 put config.txt /etc/
```

Valid values:

- `--term`: `ssh`, `telnet`
- `--transfer`: `scp`, `sftp`, `ftp`, `nc`

The override applies only to the current invocation. To persist the change,
update the `term` or `transfer` field in `hosts.json`.

## Listing hosts

Use `--list-hosts` to see which host IDs are available in the loaded lab:

```bash
otto --lab my_lab host --list-hosts
```

This is the same `--list-hosts` option available on the top-level `otto`
command.

## Dry run

Like all otto commands, `--dry-run` (or `-n`) previews what would happen
without executing commands or transferring files:

```bash
otto --lab my_lab --dry-run host router1 run "make install"
```

## Programmatic equivalents

The `otto host` subcommands map directly to methods on the
{class}`~otto.host.host.BaseHost` class.  Everything `otto host` does
from the CLI can also be done inside instructions and test suites:

```{doctest}
>>> host = LocalHost()
>>> result = run(host.run(["echo hello", "echo world"]))
>>> result.status
<Status.Success: 0>
>>> [cs.output.strip() for cs in result.statuses]
['hello', 'world']
```

File transfers work the same way -- `put` and `get` map to
{meth}`~otto.host.remoteHost.RemoteHost.put` and
{meth}`~otto.host.remoteHost.RemoteHost.get`:

```python
from pathlib import Path

# Upload
status, msg = await host.put(
    src_files=[Path("firmware.bin")],
    dest_dir=Path("/tmp"),
)

# Download
status, msg = await host.get(
    src_files=[Path("/var/log/syslog")],
    dest_dir=Path("./logs"),
)
```

```{note}
File transfer methods are only available on
{class}`~otto.host.remoteHost.RemoteHost` instances, not
{class}`~otto.host.localHost.LocalHost`.  The doctest above uses
`run` which is available on all host types.
```
