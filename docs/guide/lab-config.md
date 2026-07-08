# Lab Configuration

A *lab* is a set of hosts (and the routes between them) described in one or
more `lab.json` files.  Each file lists every host and declared link in a
given location; each host entry declares which lab names it belongs to, so a
single file can serve multiple labs and a host can belong to more than one lab
at once.  This page is the full per-host and per-link schema reference.  For
repo-level settings (paths, libs, init modules) see {doc}`repo-setup`.

(lab-files)=

## Lab files

Each directory listed under the `labs` key in `.otto/settings.toml` may
contain a `lab.json` file.  The file is a JSON **object** with two array
sections, `hosts` and `links`:

```json
{
    "hosts": [
        {
            "ip": "10.10.200.11",
            "element": "carrot",
            "board": "seed",
            "creds": [{ "login": "vagrant", "password": "vagrant" }],
            "labs": ["veggies"]
        },
        {
            "ip": "192.0.2.1",
            "element": "sprout",
            "board": "seed",
            "os_type": "zephyr",
            "labs": ["embedded"]
        }
    ],
    "links": []
}
```

`hosts` schema is unchanged from before the `lab.json` cutover.  Each entry
carries a `labs` field listing the lab names it belongs to.  Pass `--lab
veggies` (or set `OTTO_LAB=veggies`) and otto loads every host whose `labs`
field includes `"veggies"`.  `links` is covered in {ref}`lab-links` below;
when a file declares none, `"links": []` (or omitting the key) is fine.

The host **id** used by `get_host()`, `--list-hosts`, and the rest of the CLI
is `slug(element)`, plus `element_id` when set, plus (only when a `board` is
set) `_` + `slug(board)` and then `slot` when set — so `slot` never appears
in the id without a `board`.  See {ref}`host-identity` below for the exact
rules, a worked example, and how the display name and CLI handles are derived
alongside it.

## Per-host fields

### Required

| Field | Type | Description |
|-------|------|-------------|
| `ip` | string | IP address or DNS name otto will connect to. |
| `element` | string | Network-element name.  Slugged, then combined with `element_id`/`board`/`slot`, to form the host id — see {ref}`host-identity` below. |
| `creds` | array of objects | Ordered list of `{"login": ..., "password": ...}` entries (the first is the default login unless `user` pins another one).  At least one entry required for Unix hosts; optional for embedded hosts (RTOS telnet shells typically have no login step).  An entry may also carry `proxy`/`via`/`params` to describe a login-proxy hop. |
| `labs` | array of strings | Lab names this host belongs to.  Without this the host is invisible to `--lab`. |

### Common optional

| Field | Type | Description |
|-------|------|-------------|
| `board` | string | Board type, included in the host id when set. |
| `element_id` | integer | Disambiguates multiple instances of the same NE; appended to the host id. |
| `user` | string | Pin a specific user from `creds`.  Defaults to the first entry. |
| `term` | string | Terminal protocol lab pin — must be in the host's `valid_terms` menu.  Product `[host_preferences]` and CLI `--term` can override; see the precedence chain below. |
| `transfer` | string | File-transfer protocol lab pin — must be in the host's `valid_transfers` menu.  Product `[host_preferences]` and CLI `--transfer` can override; see the precedence chain below. |
| `valid_terms` | array of strings | Ordered list of term backends that may be selected for this host (gates `--term` and `[host_preferences]`).  Defaults to `["ssh", "telnet"]` for Unix hosts and `["telnet"]` for embedded hosts.  Custom backends registered via `register_term_backend` also appear. |
| `valid_transfers` | array of strings | Ordered list of transfer backends that may be selected for this host (gates `--transfer` and `[host_preferences]`).  Defaults to `["scp", "sftp", "ftp", "nc"]` for Unix hosts and `["console"]` for embedded hosts.  Custom backends registered via `register_transfer_backend` also appear. |
| `slot` | integer | Physical slot number of the board to which this host belongs.  Appended to the host id, but only when `board` is also set — see {ref}`host-identity` below. |
| `hop` | string | Host id of an intermediate SSH jump host.  Otto opens an SSH tunnel through it and routes all subsequent connections automatically.  Hops can chain. |
| `resources` | array of strings | Free-form resource tags used by the reservation backend. |
| `is_virtual` | boolean | `true` when the host is a VM or emulator. |
| `log` | boolean | Whether to log output to stdout and log files (default `true`). |
| `log_stdout` | boolean | Whether to log output to stdout (default `true`).  Setting `log` to `false` overrides this. |
| `docker_capable` | boolean | `true` when this host can run Docker containers (Unix hosts only). |

