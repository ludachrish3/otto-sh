# Netcat transfers

Netcat (`nc`) is otto's most customizable file-transfer backend. Unlike SCP/SFTP/
FTP — standard tools otto drives directly — the netcat backend has to *find a free
port* on the remote and *verify the listener is ready*, both with configurable,
auto-detecting strategies. This page collects everything netcat-specific.

Select it per invocation with `--transfer nc` (see {doc}`Connection control <../connections>`) or
persist it with `"transfer": "nc"` in `hosts.json`.

## Through hops

Netcat (PUT and GET) works through SSH hops using SSH port forwarding. PUT
connects otto to a remote ``nc -l`` listener that receives data. GET uses a
reversed-listener approach: the remote runs ``nc -l <port> < <file>`` and otto
connects through the port forward to read the data. (The other transfer protocols
through hops are covered in {doc}`Connection control <../connections>`.)

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
| ``ss``       | Check for LISTEN via ``ss -tln sport = :<port>``.                                                     |
| ``netstat``  | Grep ``netstat -tln`` for the port.                                                                   |
| ``proc``     | Scan ``/proc/net/tcp`` for LISTEN state (Linux-only, always available).                               |
| ``custom``   | Run the command in ``nc_options.listener_cmd`` with ``{port}`` placeholder. Must exit 0 if listening. |

Override the strategy under ``nc_options`` in ``hosts.json`` when auto-detection
isn't appropriate for a particular host:

```json
{
    "ip": "10.10.200.12",
    "element": "target",
    "board": "seed",
    "transfer": "nc",
    "nc_options": {
        "port_strategy": "proc",
        "listener_check": "proc"
    }
}
```

## `nc_options` reference

The `nc_options` object accepts all seven fields of {class}`~otto.host.options.NcOptions`:

| Field                | Default    | Purpose                                                                      |
|----------------------|------------|------------------------------------------------------------------------------|
| ``exec_name``        | ``"nc"``   | Netcat binary on both sides (e.g. ``ncat``, ``netcat``).                     |
| ``port``             | ``9000``   | Base port; used as the scan-start for auto-discovery strategies.             |
| ``port_strategy``    | ``"auto"`` | Strategy for finding a free remote port (see table above).                   |
| ``port_cmd``         | ``null``   | Shell command printing a free port; used when ``port_strategy="custom"``.    |
| ``listener_check``   | ``"auto"`` | Strategy for verifying the remote listener is ready (see table above).       |
| ``listener_cmd``     | ``null``   | Shell command (``{port}``); exits 0 when ``listener_check="custom"``.        |
| ``listener_timeout`` | ``30.0``   | Seconds the remote listener waits for a client before self-terminating.      |

``listener_timeout`` is passed as ``nc -w`` on the remote side and also caps the
post-transfer wait for the listener process to exit.  It prevents an
orphaned-listener hang when a port-collision race causes the listener to never
receive a client — without it the listener would block indefinitely.

`nc_options` participates in the same layered merge as the other transport option
objects — see {doc}`Host configuration <../configuration>`.

```json
{
    "nc_options": { "exec_name": "ncat", "port": 9500 }
}
```
