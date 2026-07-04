# Connection options

Each network protocol otto speaks — SSH, Telnet, SFTP, SCP, FTP,
netcat — has a dedicated options dataclass in
[`otto.host.options`](../../src/otto/host/options.py).  The default
constructor of each class reproduces otto's historical behavior, so
dropping an options object onto an existing `UnixHost` never changes
how it connects.

Options can be set in four places, layered lowest-to-highest:

1. **Hardcoded dataclass defaults** in `otto.host.options` — what you
   get when nothing else is configured.
2. **Per-host values** in `hosts.json` under the matching `*_options`
   table — the lab's own definition of what the host needs.
3. **Product preferences** in `.otto/settings.toml` under
   `[host_preferences."<selector>".<protocol>_options]` — applied to
   every host whose id matches the selector regex.  Product values **win
   over** the host's own `hosts.json` values.  Best for conventions
   shared across the whole product.
4. **Per-call overrides** passed to `get_host()` / `all_hosts()` —
   transient, scoped to a single lookup.  Best for one-off test-time
   tuning that shouldn't pollute the lab definition.

Merging between layers (1)–(3) is per-key, so each layer fills in only
the fields it sets.  Layer (4) **replaces** the matching `*_options`
field on the returned host wholesale, so the caller is responsible for
constructing the full options instance they want.

This cookbook collects the recipes you actually reach for: pushing a
common setting up to repo-wide defaults, setting a non-standard port,
disabling host-key checking on a lab host, pinning a port-forward,
switching to FTPS, tuning SCP for slow links, and reaching for a
transient override at a call site.

For the reference table of every field, see the
{ref}`connection options section in the host guide <connection-options>`.

## Product-wide preferences

When every host in a product needs the same handful of tweaks (a longer
SSH connect timeout, a larger telnet column count, an alternate `nc`
binary), set them once in `.otto/settings.toml` under
`[host_preferences]` and let `hosts.json` stay focused on what's
actually different per host.

The selector (e.g. `".*"`) is a Python regex matched against the host
**id**; `".*"` applies to all hosts.  Option tables under each selector
are per-key overrides that **win over** any value in `hosts.json`.

> **Migration note:** `[host_defaults]` was removed; its option tables
> move under `[host_preferences."<selector>".<opt>]`.

```toml
# .otto/settings.toml

# Selector = regex matched against host id; ".*" = all hosts.
# Option tables (ssh_options, …) win over hosts.json values per-key.
[host_preferences.".*"]
ssh_options = { connect_timeout = 5.0, keepalive_interval = 30 }
telnet_options = { cols = 200 }
nc_options = { exec_name = "ncat", port_strategy = "proc" }

# A narrower selector can overlay specific host groups.
[host_preferences."router.*"]
telnet_options = { port = 9023 }
```

Per-host `*_options` tables in `hosts.json` are merged per-key with
the hardcoded defaults first, then the product preferences layer is
applied on top.  A host that sets only `port` in `hosts.json` still
picks up `connect_timeout` from the product preference:

```json
{
    "ip": "10.10.200.20",
    "element": "router",
    "creds": [{ "login": "admin", "password": "secret" }],
    "labs": ["wan"],
    "ssh_options": { "port": 2222 }
}
```

The resolved `SshOptions` for this host has `port=2222`,
`connect_timeout=5.0`, and `keepalive_interval=30`.

When several repos are loaded simultaneously (`OTTO_SUT_DIRS=...`),
their `[host_preferences]` selectors are applied in list order — later
repos overlay earlier ones.  Within a repo, selectors are applied in
definition order (later selector wins on the same key).  This makes one
repo a "base" for shared conventions and others its overlays.

## Non-standard SSH port

```{doctest}
>>> from otto.host import UnixHost
>>> from otto.host.login_proxy import Cred
>>> from otto.host.options import SshOptions
>>> host = UnixHost(
...     ip='10.10.200.12',
...     creds=[Cred(login='admin', password='secret')],
...     element='lab',
...     ssh_options=SshOptions(port=2222),
... )
>>> host.ssh_options.port
2222
```

Equivalent `hosts.json` entry:

```json
{
    "ip": "10.10.200.12",
    "element": "lab",
    "creds": [{ "login": "admin", "password": "secret" }],
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

```{doctest}
>>> from otto.host.options import LocalPortForward, SshOptions
>>> ssh_options = SshOptions(
...     local_forwards=[
...         LocalPortForward(
...             listen_host='localhost', listen_port=8080,
...             dest_host='web.internal', dest_port=80,
...         ),
...     ],
... )
>>> ssh_options.local_forwards[0].dest_port
80
```

After any session on this host opens, `curl localhost:8080` on the
local machine reaches `web.internal:80` through the host.

The same forward expressed in `hosts.json` — `local_forwards`,
`remote_forwards`, and `socks_forwards` are lists of plain dicts whose
keys mirror the dataclass fields:

```json
{
    "ip": "10.10.200.12",
    "element": "lab",
    "creds": [{ "login": "admin", "password": "secret" }],
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

```{doctest}
>>> import ssl
>>> from otto.host.options import FtpOptions
>>> ctx = ssl.create_default_context()
>>> ftp_options = FtpOptions(ssl=ctx, port=990)
>>> ftp_options.port
990
```

Or, for a lab-grade "just accept whatever" context:

```python
ftp_options = FtpOptions(ssl=True)
```

## Tuning SCP block size for slow links

The default 16 KiB block size is fine for most links.  On high-latency
or very high-bandwidth links, a larger block size is a measurable win:

```{doctest}
>>> from otto.host.options import ScpOptions
>>> ScpOptions(block_size=262144).block_size  # 256 KiB
262144
```

## Telnet with live terminal resize

Interactive telnet sessions can opt in to ssh-like window-resize
propagation.  When `auto_window_resize=True` and stdin is a TTY, otto
installs a SIGWINCH handler that sends a NAWS update to the remote
side on every local resize, so remote TUIs reflow:

```{doctest}
>>> from otto.host.options import TelnetOptions
>>> TelnetOptions(auto_window_resize=True).auto_window_resize
True
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

`UnixHost.hop` names another lab host (by host ID) to use as an SSH jump
host: connections to the target are tunneled through the hop's SSH
connection, and file transfers ride the same tunnel.  Hops chain — if the
hop host itself declares a `hop`, the whole chain is tunneled outward-in
(circular chains are detected and rejected).  See
{doc}`Connections in the host guide <../guide/host/connections>` for details.

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
