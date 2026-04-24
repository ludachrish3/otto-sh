# Connection options

Each network protocol otto speaks — SSH, Telnet, SFTP, SCP, FTP,
netcat — has a dedicated options dataclass in
[`otto.host.options`](../../src/otto/host/options.py).  The default
constructor of each class reproduces otto's historical behavior, so
dropping an options object onto an existing `RemoteHost` never changes
how it connects.

This cookbook collects the recipes you actually reach for: setting a
non-standard port, disabling host-key checking on a lab host, pinning a
port-forward, switching to FTPS, and tuning SCP for slow links.

For the reference table of every field, see the
{ref}`connection options section in the host guide <connection-options>`.

## Non-standard SSH port

```python
from otto.host import RemoteHost
from otto.host.options import SshOptions

host = RemoteHost(
    ip='10.10.200.12',
    creds={'admin': 'secret'},
    ne='lab',
    ssh_options=SshOptions(port=2222),
)
```

Equivalent `hosts.json` entry:

```json
{
    "ip": "10.10.200.12",
    "ne": "lab",
    "creds": { "admin": "secret" },
    "ssh_options": { "port": 2222 }
}
```

## Disabling strict host-key checking for a lab host

Otto's default already disables host-key checking (that's the historical
behavior).  For a host where you *do* want to check:

```python
ssh_options=SshOptions(
    known_hosts='/home/engineer/.ssh/known_hosts',
)
```

…and for a host where you explicitly want the default *off* behavior
surfaced in config (for readers of the JSON file):

```json
{
    "ssh_options": { "known_hosts": null }
}
```

## Persistent local port forward through a host

Open a tunnel to an internal web service every time the SSH
connection is created:

```python
from otto.host.options import LocalPortForward, SshOptions

ssh_options = SshOptions(
    local_forwards=[
        LocalPortForward(
            listen_host='localhost', listen_port=8080,
            dest_host='web.internal', dest_port=80,
        ),
    ],
)
```

After any session on this host opens, `curl localhost:8080` on the
local machine reaches `web.internal:80` through the host.

The same forward expressed in `hosts.json` — `local_forwards`,
`remote_forwards`, and `socks_forwards` are lists of plain dicts whose
keys mirror the dataclass fields:

```json
{
    "ip": "10.10.200.12",
    "ne": "lab",
    "creds": { "admin": "secret" },
    "ssh_options": {
        "local_forwards": [
            {
                "listen_host": "localhost", "listen_port": 8080,
                "dest_host": "web.internal", "dest_port": 80
            }
        ],
        "remote_forwards": [
            {
                "listen_host": "", "listen_port": 9000,
                "dest_host": "localhost", "dest_port": 22
            }
        ],
        "socks_forwards": [
            { "listen_host": "localhost", "listen_port": 1080 }
        ]
    }
}
```

Each entry maps directly onto an asyncssh `forward_*_port` call when
the SSH connection is established. The forwards persist for the
lifetime of the connection and tear down automatically when the host
closes.

## FTPS instead of plain FTP

```python
import ssl
from otto.host.options import FtpOptions

ctx = ssl.create_default_context()
ftp_options = FtpOptions(ssl=ctx, port=990)
```

Or, for a lab-grade "just accept whatever" context:

```python
ftp_options = FtpOptions(ssl=True)
```

## Tuning SCP block size for slow links

The default 16 KiB block size is fine for most links.  On high-latency
or very high-bandwidth links, a larger block size is a measurable win:

```python
from otto.host.options import ScpOptions

scp_options = ScpOptions(block_size=262144)  # 256 KiB
```

## Telnet with live terminal resize

Interactive telnet sessions can opt in to ssh-like window-resize
propagation.  When `auto_window_resize=True` and stdin is a TTY, otto
installs a SIGWINCH handler that sends a NAWS update to the remote
side on every local resize, so remote TUIs reflow:

```python
from otto.host.options import TelnetOptions

telnet_options = TelnetOptions(auto_window_resize=True)
```

Leave it off (the default) for automated runs so captured output stays
deterministic regardless of the controlling terminal's width.

## Escape hatch: the `post_connect` hook

For anything that isn't a kwarg on `asyncssh.connect()` or a standard
port forward — UNIX-socket forwards, X11, custom subsystems — build
the `SshOptions` in Python and supply a `post_connect` coroutine:

```python
from asyncssh import SSHClientConnection
from otto.host.options import SshOptions

async def setup(conn: SSHClientConnection) -> None:
    await conn.forward_local_path('/tmp/docker.sock', '/var/run/docker.sock')

ssh_options = SshOptions(post_connect=setup)
```

The hook runs on a freshly opened connection, right after the
structured forwards have been applied, before the session is handed to
any caller.  It can't be expressed in JSON, so hosts that need it must
be constructed in Python rather than loaded from `hosts.json`.
