# Host configuration (hosts.json)

Persistent per-host connection tuning, declared in `hosts.json`. (For
per-invocation overrides, see {doc}`connections`; for the custom netcat backend,
see {doc}`commands/netcat`.)

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
| ``nc_options``    | see {doc}`commands/netcat`     |

The same six tables are recognized in four places, layered from least
to most specific:

1. **Hardcoded defaults** in `otto.host.options` — what you get when no
   `*_options` is supplied anywhere.
2. **Per-host `*_options`** in `hosts.json` — the lab's own values for
   a single host.
3. **Product `[host_preferences]`** in `.otto/settings.toml` — applied
   to every host whose id matches the selector regex.  Product values
   **win over** `hosts.json`.  See {ref}`host-preferences`.
4. **CLI `--term` / `--transfer`** — final word, applied at invocation
   time.

Merging is **per key** between layers (1)–(3).  A host that sets only
`port` in `hosts.json` still inherits `connect_timeout` from the
product preference, and so on down to the dataclass default.  The
fully resolved options are baked into the `UnixHost` at construction
time.

For one-off tuning at the call site (e.g. a single test wants a
different port), pass an `*_options=` keyword to `get_host()` /
`all_hosts()`.  See {ref}`per-call-overrides` in the cookbook.

### SSH

Set non-standard port, enable strict host-key checking, and tune the
connect timeout:

```json
{
    "ip": "10.10.200.12",
    "element": "target",
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
[connection options cookbook](../../cookbook/connection-options.md).

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

**Embedded / UART-backed consoles** — four extra fields matter when the
telnet endpoint is a QEMU ``-serial telnet:`` bridge rather than a Unix
telnetd ({class}`~otto.host.options.TelnetOptions`):

- ``write_chunk_size`` (default ``0``) — split each command write into
  chunks of at most this many bytes.  ``0`` sends the whole payload in
  one call (correct for a host-terminated shell).  Set a positive value
  (e.g. ``64``) for a UART-backed RTOS shell that overruns its console
  RX FIFO on a multi-KB ``llext load_hex`` line.
- ``write_chunk_delay`` (default ``0.0``) — seconds to pause between
  chunks; ignored when ``write_chunk_size`` is ``0``.
- ``login`` (default ``true``) — set ``false`` for a bare-metal / RTOS
  shell with no login step; otherwise otto waits for a ``login:`` prompt
  that never arrives and hangs the connection.
- ``login_prompt`` (default ``":"`` byte) — byte delimiter that ends the
  login / password prompts.  The default matches ``login:``,
  ``Username:``, ``Password:``, etc.
- ``single_client_console`` (default ``false``) — set ``true`` when the
  endpoint is a single-client console (e.g. Zephyr ``shell_telnet``).
  Otto registers the transport so the embedded teardown can force-release
  the slot if a timed-out test left it half-open.  Leave ``false`` for
  ordinary multi-session telnetd.

### SFTP, SCP, FTP, Netcat

```json
{
    "sftp_options": { "env": { "LANG": "C" } },
    "scp_options":  { "block_size": 65536, "preserve": true },
    "ftp_options":  { "port": 2121, "ssl": true, "socket_timeout": 30 }
}
```

``SftpOptions``, ``ScpOptions``, and ``FtpOptions`` each carry an ``extra``
dict that is forwarded verbatim to the underlying library (``asyncssh``,
``aioftp``) for any option not surfaced as a curated field.  ``NcOptions``
has no ``extra`` — all netcat knobs are curated fields.

**Notable SFTP fields** ({class}`~otto.host.options.SftpOptions`): ``env``
sets remote environment variables; ``send_env`` forwards named local
variables to the remote SFTP process.

**Notable SCP fields** ({class}`~otto.host.options.ScpOptions`): ``recurse``
(default ``true``) controls directory recursion — set it ``false`` to
transfer a single file without descending; ``preserve`` carries mtime/atime/mode.

**Notable FTP fields** ({class}`~otto.host.options.FtpOptions`): beyond
``port``, ``ssl``, and ``socket_timeout``, the impactful knobs are
``connection_timeout`` (handshake), ``path_timeout`` (list/stat),
``read_speed_limit`` / ``write_speed_limit`` (bytes/sec caps, ``null`` =
unlimited), and ``passive_commands`` (default ``["epsv", "pasv"]``).

Netcat has additional options and auto-detection strategies — see {doc}`commands/netcat`.

(per-host-snmp)=

## SNMP monitoring block

A host that exposes metrics over SNMP rather than a shell carries an ``snmp``
block ({class}`~otto.host.options.SnmpOptions`) instead of (or alongside) the
``*_options`` transport objects.  The full field reference and a worked example
are in {doc}`../monitor` — see the *Configuring the snmp block in hosts.json*
section.

(per-host-toolchain)=

## Per-host toolchain

Each host can specify a **toolchain** that tells otto which ``gcov`` and
``lcov`` binaries to use for coverage report generation.  This is
essential when hosts run products built with different cross-compilers.

Add an optional ``toolchain`` object to the host entry in ``hosts.json``:

```json
{
    "ip": "10.10.200.12",
    "element": "target",
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
[coverage guide](../coverage.md) for details.
