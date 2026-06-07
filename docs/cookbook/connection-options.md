# Connection options

Each network protocol otto speaks — SSH, Telnet, SFTP, SCP, FTP,
netcat — has a dedicated options dataclass in
[`otto.host.options`](../../src/otto/host/options.py).  The default
constructor of each class reproduces otto's historical behavior, so
dropping an options object onto an existing `UnixHost` never changes
how it connects.

Options can be set in three places, layered lowest-to-highest:

1. **Repo-wide defaults** in `.otto/settings.toml` under
   `[host_defaults.<protocol>_options]` — applied to every host the
   repo touches.  Best for conventions shared across the whole
   product.
2. **Per-host overrides** in `hosts.json` under the matching
   `*_options` table.  Best for the specific exceptions a host needs.
3. **Per-call overrides** passed to `get_host()` / `all_hosts()` —
   transient, scoped to a single lookup.  Best for one-off test-time
   tuning that shouldn't pollute the lab definition.

Merging between (1) and (2) is per-key, so each layer fills in only the
fields it sets.  Layer (3) **replaces** the matching `*_options` field
on the returned host wholesale, so the caller is responsible for
constructing the full options instance they want.

This cookbook collects the recipes you actually reach for: pushing a
common setting up to repo-wide defaults, setting a non-standard port,
disabling host-key checking on a lab host, pinning a port-forward,
switching to FTPS, tuning SCP for slow links, and reaching for a
transient override at a call site.

For the reference table of every field, see the
{ref}`connection options section in the host guide <connection-options>`.

## Repo-wide defaults

When every host in a lab needs the same handful of tweaks (a longer
SSH connect timeout, a larger telnet column count, an alternate `nc`
binary), set them once in `.otto/settings.toml` and let `hosts.json`
stay focused on what's actually different per host:

```toml
# .otto/settings.toml

[host_defaults.ssh_options]
connect_timeout = 5.0
keepalive_interval = 30

[host_defaults.telnet_options]
cols = 200

[host_defaults.nc_options]
exec_name = "ncat"
port_strategy = "proc"
```

Per-host `*_options` tables in `hosts.json` are merged on top
**per-key**, so a host that only needs to override `port` keeps the
repo's `connect_timeout`:

```json
{
    "ip": "10.10.200.20",
    "ne": "router",
    "creds": { "admin": "secret" },
    "labs": ["wan"],
    "ssh_options": { "port": 2222 }
}
```

The resolved `SshOptions` for this host has `port=2222`,
`connect_timeout=5.0`, and `keepalive_interval=30`.

When several repos are loaded simultaneously (`OTTO_SUT_DIRS=...`),
their `[host_defaults]` tables are reduced in list order — later repos
overlay earlier ones field-by-field.  This makes one repo a "base"
for shared conventions and others its overlays.

## Non-standard SSH port

```python
from otto.host import UnixHost
from otto.host.options import SshOptions

host = UnixHost(
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

(per-call-overrides)=

## Per-call overrides on `get_host()` / `all_hosts()`

For genuinely transient overrides — a single test that needs to
connect on a different port, a one-off command run with a tighter
timeout — pass an `*_options=` keyword to `get_host()` or
`all_hosts()` instead of editing the lab definition:

```python
from otto.configmodule import get_host, all_hosts
from otto.host.options import SshOptions, TelnetOptions

# A single host with a one-off SSH override.
host = get_host("router1", ssh_options=SshOptions(
    port=9999,
    connect_timeout=2.0,
))

# Every host yielded by all_hosts() with a wider telnet window.
for h in all_hosts(telnet_options=TelnetOptions(cols=300)):
    ...
```

A returned override host is a fresh `dataclasses.replace`-style copy
of the stored host whose `__post_init__` has re-run, so its
`ConnectionManager` is constructed with the override options from the
start.  This is required because protocol options shape the
connection itself (key algorithms, hop wiring, etc.) and cannot be
swapped on an already-open connection — the override copy opens its
own connection on first use, and the stored host (and any connection
it owns) is untouched.

When no `*_options=` kwarg is passed, `get_host()` returns the stored
instance unchanged so identity (`get_host("x") is get_host("x")`) is
preserved for non-override callers.

The same kwargs are accepted by `do_for_all_hosts()` and
`run_on_all_hosts()`:

```python
from otto.configmodule import run_on_all_hosts
from otto.host.options import SshOptions

results = await run_on_all_hosts(
    "uname -a",
    ssh_options=SshOptions(connect_timeout=10.0),
)
```

```{note}
Per-call overrides **replace** the corresponding `*_options` field
wholesale on the returned copy.  If you want to keep some of the
stored or repo-default values, construct the full `SshOptions` (etc.)
instance you want — there is no per-key merge at this layer.
```

```{note}
Hop hosts (`UnixHost.hop`) are resolved internally via `get_host()`.
A per-call override on the parent host does **not** flow into hop
resolution.
```

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