(host-identity)=

### Host identity & naming

There is no separate `id` field.  `element` is both the human-readable name
*and* the id source: it is **slugged** — lower-cased, with every run of
characters outside `[a-z0-9]` (spaces, punctuation, `_`) collapsed to a
single `-`, leading/trailing `-` stripped — to form the canonical id.  The id
is then `slug(element)`, plus `element_id` when set, plus (only when a `board`
is set) `_` + `slug(board)` and then `slot` when set: `element_id` can follow
the element with no board, but `slot` never appears without a board.
`"Lab X Server"` slugs to `lab-x-server`.  **Renaming `element` changes the
host's id** — and, transitively, the id of any declared {ref}`link
<lab-links>` whose `endpoints[].host` names it.

When the same `element` string appears more than once in a lab, disambiguate
with distinct `element` strings, an `element_id`, or `board`/`slot` — any of
these changes the resulting id.  Two hosts that still resolve to the same id
fail the lab load with a clear error instead of one silently overwriting the
other.

The **display name** (`host.name`) is a separate, human-friendly label: the
original-case `element`, a small logical number, `board`, and `slot`,
space-joined (parts omitted when absent).  The logical number is only added
when the `element` string repeats in the lab — it counts instances in
ascending `element_id` order, starting at `1` — so a unique `element` gets no
number.  Setting an explicit `name` on the host entry overrides this
generated label entirely.

On the CLI, wherever a *host* is named — the `otto host <id>` positional,
`--hop`, and docker's `--on` — you can type either the canonical id or the
shorter positional handle `<element-slug><logical number>` (e.g. `dut1` for
the first `dut`); tab completion offers both forms.

```json
{
  "hosts": [
    { "ip": "10.0.0.2", "element": "Lab X Server" },
    { "ip": "10.0.0.3", "element": "dut", "element_id": 47 },
    { "ip": "10.0.0.4", "element": "dut", "element_id": 103 }
  ]
}
```

| `element` / `element_id` | Id | Display name | CLI handle(s) |
|---|---|---|---|
| `"Lab X Server"` | `lab-x-server` | `Lab X Server` | `lab-x-server` |
| `"dut"` / `47` | `dut47` | `dut 1` | `dut47`, `dut1` |
| `"dut"` / `103` | `dut103` | `dut 2` | `dut103`, `dut2` |

`Lab X Server` is the only host with that `element`, so it has no logical
number and no positional handle — just its id.  The two `dut` hosts share an
`element`, so each is numbered by ascending `element_id` (`47` before `103`)
in both its display name and its positional handle.

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

### Network interfaces

The optional `interfaces` field maps a network-device name to that device's
address, so links (below) and later impairment/capture tooling can address a
specific device directly instead of just the host's management `ip`.

| Field | Type | Description |
|-------|------|-------------|
| `interfaces` | object | Map of netdev name (e.g. `"eth0"`, `"eth1"`) to an interface definition. |
| `interfaces.<name>` | object or string | `{"ip": "10.0.0.5"}`, or the bare string `"10.0.0.5"` as shorthand for the same object. |

```json
{
    "interfaces": {
        "eth0": "10.0.0.5",
        "eth1": { "ip": "10.0.1.5" }
    }
}
```

A host with no `interfaces` (or exactly one entry) needs no `interface` on a
declared-link endpoint — otto assumes it. A host with more than one entry
requires each declared-link endpoint on it to name which one; see
{ref}`lab-links` below.

### Power control

The optional `power_control` block configures a pluggable power controller for
the host.  The built-in `"command"` controller runs configured shell commands on
a *controller* host in the lab:

| Field | Type | Description |
|-------|------|-------------|
| `power_control` | object or string | Power controller spec.  A string selects a registered controller by type name; an object takes the fields below.  Omit to leave the host without power control. |
| `power_control.type` | string | Controller type name (e.g. `"command"`).  Selects the registered `PowerController` implementation. |
| `power_control.controller` | string | Host id of the lab host that runs the on/off/status commands.  `null` or absent runs commands on the local otto machine. |
| `power_control.on_cmd` | string | Shell command to power the host on.  Templated with `{name}`, `{ip}`, `{id}`. |
| `power_control.off_cmd` | string | Shell command to power the host off.  Same template variables. |
| `power_control.status_cmd` | string | Shell command to query power state (optional). |
| `power_control.status_on` | string | Substring of `status_cmd` output that means *on* (default `""`). |

```json
{
    "power_control": {
        "type": "command",
        "controller": "hypervisor1",
        "on_cmd": "virsh start {name}",
        "off_cmd": "virsh destroy {name}",
        "status_cmd": "virsh domstate {name}",
        "status_on": "running"
    }
}
```

See {doc}`host/capabilities` for the Power Control section, runtime API
(`host.power()`, `host.reboot(hard=True)`), and how to register a custom
controller (`register_power_controller`).

### SNMP monitoring

The optional `snmp` block configures SNMP polling for a host's metrics.  See
the SNMP section of {doc}`monitor`.

| Field | Type | Description |
|-------|------|-------------|
| `snmp.address` | string | SNMP agent IP address. |
| `snmp.port` | integer | SNMP UDP port. |
| `snmp.community` | string | SNMP community string. |
| `snmp.oids` | array of strings | OIDs to poll — raw dotted OIDs, named bundles (below), or a mix of both. |

Each entry in `oids` is either a raw dotted OID or one of otto's built-in
**named bundles**, which expand to a group of related OIDs and register
their descriptors as a side effect.  Bundles and raw OIDs mix freely in the
same list:

- `otto-core` — the five core scalars (uptime, overall CPU, heap used, heap
  free, thread count).
- `otto-net:N` — network OIDs for interfaces `0..N-1`.  `otto-net` alone
  (no `:N`) means `otto-net:1`, i.e. just interface `0`.
- `otto-fs:N` — filesystem OIDs for filesystems `0..N-1`, same `:N` default.

```json
{
    "snmp": {
        "address": "10.10.200.14",
        "port": 16101,
        "oids": [
            "otto-core",
            "otto-net:2",
            "otto-fs:1",
            "1.3.6.1.4.1.99999.1.5.0"
        ]
    }
}
```

`N` must be a positive integer.  An unknown bundle name fails fast at
monitor startup rather than silently polling nothing.  See
[Per-interface and per-filesystem OIDs](monitor.md#per-interface-and-per-filesystem-oids)
in {doc}`monitor` for what each expanded OID charts.

### Per-protocol option tables

Each of the following keys accepts an object that overrides individual
protocol fields.  They merge per-key with hardcoded dataclass defaults;
product `[host_preferences]` values are then applied on top (product wins
over the host's own values).  See {doc}`host/configuration` for the full
connection-options reference.

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
{
    "hosts": [
        {
            "ip": "10.10.200.11",
            "element": "carrot",
            "os_type": "unix",
            "board": "seed",
            "term": "ssh",
            "transfer": "scp",
            "is_virtual": true,
            "creds": [
                {"login": "vagrant", "password": "vagrant"},
                {"login": "test", "password": "Password1"}
            ],
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
    ],
    "links": []
}
```

