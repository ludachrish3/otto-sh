# Lab Configuration

A *lab* is a set of hosts described in one or more `hosts.json` files.  Each
file lists every host in a given location; each host entry declares which lab
names it belongs to, so a single file can serve multiple labs and a host can
belong to more than one lab at once.  This page is the full per-host schema
reference.  For repo-level settings (paths, libs, init modules) see
{doc}`repo-setup`.

## Lab files

Each directory listed under the `labs` key in `.otto/settings.toml` may
contain a `hosts.json` file.  The file is a JSON **array** of host objects:

```json
[
    {
        "ip": "10.10.200.11",
        "element": "carrot",
        "board": "seed",
        "creds": { "vagrant": "vagrant" },
        "labs": ["veggies"]
    },
    {
        "ip": "192.0.2.1",
        "element": "sprout",
        "board": "seed",
        "os_type": "zephyr",
        "labs": ["embedded"]
    }
]
```

Each entry carries a `labs` field listing the lab names it belongs to.  Pass
`--lab veggies` (or set `OTTO_LAB=veggies`) and otto loads every host whose
`labs` field includes `"veggies"`.

The host **id** used by `get_host()`, `--list-hosts`, and the rest of the CLI
is derived from the `element`, `board`, and `element_id` fields.  `carrot` with board
`seed` becomes `carrot_seed`; `carrot` with board `seed` and `element_id` `2`
becomes `carrot_seed_2`.

## Per-host fields

### Required

| Field | Type | Description |
|-------|------|-------------|
| `ip` | string | IP address or DNS name otto will connect to. |
| `element` | string | Network-element name.  Combined with `board` and `element_id` to form the host id. |
| `creds` | object | Map of username → password.  At least one entry required for Unix hosts; optional for embedded hosts (RTOS telnet shells typically have no login step). |
| `labs` | array of strings | Lab names this host belongs to.  Without this the host is invisible to `--lab`. |

### Common optional

| Field | Type | Description |
|-------|------|-------------|
| `board` | string | Board type, included in the host id when set. |
| `element_id` | integer | Disambiguates multiple instances of the same NE; appended to the host id. |
| `user` | string | Pin a specific user from `creds`.  Defaults to the first entry. |
| `term` | string | Terminal protocol — `"ssh"` (default) or `"telnet"`. |
| `transfer` | string | File-transfer protocol — `"scp"` (default), `"sftp"`, `"ftp"`, or `"nc"` for Unix hosts; `"console"` (default) or `"tftp"` for embedded hosts. |
| `hop` | string | Host id of an intermediate SSH jump host.  Otto opens an SSH tunnel through it and routes all subsequent connections automatically.  Hops can chain. |
| `resources` | array of strings | Free-form resource tags used by the reservation backend. |
| `is_virtual` | boolean | `true` when the host is a VM or emulator. |
| `log` | boolean | Whether to log output to stdout and log files (default `true`). |
| `log_stdout` | boolean | Whether to log output to stdout (default `true`).  Setting `log` to `false` overrides this. |
| `docker_capable` | boolean | `true` when this host can run Docker containers (Unix hosts only). |

### Host type / OS

| Field | Type | Description |
|-------|------|-------------|
| `os_type` | string | Profile selector.  Defaults to `"unix"`.  Resolves to a registered host class and optional defaults bundle — see {doc}`os-profiles`. |
| `os_name` | string | Human-readable OS name (e.g. `"Linux"`, `"Zephyr"`). |
| `os_version` | string | OS or kernel version string (e.g. `"3.7"`, `"4.4"`). |

### Embedded-only fields

These fields apply only to hosts with an embedded base type (e.g.
`os_type: "zephyr"` or `os_type: "embedded"`).  See {doc}`embedded` for full
details.

| Field | Type | Description |
|-------|------|-------------|
| `command_frame` | string | Shell-framing dialect (e.g. `"zephyr"`, `"zephyr-serial"`). |
| `filesystem` | string | On-device filesystem variant (`"none"`, `"fat-ram"`, `"littlefs"`). |
| `max_filename_len` | integer | Maximum filename length accepted by the target filesystem. |

File transfer for embedded hosts uses `"console"` or `"tftp"` — see
{doc}`embedded`.

### SNMP monitoring

The optional `snmp` block configures SNMP polling for a host's metrics.  See
the SNMP section of {doc}`monitor`.

| Field | Type | Description |
|-------|------|-------------|
| `snmp.address` | string | SNMP agent IP address. |
| `snmp.port` | integer | SNMP UDP port. |
| `snmp.community` | string | SNMP community string. |
| `snmp.oids` | array of strings | OIDs to poll. |

