# Netcat transfers

Netcat (`nc`) is otto's most customizable file-transfer backend. Unlike SCP/SFTP/
FTP — standard tools otto drives directly — the netcat backend has to *find a free
port* on the remote and *verify the listener is ready*, both with configurable,
auto-detecting strategies. This page collects everything netcat-specific.

Select it per invocation with `--transfer nc` (see {doc}`Connection control <connections>`) or
persist it with `"transfer": "nc"` in `lab.json`.

## Through hops

Netcat (PUT and GET) works through SSH hops using SSH port forwarding. PUT
connects otto to a remote ``nc -l`` listener that receives data. GET uses a
reversed-listener approach: the remote runs ``nc -l <port> < <file>`` and otto
connects through the port forward to read the data. (The other transfer protocols
through hops are covered in {doc}`Connection control <connections>`.)

## Port and listener strategies

Netcat transfers need two things on the remote host: a **free port** to listen on,
and a way to **verify the listener is ready** before sending data.  Both use a
configurable strategy that defaults to ``auto``.

**Port-finding strategies** (``nc_options.port_strategy``, default ``auto``):

| Strategy     | How it works                                                                    |
|--------------|---------------------------------------------------------------------------------|
| ``auto``     | Try each built-in strategy in order and cache the first success.                |
| ``ss``       | Parse ``ss -tln`` output to find unused ports.                                  |
| ``netstat``  | Parse ``netstat -tln`` output (fallback for hosts without ss).                  |
| ``python``   | Bind a socket to port 0 via a ``python``/``python3`` one-liner.                 |
| ``proc``     | Read ``/proc/net/tcp`` and ``tcp6`` directly (Linux-only; the last resort).     |
| ``custom``   | Run the command in ``nc_options.port_cmd``; must print a free port to stdout.   |

The auto cascade order is: ss → netstat → python → proc.

**Listener-check strategies** (``nc_options.listener_check``, default ``auto``):

| Strategy     | How it works                                                                                          |
|--------------|-------------------------------------------------------------------------------------------------------|
| ``auto``     | Probe for ss, then netstat, falling back to proc. Cache the result.                                   |
| ``ss``       | Count LISTEN sockets via ``ss -tln sport = :<port>``, asked per address family (``-4``, ``-6``).      |
| ``netstat``  | Count the port's lines in ``netstat -tln``.                                                           |
| ``proc``     | Count ``/proc/net/tcp`` and ``tcp6`` rows in LISTEN state on the port (Linux-only; the last resort).  |
| ``custom``   | Run the command in ``nc_options.listener_cmd`` with ``{port}`` placeholder. Must exit 0 if listening. |

The built-in checks count listeners rather than merely detecting one. A port
held by more than one listener is refused and the file is retried on a fresh
port; the scan for that port starts at a random offset above
``nc_options.port`` so two scanners overlapping in time rarely agree. IPv4
and IPv6 listeners are counted apart and the larger count is the answer, so
a netcat that binds one socket of each family for its port, as ``ncat``
does, is one listener, and two listeners of one family share the port. The
``custom`` check exits 0 or 1 by its contract and cannot count, so it remains
a presence check.

Override the strategy under ``nc_options`` on the host entry in ``lab.json``
when auto-detection isn't appropriate for a particular host:

```json
{
    "ip": "10.10.200.12",
    "board": "seed",
    "transfer": "nc",
    "nc_options": {
        "port_strategy": "proc",
        "listener_check": "proc"
    }
}
```

The full option list is in {doc}`../../configuration/host-options`.

### Choosing a strategy by measurement

`otto host <id> probe` runs the strategies on the host and prints the
`nc_options` fragment to paste; see
{doc}`Netcat strategies in the protocol survey <capabilities/protocols>`.

### When a strategy fails

Most hosts never see a failure: ``auto`` picks a tool that works and keeps
using it. A kernel built without IPv6 is supported too: its tools complain
that ``/proc/net/tcp6`` is missing but still answer, and otto ignores the
complaint.

When a port-finding strategy fails under ``auto``, otto warns once and tries
the next one in the order. When a strategy that worked earlier fails later,
otto tries every other strategy in the order, so one that failed before and
works now is used again. The warning names the strategy and the option that
pins another:

```text
<host>: the netcat port finder's 'ss' rung failed (<error>); trying the next rung. Set nc_options.port_strategy to pin another.
```

If every strategy fails, the error reads ``All port-finding strategies failed
on <host>: ...`` and ends ``Set nc_options.port_strategy to pin one.``

When you pin a port strategy and it fails, the transfer fails with an error
that names the strategy, for example ``The netcat port finder's pinned 'ss'
rung failed on <host>: ...``. otto does not try another strategy behind your
back.

The listener check never falls back to another strategy, even under ``auto``.
If its tool fails, the transfer stops at once with an error that names the
strategy and ``nc_options.listener_check``. If the check prints something
that is not a count, the attempt fails with an error that quotes what came
back.

otto waits for the remote listener and then connects to it. A failure names
the stage it happened in, and the connect's stage depends on the route:

- ``remote readiness``: the listener check has not seen the listener yet.
- ``connect``: otto could not connect to the listener directly.
- ``forwarded connect``: otto could not connect to the listener through an SSH
  port forward.

Each message names the host, the remote port and the listener check, and says
whether the listener process was still running. A ``forwarded connect``
message also names the forward's local end, ``localhost:<port>``.

When the netcat command ends before the transfer starts, otto stops waiting at
once and reports how it ended. A put to ``/srv/data/payload.bin`` whose netcat
is not on the host reports this, after the local file's path ``<src>``:

```text
<src>: nc put to /srv/data/payload.bin: the listener on <host> port <port> ended before the transfer started: 'nc' exited with status 127, so it was not found on the host (nc_options.exec_name names the netcat to run)
```

The status is the whole remote command line's, not only the netcat's. Status
127 means the netcat in ``nc_options.exec_name`` is not on the host, and 126
that it is there but cannot be run; only these two point at
``nc_options.exec_name``. Any other status is reported as the listener's (or
the sender's) command ending with that status, naming the remote file. It
usually means the command's redirect failed before the netcat ran: the
destination directory does not exist or cannot be written, or the source file
cannot be read. An old BusyBox ``timeout`` (1.16.1 on otto's own test bed)
reports a missing program as status 1, not 127, so on such a host the message
does not say "not found" and otto tries twice. On a direct get, where the host
sends to otto, the message says the sender ended without connecting. A command
that failed, or gave no exit status, is reported with its cause.

A failed attempt (a timeout, a port another listener shares, an answer that is
not a count, or a netcat that ended early with any other status) is retried
once on a fresh port, and the message then ends ``(two attempts were made)``.
A failure a second attempt cannot change ends the file without a retry: a
listener check whose tool failed, a netcat that exited with status 126 or 127,
a pinned port strategy that failed, every port strategy failing, and a file
the backend cannot send.