(lab-links)=

## Links

A `links` entry in `lab.json` declares a data-plane route between two hosts —
distinct from the `hop` field's SSH/telnet *management* path. It is resolved
into a runtime `Link` object (`otto.link`) at lab-load time, the foundation
for tooling that will tunnel and impair such routes:

```json
{
    "name": "data-plane-a",
    "endpoints": [
        { "host": "carrot_seed", "interface": "eth1" },
        { "host": "tomato_seed", "interface": "eth1" }
    ],
    "protocol": "udp"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `endpoints` | array of exactly 2 objects | The two ends of the route, each `{"host": <id>, "interface": <netdev name>}`. |
| `endpoints[].host` | string | Host id (see {ref}`lab-files` above) — must resolve to a host loaded from *some* lab file. |
| `endpoints[].interface` | string | A key in that host's `interfaces` map (above). **Required only when the host defines more than one interface** — with one interface (or none) otto assumes it and its IP. Omitting it on a host with more than one interface is a load-time validation error ("ambiguous interface — specify one of {…}"), since otto can't disambiguate. |
| `protocol` | string | Optional; defaults to `"tcp"`. Informational for a declared link (documents what the route carries — e.g. `"udp"`, `"rtp"`); becomes functional for a *dynamic* link created by `otto link add` (drives socat UDP-vs-TCP). |
| `name` | string | Optional friendly handle; the link's id is otherwise derived from its endpoints. |
| `impair` / `management` | string | Optional, reserved for later `otto link` sub-projects (link impairment and management-host source attribution); accepted today but not yet acted on. |

**Lab membership is derived, not authored** — a link carries no `labs` field
of its own. It belongs to the union of both endpoints' `labs`: loading lab
`veggies` surfaces every link with at least one endpoint in `veggies`, even
one whose *other* endpoint lives in a different lab (that far endpoint
renders as a stub/dangling node). A link can legitimately span two labs.

(host-preferences)=

## Product host preferences

Most products share a common set of connection conventions — a non-standard
SSH port, a longer connect timeout, an alternate `nc` binary, preferred
terminal or transfer backend.  Restating those values on every host entry is
repetitive and error-prone.  Move the shared values into
`[host_preferences]` in `.otto/settings.toml`.

> **Migration note:** `[host_defaults]` was removed; its option tables move
> under `[host_preferences."<selector>".<opt>]`.

The `[host_preferences]` block is a map whose keys are **Python regexes**
matched (`re.fullmatch`) against each host's **id** (e.g. `carrot_seed`,
`router_seed_2`).  Under each selector, two kinds of values are allowed:

- **Selection lists** (`term`, `transfer`) — an ordered list of preferred
  backends.  Otto picks the first entry that is in the host's lab-defined
  `valid_terms` / `valid_transfers` menu; out-of-menu entries are skipped.
- **Option tables** (`ssh_options`, `telnet_options`, `sftp_options`,
  `scp_options`, `ftp_options`, `nc_options`) — per-key value overrides.

```toml
# .otto/settings.toml

