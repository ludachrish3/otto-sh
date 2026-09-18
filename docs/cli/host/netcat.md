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
| ``proc``     | Read ``/proc/net/tcp`` directly (Linux-only, always available).                 |
| ``custom``   | Run the command in ``nc_options.port_cmd``; must print a free port to stdout.   |

The auto cascade order is: ss → netstat → python → proc.

**Listener-check strategies** (``nc_options.listener_check``, default ``auto``):

| Strategy     | How it works                                                                                          |
|--------------|-------------------------------------------------------------------------------------------------------|
| ``auto``     | Probe for ss, then netstat, falling back to proc. Cache the result.                                   |
| ``ss``       | Count LISTEN sockets via ``ss -tln sport = :<port>``.                                                 |
| ``netstat``  | Count the port's lines in ``netstat -tln``.                                                           |
| ``proc``     | Count ``/proc/net/tcp`` rows in LISTEN state on the port (Linux-only, always available).              |
| ``custom``   | Run the command in ``nc_options.listener_cmd`` with ``{port}`` placeholder. Must exit 0 if listening. |

The built-in checks count listeners rather than merely detecting one. Some
netcats (OpenBSD ``nc`` among them) bind with ``SO_REUSEPORT``, so a second
process that chose the same port at the same moment gets a listener beside
otto's instead of an address-in-use error, and the kernel then decides which
of the two receives the connection. A port held by more than one listener is
refused and the file is retried on a fresh port; the scan for that port starts
at a random offset above ``nc_options.port`` so two scanners overlapping in
time rarely agree. The ``custom`` check exits 0 or 1 by its contract and
cannot count, so it remains a presence check.

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
