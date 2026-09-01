# Host options

Persistent per-host connection tuning, declared in `lab.json`. (For
per-invocation overrides, see {doc}`../cli/host/connections`; for the custom netcat backend,
see {doc}`../cli/host/netcat`.)

(connection-options)=

## Connection options

Every host can be configured with a dedicated options object per network
protocol.  The default-constructed options reproduce otto's historical
defaults exactly, so existing `lab.json` entries keep working without
changes.  To tune a protocol, add the matching ``*_options`` object to
the host entry — one entry of its element's ``hosts`` array, which is
what every JSON fragment on this page shows:

| Object            | Protocol                       |
|-------------------|--------------------------------|
| ``ssh_options``   | SSH sessions                   |
| ``telnet_options``| Telnet sessions                |
| ``sftp_options``  | SFTP transfers                 |
| ``scp_options``   | SCP transfers                  |
| ``ftp_options``   | FTP transfers (aioftp)         |
| ``nc_options``    | see {doc}`../cli/host/netcat`     |

One further table, ``userland_options``, sits alongside these but names no
protocol: it declares facts about the *device* (which elevation mechanism it
has, which ``timeout`` convention its applet speaks) that otto otherwise
probes for once per host.  It layers exactly like the six above, per-call
override included.  `otto host <id> probe` prints the table ready to paste —
see {ref}`userland-capabilities`.  See also {doc}`lab-config`.

The same tables are recognized in four places, layered from least
to most specific:

1. **Hardcoded defaults** in `otto.host.options` — what you get when no
   `*_options` is supplied anywhere.
2. **Per-host `*_options`** in `lab.json` — the lab's own values for
   a single host.
3. **Product `[host_preferences]`** in `.otto/settings.toml` — applied
   to every host whose id matches the selector regex.  Product values
   **win over** `lab.json`.  See {ref}`host-preferences`.
4. **CLI `--term` / `--transfer`** — final word, applied at invocation
   time.

Merging is **per key** between layers (1)–(3).  A host that sets only
`port` in `lab.json` still inherits `connect_timeout` from the
product preference, and so on down to the dataclass default.  The
fully resolved options are baked into the `UnixHost` at construction
time.

For one-off tuning at the call site (e.g. a single test wants a
different port), pass an `*_options=` keyword to `get_host()` /
`all_hosts()`.  See {ref}`per-call-overrides` in the library recipes.

### SSH

Set non-standard port, enable strict host-key checking, and tune the
connect timeout:

```json
{
    "ip": "10.10.200.12",
    "creds": [{ "login": "admin", "password": "secret" }],
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
[connection options recipe](../../library/connection-options.md).

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

Each of these backends reports transfer progress at a fixed stride — the most
the bar may advance between two ticks: 16 KiB for ``sftp`` and ``scp``, 8 KiB
for ``ftp`` and ``nc``.  Only ``scp``'s follows your configuration: whatever
``block_size`` reaches ``asyncssh`` becomes the stride, whether you set it as
the field above or through ``extra``, which is applied last and wins.
{ref}`The support-matrix page <matrix-progress-promises>` lists every backend's
promise and how a run measures it.

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

Netcat has additional options and auto-detection strategies — see {doc}`../cli/host/netcat`.

(per-host-shell-history)=

## Shell history

otto drives a Unix host through a persistent interactive shell, so without
intervention every command it runs is appended to that host's shell history —
burying a human's own history under automation traffic on a shared lab box.

By default otto suppresses this: on each shell it opens it neutralizes
``HISTFILE`` before running anything, so nothing it does reaches
``~/.bash_history``. Set ``shell_history`` to ``true`` on a host where otto's
commands *should* stay visible in the shell's own history (e.g. an audited
box where the history file is the record of what touched it):

```json
{
    "ip": "10.10.200.12",
    "creds": [{ "login": "admin", "password": "secret" }],
    "shell_history": true
}
```

Being a plain host field, it can also be defaulted for a whole class of hosts
from an ``[os_profiles.<name>]`` block in `.otto/settings.toml`, with a
per-host ``lab.json`` value overriding the profile. It is not accepted in
``[host_preferences]``, which takes only the menu-style capabilities
(``term`` / ``transfer`` / ``impairer``).

What suppression covers, and what it deliberately doesn't:

| Path | Suppressed? |
|------|-------------|
| SSH and telnet sessions (`host.run`, named sessions, app shells) | yes |
| Shells entered via a login proxy — `switch_user`, `as_user` | yes; `su` starts a fresh shell that re-reads rc files, so it is re-applied there |
| `host.exec(...)` | not needed — an exec channel has no PTY, and a non-interactive shell keeps no history at all |
| Local host commands | not needed — non-interactive |
| `otto login` | **no**, deliberately — see the caveat below |
| Embedded / Zephyr targets | not applicable — their shell history is a RAM ring buffer, never a file |

```{note}
`otto login` is excluded because it hands *you* a real shell, and silently
losing up-arrow recall would be worse than the noise. The trade-off is not
free: if that login goes through a login proxy (`--user`), otto's own
`__OTTO_…_RECOVER__` resync probe is written into the elevated shell and so
appears in *its* history. The two cannot both be had — suppressing the probe
means suppressing your history for the whole session.
```

Suppression is best-effort and silent by design: every part of it is guarded,
so a shell that refuses all of it keeps working, merely unsuppressed. The
guards are load-bearing rather than decorative — POSIX makes *both* an error
in a special builtin and a failed variable assignment abort the line, either
of which would strand the readiness probe that shares it and take the host
offline. Do not simplify them away.

Notably otto neutralizes ``HISTFILE`` rather than clearing ``HISTSIZE`` —
setting ``HISTSIZE=0`` would make bash write its emptied history list *over*
the history file at exit, destroying the user's real history.

(per-host-snmp)=

## SNMP monitoring block

A host that exposes metrics over SNMP rather than a shell carries an ``snmp``
block ({class}`~otto.host.options.SnmpOptions`) instead of (or alongside) the
``*_options`` transport objects.  The full field reference and a worked example
are under [SNMP monitoring](lab-config.md#snmp-monitoring) in the lab schema;
see {doc}`../cli/monitor/metrics` for what ``otto monitor`` does with the
readings.

(per-host-toolchain)=

## Per-host toolchain

Each host can specify a **toolchain** that tells otto which ``gcov`` and
``lcov`` binaries to use for coverage report generation.  This is
essential when hosts run products built with different cross-compilers.

Add an optional ``toolchain`` object to the host entry in ``lab.json``:

```json
{
    "ip": "10.10.200.12",
    "board": "arm-board",
    "creds": [{ "login": "admin", "password": "secret" }],
    "toolchain": {
        "sysroot": "/opt/arm-toolchain"
    }
}
```

Tool paths (``gcov``, ``lcov``) are resolved **relative to the sysroot**.
The defaults are ``usr/bin/gcov`` and ``usr/bin/lcov``, so setting just
``sysroot`` is sufficient when the toolchain follows the standard layout.

For non-standard layouts, override individual paths.  A host whose
product is built with ``clang --coverage`` points ``gcov`` at an
``llvm-cov`` binary — otto substitutes the one-word ``llvm-cov gcov``
wrapper that ``lcov`` requires at capture time:

```json
{
    "toolchain": {
        "sysroot": "/usr/lib/llvm-18",
        "gcov": "bin/llvm-cov",
        "lcov": "/usr/bin/lcov"
    }
}
```

### Resolution order

With no explicit `toolchain` object, otto resolves the tools in this order:

1. **Explicit config** — the `toolchain` object above.
2. **Auto-discovery** — otto reads the gcov *version stamp* from the build's
   `.gcno` headers (a `.gcno` embeds no compiler path, but every compiler
   stamps the format version it wrote). A clang stamp resolves to `llvm-cov`
   from `PATH`; a GCC stamp means the default `gcov` already applies — a
   *cross*-GCC toolchain cannot be located from the `.gcno` alone and must be
   configured on the host.
3. **System default** — `/usr/bin/gcov` and `/usr/bin/lcov`.

When the resolved tool cannot actually read the build's counters — classically
a clang build captured with GNU `gcov` — the capture stops with a typed error
naming both versions and the fix, instead of producing an empty or wrong
report.


## `nc_options` reference

The `nc_options` object accepts all eight fields of {class}`~otto.host.options.NcOptions`:

| Field                        | Default    | Purpose                                                                   |
|------------------------------|------------|---------------------------------------------------------------------------|
| ``exec_name``                | ``"nc"``   | Netcat binary on both sides (e.g. ``ncat``, ``netcat``).                  |
| ``port``                     | ``9000``   | Base port; used as the scan-start for auto-discovery strategies.          |
| ``port_strategy``            | ``"auto"`` | Strategy for finding a free remote port (see {doc}`../cli/host/netcat`).                |
| ``port_cmd``                 | ``null``   | Shell command printing a free port; used when ``port_strategy="custom"``. |
| ``listener_check``           | ``"auto"`` | Strategy for verifying the remote listener is ready (see {doc}`../cli/host/netcat`).    |
| ``listener_cmd``             | ``null``   | Shell command (``{port}``); exits 0 when ``listener_check="custom"``.     |
| ``listener_timeout``         | ``30.0``   | Seconds otto waits for the remote listener to exit after a transfer ends. |
| ``max_concurrent_transfers`` | ``null``   | Files in flight at once; ``null`` fits a default sshd (see below).        |

``listener_timeout`` is otto's own bound and is not passed to the remote
netcat.  It caps the wait for the listener process to exit once a transfer has
ended, so a port-collision race that leaves a listener servicing someone else
surfaces as a named error instead of a hang.  What ends the remote listener
itself is otto reaping it on every error path, plus the ``timeout`` prefix the
spawn carries — never ``nc -w``, which is not emitted: measured 2026-08-25, it
bounds the idle time of an *accepted* connection and so kills a stalled
transfer with a success code and a partial file.

### What a GET can fetch

The nc backend transfers **regular files**, and transfers exactly the number
of bytes the remote ``stat`` reports — that size is what ends otto's read,
because the spawn carries no netcat option that ends itself.  A symlink is
followed and delivers its target.  A directory or a device file is refused by
name: what ``stat`` reports for those is not what reading them delivers.
procfs and sysfs pseudo-files report a size that is not their content either
(``/proc/version`` reports 0), and nothing in the remote ``stat`` tells them
apart from a genuinely empty file — so they are not refused, they simply
arrive empty.  Use the ``shell`` backend for those.

### Concurrency and the remote channel budget

An nc transfer is not free of SSH channels just because it moves its bytes over
its own TCP connection: otto holds one exec channel for the whole life of the
remote ``nc -l``, and the listener-readiness poll opens another while it is
held. A default OpenSSH server allows ``MaxSessions 10`` channels per
*connection* and **refuses** the excess rather than queueing it, so an
unbounded bulk transfer turns "many files" into ``open failed`` — or into
``Remote nc listener on port N not ready`` when it is the readiness poll that
loses its channel.

otto therefore caps the files in flight per host connection. The default is
derived from the OpenSSH default, leaving headroom for otto's own control
commands. Set ``max_concurrent_transfers`` when the remote sshd is *not*
default — raise it for a host with a raised ``MaxSessions`` to transfer wider,
and lower it for a host with a lowered one, which would otherwise lose files.
otto cannot read the server's setting, so this is the only way to tell it.

```json
{
    "nc_options": { "max_concurrent_transfers": 12 }
}
```

`nc_options` participates in the same layered merge as the other transport
option objects, described at the top of this page.

```json
{
    "nc_options": { "exec_name": "ncat", "port": 9500 }
}
```