# Selector = Python regex matched against host id (".*" = all hosts).
# Selections (term/transfer) are ordered preferences gated by each host's
# lab menu; option tables are per-key values that win over lab.json.
[host_preferences.".*"]
term = ["telnet"]
transfer = ["nc"]
ssh_options = { connect_timeout = 5.0, keepalive_interval = 30 }
telnet_options = { cols = 200, echo_negotiation_timeout = 1.0 }
nc_options = { exec_name = "ncat", port_strategy = "proc" }

# Narrower selectors overlay specific host groups.
[host_preferences."router.*"]
telnet_options = { port = 9023 }
```

Valid option-table names are: `ssh_options`, `telnet_options`,
`sftp_options`, `scp_options`, `ftp_options`, `nc_options`.  Unknown keys
raise at startup so typos fail loudly instead of silently no-opping.

**Precedence (lowest to highest):**

1. The hardcoded dataclass defaults in `otto.host.options`.
2. The host's own `*_options` table and `term`/`transfer` pin in
   `lab.json` (the `valid_*` menu hard-gates selections).
3. `[host_preferences]` from each repo — product values **win over**
   `lab.json`.  Repos are applied in `OTTO_SUT_DIRS` order (later repo
   wins); within a repo, selectors are applied in definition order (later
   selector wins on the same key).
4. CLI `--term` / `--transfer` — final word, applied at invocation time.

Merging happens **per key** at every option-table layer.  Setting only
`port` on a host in `lab.json` still inherits `connect_timeout` from the
product preference, and so on down to the dataclass default.

The merge is performed at host construction time, so the resulting host
carries the fully-resolved `*_options` instances — nothing has to be
re-resolved at use time.

For the full `*_options` field reference and per-field semantics, see
{doc}`host/configuration`.

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