### Per-protocol option tables

Each of the following keys accepts an object that overrides individual
protocol fields.  They merge per-key with repo-level `[host_defaults]`; the
host's own values win.  See {doc}`host` for the full connection-options
reference.

| Key | Protocol |
|-----|----------|
| `ssh_options` | SSH (term and hop) |
| `telnet_options` | Telnet (term, and the embedded console) |
| `sftp_options` | SFTP transfer |
| `scp_options` | SCP transfer |
| `ftp_options` | FTP transfer |
| `nc_options` | Netcat transfer |

### Coverage toolchain

The `toolchain` object points to the cross-toolchain binaries used by the
coverage pipeline.  See {doc}`coverage`.

| Field | Type | Description |
|-------|------|-------------|
| `toolchain.sysroot` | string | Path to the cross-toolchain sysroot. |
| `toolchain.gcov` | string | Path to `gcov` relative to `sysroot`, or an absolute path. |
| `toolchain.lcov` | string | Path to the `lcov` binary. |

## Example

Two real entries from the test fixture — one Unix host and one Zephyr host:

```json
[
    {
        "ip": "10.10.200.11",
        "element": "carrot",
        "os_type": "unix",
        "board": "seed",
        "term": "ssh",
        "transfer": "scp",
        "is_virtual": true,
        "creds": {
            "vagrant": "vagrant",
            "test": "Password1"
        },
        "resources": [
            "carrot"
        ],
        "labs": [
            "veggies"
        ]
    },
    {
        "ip": "192.0.2.1",
        "element": "sprout",
        "os_type": "zephyr",
        "os_version": "3.7",
        "transfer": "console",
        "filesystem": "fat-ram",
        "max_filename_len": 32,
        "is_virtual": true,
        "hop": "basil_seed",
        "snmp": {
            "address": "10.10.200.14",
            "port": 16101,
            "community": "public",
            "oids": [
                "1.3.6.1.2.1.1.3.0",
                "1.3.6.1.4.1.63245.1.1.0",
                "1.3.6.1.4.1.63245.1.2.0",
                "1.3.6.1.4.1.63245.1.3.0",
                "1.3.6.1.4.1.63245.1.4.0"
            ]
        },
        "resources": [
            "sprout"
        ],
        "labs": [
            "embedded"
        ]
    }
]
```

(host-defaults)=

## Repo-level host defaults

Most labs share a common set of connection conventions — a non-standard SSH
port, a longer connect timeout, an alternate `nc` binary, etc.  Restating
those values on every host entry is repetitive and error-prone.  Move the
shared values into `[host_defaults]` in `.otto/settings.toml` and let the
per-host `*_options` tables override only the values that genuinely differ.

```toml
# .otto/settings.toml

[host_defaults.ssh_options]
port = 2222
connect_timeout = 5.0
keepalive_interval = 30

[host_defaults.telnet_options]
cols = 200
echo_negotiation_timeout = 1.0

[host_defaults.nc_options]
exec_name = "ncat"
port_strategy = "proc"
```

Valid sub-table names are exactly the per-host option keys:
`ssh_options`, `telnet_options`, `sftp_options`, `scp_options`,
`ftp_options`, `nc_options`.  Unknown keys raise at startup so typos
fail loudly instead of silently no-opping.

**Precedence (lowest to highest):**

1. The hardcoded dataclass defaults in `otto.host.options`.
2. `[host_defaults]` from each repo, applied in `OTTO_SUT_DIRS` order.
   When the same field appears in two repos, the later repo wins.
3. The host's own `*_options` table in `hosts.json`.

Merging happens **per key** at every layer.  Setting only `port` on a host
still inherits `connect_timeout` from the repo default; setting only
`connect_timeout` in the repo default still inherits `port` from the
dataclass default.

The merge is performed at host construction time, so the resulting host
carries the fully-resolved `*_options` instances — nothing has to be
re-resolved at use time.

## Merging labs

Pass multiple lab names to combine them:

```bash
otto --lab lab_a,lab_b test TestDevice
```

Hosts from all labs are merged into a single lab.  If two labs define the
same host ID, the later lab's definition wins.

## Exploring labs

```bash
otto --lab my_lab --list-labs      # list all available lab names
otto --lab my_lab --list-hosts     # list host IDs in the loaded lab
otto --lab my_lab --show-lab       # full lab details (use -v for expanded output)
```
