# Lab configuration

A *lab* is a named set of hosts (and the routes between them), described in
one or more `lab.json` files.  A lab is **declared**: it exists only once
some source lists it in the top-level `labs` table, which is also where what
belongs to the lab as a whole — its reservable `resources`, its `metadata` —
is written.

Hosts are not listed loose.  Each one belongs to an **element**: one piece of
lab equipment (a box, a chassis, a VM).  The element is the smallest unit
that joins a lab — every host of an element is in every lab the element is
in — and it joins by **pattern**, so one element can serve a whole family of
labs named by a scheme.  A host entry is one addressable portion of its
element: a board, a console, a management interface.

This page is the full field reference for all four levels: the `labs` table,
the element entry, the host entry, and the declared link.  For repo-level
settings (paths, libs, init modules) see {doc}`settings`; for how a repo
declares the sources it reads lab data from, see {doc}`host-sources`.

(lab-files)=

## Lab files

Each directory a json `[[lab.sources]]` entry lists in `paths` (in
`.otto/settings.toml`) may contain a `lab.json` file — and a `paths` entry
may name a `.json` file directly, or a glob matching several ([Splitting a
lab across files](#splitting-a-lab-across-files) below).  The file is a JSON
**object** with three sections:

```json
{
    "$schema": "../.otto/schemas/lab.schema.json",
    "labs": {
        "unix": {
            "resources": ["unix-bed"],
            "metadata": {"description": "unix regression bed"}
        },
        "embedded": {"resources": ["embedded-bed"]}
    },
    "elements": [
        {
            "name": "test1",
            "labs": ["unix"],
            "metadata": {"rack": "B4"},
            "hosts": [
                {
                    "ip": "10.10.200.11",
                    "os_type": "unix",
                    "creds": [{"login": "vagrant", "password": "vagrant"}]
                }
            ]
        },
        {
            "name": "zephyr37_fat",
            "labs": ["embedded"],
            "hosts": [
                {"ip": "192.0.2.1", "os_type": "zephyr", "os_version": "3.7"}
            ]
        }
    ],
    "links": []
}
```

`labs` is the table of declared labs, keyed by name ([The labs
table](#the-labs-table)).  `elements` is the array of element entries, each
grouping the host entries that address it ([Elements](#elements)).  `links`
declares data-plane routes and is covered in {ref}`lab-links` below.  All
three are optional: omitting a section is the same as writing it empty.

A top-level `$schema` is comment space, and so is any key beginning with `_`
at **every** level: the document, the `labs` table, a `labs` entry, an
element, a host entry, a link.  Otto strips them before validation, so a file
can carry a note inline anywhere and still be wired to the generated schema
in an editor.  Every other unknown key fails the load naming itself, and a
top-level `hosts` key is the pre-v2 shape: it fails with a migration message
pointing at [Migrating from the hosts
array](#migrating-from-the-hosts-array).

Pass `--lab unix` (or set `OTTO_LAB=unix`) and otto loads every host of every
element whose `labs` patterns match `unix`.  The lab has to be declared
first — elements alone never conjure one.

The host **id** used by `get_host()`, `--list-hosts`, and the rest of the CLI
is `slug(<element name>)`, plus the element's `id` when set, plus (only when
a `board` is set) `_` + `slug(board)` and then `slot` when set — so `slot`
never appears in the id without a `board`.  See {ref}`host-identity` below
for the exact rules, a worked example, and how the display name and CLI
handles are derived alongside it.

### Splitting a lab across files

Hundreds of elements do not belong in one file.  Every file a source names is
a complete lab document that may carry **any subset** of the three sections,
and one source composes all of its files by **union**: the `labs` tables
merge, the `elements` arrays concatenate, and so do the `links`.  An element
in one file joins a lab declared in another, and a file holding nothing but a
`labs` table is a fine home for a whole site's declarations.

A `paths` entry names those files.  Each entry is a **directory**
(contributing its `lab.json`), a path ending in **`.json`** (read as the lab
file itself), or a **glob** — an entry containing `*`, `?` or `[`.  A glob is
expanded relative to its non-glob prefix and contributes the `.json` files it
matches, in sorted order; one that matches nothing contributes nothing,
exactly like an absent `lab.json`.

```toml
# .otto/settings.toml

[[lab.sources]]
backend = "json"
paths = ["lab_data/labs.json", "lab_data/elements/*.json"]
```

A layout that scales: one file holding every declaration, and one file per
site (or per element) beside it.

```text
lab_data/
├── labs.json          # the labs table: every lab, its resources, its metadata
└── elements/
    ├── rack-b4.json   # elements only
    └── bench1.json    # elements only
```

Within **one** source a duplicate is a typo, never an override: the same lab
declared by two of a source's files, or the same element `(name, id)` carried
by two of them, fails the load naming both files.  Overriding is the opt-in
of a second `[[lab.sources]]` entry — see {doc}`host-sources`.

## The labs table

`labs` is keyed by lab name; each value declares what belongs to that lab as
a whole.

| Field | Type | Description |
|-------|------|-------------|
| `resources` | array of strings | Reservation identifiers this lab holds, matched byte-for-byte by the reservation backend.  Defaults to empty — a lab that reserves nothing is a perfectly good declaration.  See {doc}`../cli/reservation/index`. |
| `metadata` | object | Opaque lab-level user data; otto never reads it.  Surfaces as `lab.metadata["<lab name>"]`, and on every host of the lab as `host.lab_info.metadata`. |

**A lab exists if and only if some source declares it here.**  `otto
--list-labs` reads the declared names, and loading a name nothing declares
fails naming every configured source — even when elements' patterns would
have matched it.  The converse is a mistake too: a declared lab that no
element anywhere matches fails the load rather than yielding an empty lab.
What *is* fine is a `labs` entry in a file that holds none of that lab's
elements — another file, or another source, supplies them.

Declaration is what makes pattern membership sound.  Because an element joins
labs by regex ([Elements](#elements) below), the set of lab names is no
longer derivable from the elements — a pattern like `"unix.*"` names nothing
in particular — so the `labs` table is the enumerable record of what exists.

Resources are **declared**, never derived from hosts: the lab is the
reservable unit, and a host carries no resources of its own.  One consequence
is worth stating out loud — two labs that share elements contend with each
other only if their declarations share a resource identifier.  `otto init`
warns about any pair that shares an element and reserves nothing in common,
naming the labs, the shared elements, and the remedy.  (A lab that declares
no resources reserves nothing at all, so it is never half of such a pair.)

A **sub-lab** is just another declared lab whose name follows a scheme —
`unix.rack-b4` alongside `unix` — with its own `resources` and `metadata`.
Elements opt into it through their membership patterns: `"unix(\\..*)?"`
joins `unix` and every dotted sub-lab of it.  Reserving a portion of a lab
means declaring that portion as a lab; elements stay the smallest divisible
unit, and there is deliberately no host-level lab addressing.

## Elements

An `elements` entry is one piece of equipment and the host entries that
address it.  It carries identity, lab membership, and metadata — and nothing
operational.  There is no element-to-host inheritance: credentials, hops, and
option tables stay on the host entries.

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Element name — the human-readable name of the equipment, and the source of every child host's id (`slug(name)`; see {ref}`host-identity`).  Must slug to a non-empty token.  Required. |
| `id` | integer | Disambiguates repeats of the same `name`; must be `>= 0`.  Appended to each child host's id.  Omit when the name alone is unique. |
| `labs` | array of strings | Membership patterns, `re.fullmatch`-ed against a lab name (below).  Required and non-empty: an element that joins nothing is a mistake, and "every lab" is spelled `[".*"]`. |
| `metadata` | object | Opaque element-level user data; otto never reads it.  Surfaces as `host.element_metadata` on every host of the element — a copy per host, so two hosts of one element never share a mutable dict. |
| `hosts` | array of objects | The element's host entries — the [per-host fields](#per-host-fields) below.  Required and non-empty. |

**Membership is by pattern.**  Each `labs` entry is a Python regular
expression matched with `re.fullmatch` against a lab name — the same rule
`[project] lab_patterns` uses ({ref}`project-scope` below), so there is one
matching rule to learn:

- A bare name is a pattern that matches itself: `"unix"` joins `unix`, and
  does *not* join `unix2`.
- `".*"` joins every declared lab.
- `"unix(\\..*)?"` joins `unix` and every dotted sub-lab of it.
- `.` is a metacharacter.  A lab actually named `lab.1` is matched by
  `"lab\\.1"`; the unescaped `"lab.1"` matches `lab-1` as well.

An invalid regex fails at parse, naming the element and the pattern.  A
pattern that fullmatches no declared lab anywhere is *dead*: `otto init`
reports it as a warning rather than a load error, because a shared file may
legitimately serve projects that declare different labs.

**The element is the smallest unit assignable to a lab.**  A chassis whose
boards must belong to *different* labs is modelled as separate elements with
distinct names.  `os_type` stays per host either way, so a Zephyr board and
its Unix management host can share one element when they share membership.

**The element is also the unit of multi-source override.**  When two
`[[lab.sources]]` entries carry the same element `(name, id)`, the later
source's element replaces the earlier one **wholesale** — hosts, metadata,
and the membership the later element states — with a warning naming both
sources.  Overriding one board of a four-board chassis means restating the
whole element entry; in exchange, a hybrid element (this source's hosts with
that source's metadata) cannot exist.  `labs` entries replace the same way,
resources and metadata together.  See {doc}`host-sources`.

Replacement happens per lab load, so it covers exactly the labs *both*
elements match.  An override that **drops** a membership pattern therefore
does not take the element out of a lab the earlier source's element still
matches — that lab keeps loading the earlier element; to remove an element
from a lab, change it at the source that declares it.

## Per-host fields

A host entry is one addressable endpoint of its element.  `element`,
`element_id`, `labs`, and `resources` are **not** host fields: the first two
are the element's `name` and `id`, `labs` is the element's membership, and
`resources` belongs to the [labs table](#the-labs-table).  A host entry
carrying any of them fails the load naming the key.

### Required

| Field | Type | Description |
|-------|------|-------------|
| `ip` | string | IP address or DNS name otto will connect to. |
| `creds` | array of objects | Ordered list of `{"login": ..., "password": ...}` entries (the first is the default login unless `user` pins another one).  At least one entry required for Unix hosts; optional for embedded hosts (RTOS telnet shells typically have no login step).  An entry may also carry `proxy`/`via`/`params` to describe a login-proxy hop. |

### Common optional

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Display-name override.  Otto derives a human-friendly label from the element name, its logical number, `board`, and `slot`; setting `name` replaces that label entirely.  It does **not** change the host id. |
| `metadata` | object | Opaque user data — the sanctioned home for custom fields, so `extra="forbid"` never has to give way.  Otto never reads it.  Surfaces as `host.metadata`; the element's as `host.element_metadata`; the lab's as `host.lab_info`. |
| `board` | string | Board type, included in the host id when set. |
| `user` | string | Pin a specific user from `creds`.  Defaults to the first entry. |
| `term` | string | Terminal protocol lab pin — must be in the host's `valid_terms` menu.  Product `[host_preferences]` and CLI `--term` can override; see the precedence chain below. |
| `transfer` | string | File-transfer protocol lab pin — must be in the host's `valid_transfers` menu.  Product `[host_preferences]` and CLI `--transfer` can override; see the precedence chain below. |
| `impairer` | string | Link-impairment backend lab pin — must be in the host's `valid_impairers` menu (Unix hosts only).  Product `[host_preferences]` can override.  See {doc}`../cli/link/index`. |
| `valid_terms` | array of strings | Ordered list of term backends that may be selected for this host (gates `--term` and `[host_preferences]`).  Defaults to `["ssh", "telnet"]` for Unix hosts and `["telnet"]` for embedded hosts.  Custom backends registered via `register_term_backend` also appear. |
| `valid_transfers` | array of strings | Ordered list of transfer backends that may be selected for this host (gates `--transfer` and `[host_preferences]`).  Defaults to `["scp", "sftp", "ftp", "nc"]` for Unix hosts and `["console"]` for embedded hosts.  Custom backends registered via `register_transfer_backend` also appear. |
| `valid_impairers` | array of strings | Ordered list of impairer backends that may be selected for this host (gates `impairer` and `[host_preferences]`).  Unix hosts only; defaults to `["netem"]`.  Custom impairers registered via `register_impairer` are valid entries too. |
| `slot` | integer | Physical slot number of the board to which this host belongs.  Appended to the host id, but only when `board` is also set — see {ref}`host-identity` below. |
| `hop` | string | Host id of an intermediate SSH jump host.  Otto opens an SSH tunnel through it and routes all subsequent connections automatically.  Hops can chain. |
| `default_dest_dir` | string | Directory an empty or relative `put`/`get` destination resolves against.  Defaults to empty — SCP/SFTP then land in the login user's home directory, and an embedded host with a mounted filesystem falls back to its mount point. |
| `max_filename_len` | integer | Longest basename the host's filesystem accepts.  Defaults to `255` (the Linux `NAME_MAX`, and the typical LittleFS ceiling); lower it where the firmware enforces a tighter limit — e.g. `32` on a Zephyr build with a short-name FAT. |
| `debug_log_globs` | array of strings | Remote log paths `get_debug_logs` fetches off this host.  A pattern (`*`, `?`, `[`) is expanded on the device itself, so embedded hosts — which have no shell to expand with — declare concrete paths.  See {doc}`../cli/host/capabilities/index`. |
| `is_virtual` | boolean | `true` when the host is a VM or emulator. |
| `log` | string | Standing log disposition for this host's command I/O, named by its `LogMode`: `"normal"` (the default) logs everywhere, `"quiet"` keeps it in `verbose.log` but off the console, `"never"` redacts it from every sink.  Composed with each command's own mode, the more restrictive winning.  Booleans are no longer accepted — write the mode name. |
| `log_stdout` | boolean | Whether this host's output is echoed to stdout (default `true`).  Currently unused by otto — `log` alone governs the console: `"quiet"` keeps output off it, `"never"` redacts it everywhere. |
| `docker_capable` | boolean | `true` when this host can run Docker containers (Unix hosts only). |
| `has_bash` | boolean | `true` when the host has a working `bash` to `exec -a`-tag processes through. Gates which hosts can host or be scanned for `otto tunnel` tunnels — see {doc}`../cli/tunnel/index`. Defaults to `true` for Unix hosts (including `local` and Docker containers), `false` for embedded hosts. |
| `shell_history` | boolean | Whether otto's own commands are recorded in this host's shell history (Unix hosts only). Defaults to `false` — otto neutralizes `HISTFILE` on each shell it opens so automation traffic doesn't bury a human's history. Set `true` where otto's commands should stay visible in the history file. See {ref}`per-host-shell-history`. |

(host-identity)=

### Host identity & naming

There is no `id` field on a host entry.  The **element's `name`** is both the
human-readable name *and* the id source: it is **slugged** — lower-cased,
with every run of characters outside `[a-z0-9]` (spaces, punctuation, `_`)
collapsed to a single `-`, leading/trailing `-` stripped — to form the
canonical id.  The id is then `slug(name)`, plus the element's `id` when set,
plus (only when a `board` is set) `_` + `slug(board)` and then `slot` when
set: the element's `id` can follow the name with no board, but `slot` never
appears without a board.  `"Lab X Server"` slugs to `lab-x-server`.
**Renaming an element changes its hosts' ids** — and, transitively, the id of
any declared {ref}`link <lab-links>` whose `endpoints[].host` names one.

An `os_profile` can supply `board`, `slot`, or — for an element that declares
no `id` — `element_id` as a default, so a host record that names no `board`
may still get one, and a different id than the record alone suggests.  (An
element that *does* declare an `id` always wins — the host record beats the
profile defaults it is merged with.)  Values are also coerced before the id
is built, so a JSON
`3.0` becomes `3`.  When authoring a link endpoint, take the id from `otto
host <TAB>` (or `otto --show-lab`) rather than composing it by eye: an
endpoint naming an id no host answers to fails the lab load.

When the same element `name` appears on more than one element in a lab,
disambiguate with distinct names, an element `id`, or `board`/`slot` — any of
these changes the resulting id.  Two hosts that still resolve to the same id
fail the lab load with a clear error instead of one silently overwriting the
other.

The **display name** (`host.name`) is a separate, human-friendly label: the
original-case element `name`, a small logical number, `board`, and `slot`,
space-joined (parts omitted when absent).  The logical number is only added
when the element `name` repeats in the lab — it counts instances in ascending
element `id` order, starting at `1` — so a unique name gets no number.
Setting an explicit `name` on the *host entry* overrides this generated label
entirely.

On the CLI, wherever a *host* is named — the `otto host <id>` positional,
`--hop`, and docker's `--on` — you can type either the canonical id or the
shorter positional handle `<element-slug><logical number>` (e.g. `dut1` for
the first `dut`); tab completion offers both forms.

```json
{
  "elements": [
    { "name": "Lab X Server", "labs": ["unix"],
      "hosts": [{ "ip": "10.0.0.2", "creds": [{"login": "root", "password": "pw"}] }] },
    { "name": "dut", "id": 47, "labs": ["unix"],
      "hosts": [{ "ip": "10.0.0.3", "creds": [{"login": "root", "password": "pw"}] }] },
    { "name": "dut", "id": 103, "labs": ["unix"],
      "hosts": [{ "ip": "10.0.0.4", "creds": [{"login": "root", "password": "pw"}] }] }
  ]
}
```

| Element `name` / `id` | Id | Display name | CLI handle(s) |
|---|---|---|---|
| `"Lab X Server"` | `lab-x-server` | `Lab X Server` | `lab-x-server` |
| `"dut"` / `47` | `dut47` | `dut 1` | `dut47`, `dut1` |
| `"dut"` / `103` | `dut103` | `dut 2` | `dut103`, `dut2` |

`Lab X Server` is the only element with that name, so its host has no logical
number and no positional handle — just its id.  The two `dut` elements share
a name, so each is numbered by ascending element `id` (`47` before `103`) in
both its display name and its positional handle.

### Host type / OS

| Field | Type | Description |
|-------|------|-------------|
| `os_type` | string | Profile selector.  Defaults to `"unix"`.  Resolves to a registered host class and optional defaults bundle — see {doc}`os-profiles`. |
| `os_name` | string | Human-readable OS name (e.g. `"Linux"`, `"Zephyr"`). |
| `os_version` | string | OS or kernel version string (e.g. `"3.7"`, `"4.4"`). |
| `hw_version` | string | Free-form hardware version description (Unix hosts only).  Informational — otto never parses it. |
| `sw_version` | string | Free-form software version description (Unix hosts only).  Informational — otto never parses it. |

### Embedded-only fields

These fields apply only to hosts with an embedded base type (e.g.
`os_type: "zephyr"` or `os_type: "embedded"`).  See {doc}`../cli/host/embedded` for full
details.

| Field | Type | Description |
|-------|------|-------------|
| `command_frame` | string | Shell-framing dialect (e.g. `"zephyr"`, `"zephyr-serial"`). |
| `filesystem` | string | On-device filesystem variant (`"none"`, `"fat-ram"`, `"littlefs"`). |
| `loader` | string | Binary-load strategy for this target's runtime, by registry name (e.g. `"llext-hex"`).  Optional — omit it on a target that never loads binaries, and `host.load()`/`unload()` then fail loud.  Projects register their own via `register_binary_loader`. |

File transfer for embedded hosts uses `"console"` or `"tftp"` — see
{doc}`../cli/host/embedded`.

### Network interfaces

The optional `interfaces` field maps a network-device name to that device's
address, so links (below) and later impairment/capture tooling can address a
specific device directly instead of just the host's management `ip`.

| Field | Type | Description |
|-------|------|-------------|
| `interfaces` | object | Map of netdev name (e.g. `"eth0"`, `"eth1"`) to an interface definition. |
| `interfaces.<name>` | object or string | `{"ip": "10.0.0.5"}`, or the bare string `"10.0.0.5"` as shorthand for the same object. |
| `interfaces.<name>.subnet` | string | Optional network the interface belongs to, in CIDR form (`"192.168.1.0/24"` — the network address, not a host address). Declares the interface's L3 neighborhood for topology and reachability tooling; when set, the interface `ip` must fall inside it (validated at load). |

```json
{
    "interfaces": {
        "eth0": "10.0.0.5",
        "eth1": { "ip": "192.168.1.5", "subnet": "192.168.1.0/24" }
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

See {doc}`../cli/host/capabilities/index` for the Power Control section, runtime API
(`host.power()`, `host.reboot(hard=True)`), and how to register a custom
controller (`register_power_controller`).

### SNMP monitoring

The optional `snmp` block configures SNMP polling for a host's metrics — add
it to a host entry in `lab.json` and `otto monitor` starts collecting for that
host.  See {doc}`../cli/monitor/metrics` for what otto does with the readings.

The `address` and `port` are the endpoint reachable from the otto host — for
an embedded device behind a hop this is typically the local end of a UDP relay
on the hop host, not the device's own address.  `community` defaults to
`"public"`.  Presentation (label, chart group, unit) is supplied by the
descriptor registry, not by lab data.

| Field | Type | Description |
|-------|------|-------------|
| `snmp` | object | The SNMP polling block.  Omit it to leave the host unpolled. |
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
[Per-interface and per-filesystem OIDs](../cli/monitor/metrics.md#per-interface-and-per-filesystem-oids)
in {doc}`../cli/monitor/index` for what each expanded OID charts.

### Option tables

Each of the following keys accepts an object that overrides individual
fields.  They merge per-key with hardcoded dataclass defaults;
product `[host_preferences]` values are then applied on top (product wins
over the host's own values).  See {doc}`host-options` for the full
connection-options reference.

| Key | Covers |
|-----|----------|
| `ssh_options` | SSH (term and hop) |
| `telnet_options` | Telnet (term, and the embedded console) |
| `sftp_options` | SFTP transfer |
| `scp_options` | SCP transfer |
| `ftp_options` | FTP transfer |
| `nc_options` | Netcat transfer |
| `userland_options` | The device's own userland (see below) |

`userland_options` is the odd one out: it names no protocol.  It carries
declared answers about the *device* — which elevation mechanism it has,
which `timeout` calling convention its applet speaks, and so on — that otto
otherwise probes for once per host and caches.  Every key defaults to
"probe it", so the table is only worth writing to skip a probe or to correct
one.  Run `otto host <id> probe` and otto prints the answers it settled as a
`userland_options` object ready to paste in — see
{ref}`userland-capabilities`.

```json
"userland_options": { "elevation": "su", "timeout_style": "dash-t" }
```

### Coverage toolchain

The `toolchain` object points to the cross-toolchain binaries used by the
coverage pipeline.  See {doc}`../cli/cov/index`.

| Field | Type | Description |
|-------|------|-------------|
| `toolchain` | object | The cross-toolchain block.  Omit it to leave the host on system-installed `gcov`/`lcov`. |
| `toolchain.sysroot` | string | Path to the cross-toolchain sysroot. |
| `toolchain.gcov` | string | Path to `gcov` relative to `sysroot`, or an absolute path. |
| `toolchain.lcov` | string | Path to the `lcov` binary. |
| `toolchain.tools` | array of objects | Artifacts otto *installs onto* this host (`name`, `source`, `dest`, `user`, `mode`) — the inverse of the three fields above, which otto only reads from.  See {doc}`../cli/host/capabilities/index`. |

## Example

Two elements from otto's own test fixture — one Unix host, one Zephyr host —
with the `labs` entries that declare their labs (the fixture's own tables
list more resources than are shown here):

```json
{
    "labs": {
        "unix": {
            "resources": ["test1", "test2", "test3"],
            "metadata": {"description": "unix regression bed"}
        },
        "busybox": {"resources": ["test1", "bb1161"]},
        "embedded": {"resources": ["test4", "zephyr37_fat"]}
    },
    "elements": [
        {
            "name": "test1",
            "labs": ["unix", "busybox"],
            "metadata": {"role": "hub"},
            "hosts": [
                {
                    "ip": "10.10.200.11",
                    "os_type": "unix",
                    "valid_terms": ["ssh", "telnet"],
                    "valid_transfers": ["scp", "sftp", "ftp", "nc"],
                    "is_virtual": true,
                    "docker_capable": true,
                    "creds": [
                        {"login": "vagrant", "password": "vagrant"},
                        {"login": "test", "password": "Password1"}
                    ],
                    "interfaces": {
                        "eth2": {"ip": "192.168.1.11", "subnet": "192.168.1.0/24"},
                        "bbeth-1350": {"ip": "198.51.100.18", "subnet": "198.51.100.16/30"}
                    }
                }
            ]
        },
        {
            "name": "zephyr37_fat",
            "labs": ["embedded"],
            "hosts": [
                {
                    "ip": "192.0.2.1",
                    "os_type": "zephyr",
                    "os_version": "3.7",
                    "valid_transfers": ["console"],
                    "filesystem": "fat-ram",
                    "max_filename_len": 32,
                    "is_virtual": true,
                    "hop": "test4",
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
                    }
                }
            ]
        }
    ],
    "links": []
}
```

`test1` is in two labs because its *element* is: every host it holds joins
`unix` and `busybox` together.  Note that the reservation identifier `test1`
is declared on both labs, so reserving either contends with the other — the
overlap rule the [labs table](#the-labs-table) describes.  The Zephyr host's
`hop` names `test4`, another element of the same fixture: a management path
to reach the device, not a declared link.

(lab-links)=

## Links

A `links` entry in `lab.json` declares a data-plane route between two hosts —
distinct from the `hop` field's SSH/telnet *management* path. It is resolved
into a runtime `Link` object (`otto.link`) at lab-load time: the edge that
`otto tunnel add` (see {doc}`../cli/tunnel/index`) rides to actually stand up traffic, and
that `otto link impair` (see {doc}`../cli/link/index`) targets to impair it:

```json
{
    "name": "data-plane-a",
    "endpoints": [
        { "host": "test1", "interface": "eth1" },
        { "host": "test2", "interface": "eth1" }
    ],
    "protocol": "udp"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `endpoints` | array of exactly 2 objects | The two ends of the route, each `{"host": <id>, "interface": <netdev name>}`. |
| `endpoints[].host` | string | Host id (see {ref}`lab-files` above) — must resolve to a host loaded from *some* lab file. |
| `endpoints[].interface` | string | A key in that host's `interfaces` map (above). **Required only when the host defines more than one interface** — with one interface (or none) otto assumes it and its IP. Omitting it on a host with more than one interface is a load-time validation error ("ambiguous interface — specify one of {…}"), since otto can't disambiguate. |
| `protocol` | string | Optional; defaults to `"tcp"`. Informational for a declared link (documents what the route carries — e.g. `"udp"`, `"rtp"`); the analogous `otto tunnel add --protocol` flag is what actually drives socat UDP-vs-TCP when a tunnel is built over this route. |
| `name` | string | Optional friendly handle; the link's id is otherwise derived from its endpoints. |
| `impair` | string | Optional in-path middlebox **host id** — a bare string, validated as a known host reference at lab load. When set, `otto link impair` (see {doc}`../cli/link/index`) places both directions' netem on that middlebox's live-resolved facing interfaces instead of the link's own endpoints. `None` (the default) is endpoint-anchored impairment. |
| `management` | string | Optional; accepted but currently has no effect. |

**Lab membership is derived, not authored** — a link carries no membership of
its own. It belongs to the union of both endpoints' *elements'* labs: loading
lab `unix` surfaces every link with at least one endpoint in `unix`, even one
whose *other* endpoint lives in a different lab (that far endpoint renders as
a stub/dangling node). A link can legitimately span two labs.

## Migrating from the hosts array

Before v2, a `lab.json` carried a top-level `hosts` array, and each host entry
declared its own `element`, `element_id`, `labs`, and `resources`.  That shape
is gone: a top-level `hosts` key fails the load with a message naming the
file.  There is no migration tool — the transform is mechanical, and this is
it.

Before:

```json
{
    "hosts": [
        {
            "ip": "10.10.200.11",
            "element": "test1",
            "creds": [{"login": "vagrant", "password": "vagrant"}],
            "resources": ["test1"],
            "labs": ["unix"]
        },
        {
            "ip": "10.0.0.5",
            "element": "dut",
            "element_id": 3,
            "board": "cpu",
            "creds": [{"login": "admin", "password": "admin"}],
            "labs": ["embedded"]
        },
        {
            "ip": "10.0.0.6",
            "element": "dut",
            "element_id": 3,
            "board": "mgmt",
            "creds": [{"login": "root", "password": "root"}],
            "labs": ["embedded"]
        }
    ],
    "links": []
}
```

After:

```json
{
    "labs": {
        "unix": {"resources": ["test1"]},
        "embedded": {}
    },
    "elements": [
        {
            "name": "test1",
            "labs": ["unix"],
            "hosts": [
                {
                    "ip": "10.10.200.11",
                    "creds": [{"login": "vagrant", "password": "vagrant"}]
                }
            ]
        },
        {
            "name": "dut",
            "id": 3,
            "labs": ["embedded"],
            "hosts": [
                {
                    "ip": "10.0.0.5",
                    "board": "cpu",
                    "creds": [{"login": "admin", "password": "admin"}]
                },
                {
                    "ip": "10.0.0.6",
                    "board": "mgmt",
                    "creds": [{"login": "root", "password": "root"}]
                }
            ]
        }
    ],
    "links": []
}
```

Four steps, in order:

1. **Group the `hosts` array by `element` + `element_id`.**  Each group
   becomes one `elements` entry: `element` becomes the element's `name`,
   `element_id` becomes its `id`, and the group's host entries become its
   `hosts`.  Delete both keys from the host entries — they are errors there
   now.
2. **Move each group's `labs` up to the element.**  The hosts of one element
   must have carried the same `labs`; where they did not, they were never one
   element — split them into elements with distinct names.
3. **Union each lab's hosts' `resources` into that lab's `labs` entry.**
   Every lab name any element joins must appear in some source's `labs`
   table: a lab exists only once it is declared.  A lab that reserves nothing
   is written `{}`.
4. **Delete `resources` from the host entries.**  The lab is the reservable
   unit; hosts carry no resources.

Two things do *not* change.  Host ids compose exactly as before — `slug(name)`
plus the element `id` plus `board`/`slot` — so declared-link endpoints,
`[project] host_patterns`, and every scripted `get_host("dut3_cpu")` keep
working.  And every operational host field stays exactly where it was.

One host field does change alongside them: `log` no longer accepts `true` or
`false`.  Write the mode name — `"normal"`, `"quiet"`, or `"never"`.

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
matched (`re.fullmatch`) against each host's **id** (e.g. `test1`,
`router_seed2`).  Under each selector, two kinds of values are allowed:

- **Selection lists** (`term`, `transfer`, `impairer`) — an ordered list of
  preferred backends.  Otto picks the first entry that is in the host's
  lab-defined `valid_terms` / `valid_transfers` / `valid_impairers` menu;
  out-of-menu entries are skipped.
- **Option tables** (`ssh_options`, `telnet_options`, `sftp_options`,
  `scp_options`, `ftp_options`, `nc_options`, `userland_options`) — per-key
  value overrides.

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
`sftp_options`, `scp_options`, `ftp_options`, `nc_options`,
`userland_options`.  Unknown keys raise at startup so typos fail loudly
instead of silently no-opping.

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
{doc}`host-options`.

(project-scope)=

## Project scope: the labs and hosts a repo targets

A lab database can describe every host an organization owns; a given repo
usually cares about a fraction of them.  The `[project]` table in
`.otto/settings.toml` is where a repo declares its **fleet of interest** — the
labs it applies to, and the hosts it targets inside them.

```toml
# .otto/settings.toml

[project]
lab_patterns  = ["tech-.*", "bench1"]     # labs this project applies to
host_patterns = ["sensor-.*", "gw-\\d+"]  # hosts of interest within those labs
```

Both keys hold **Python regexes**, and both are matched with `re.fullmatch`
against a whole name — never `re.search`.  `bench` does not admit
`bench-overflow`; write `bench.*` (wrapping any alternation first, as
`(bench|floor).*`).  Entries within a key are OR-ed: a lab is applicable when
**any** `lab_patterns` entry matches it, and a host is in the universe when its
lab is applicable **and any** `host_patterns` entry matches its id.

`lab_patterns`
: The labs this project applies to.  There is **no default**, and leaving the
  key out of a `[project]` table you did write is not "every lab" — it compiles
  to no patterns, which matches nothing.  Every-lab is spelled `[".*"]`, out
  loud: match-all is a visible choice here, never a default that quietly widens
  a project's reach.

`host_patterns`
: The hosts of interest within those labs.  Defaults to `[".*"]` — every host
  of every applicable lab — so a repo that only needs the lab axis writes only
  `lab_patterns`.

```{warning}
Writing `[project]` **without** `lab_patterns` declares a repo that applies to
no lab at all.  That is not a quiet no-op: the repo is excluded, and if it is
the project driving the run, every project-layer verb aborts naming the loaded
labs and your patterns.  A `[project]` table with only `host_patterns` in it is
the shape that hits this.
```

An invalid regex fails at settings parse — the moment the repo is discovered,
not on the first fleet walk — naming the offending pattern and what `re` made
of it:

```text
[project] pattern 'bench(' is not a valid regular expression: missing ), unterminated subpattern at position 5
```

### Required once a repo registers providers

A repo that registers a product or dev-tool provider (see
{doc}`../cli/host/capabilities/index`) **must** declare `lab_patterns`.  The check runs
at bootstrap, right after init modules have been imported — the earliest moment
the registries can answer — and it aborts the whole run rather than being
downgraded to a warning:

```text
repo 'sensors' registers product/dev-tool providers but declares no
[project] lab_patterns in .otto/settings.toml. A providing repo must say
which labs it applies to. Add:

    [project]
    lab_patterns = [".*"]   # every lab — make the reach explicit
    #host_patterns = [".*"]

and narrow the patterns to the labs this project actually targets.
```

`lab_patterns = []` gets the same refusal: an empty list is not a narrower
declaration, it is the same "no lab" the missing key compiles to.  An empty
`host_patterns = []` is refused separately — it admits no host in any lab, so
every provider the repo registers would be dead code.

A repo that registers **no** providers needs no `[project]` table at all.

### What the declaration changes

- **Fleet walks are bounded by it.**  `ctx.all_hosts()`,
  `ctx.do_for_all_hosts(...)` and `ctx.run_on_all_hosts(...)` iterate the
  declared universe rather than the whole loaded lab.  See
  {doc}`../cli/run/defaults` for the walk semantics, the union across repos, and
  the whole-lab fallback for repos that declare nothing.
- **Providers are gated by it.**  A repo's product and dev-tool providers are
  not invoked at all on a host outside its universe, so nothing that repo owns
  can attach to a machine it never declared.
- **Explicit targeting is not bounded by it.**  `otto host <id> <verb>`,
  `ctx.get_host("id")` and the `otto host` id listing reach any host in the
  loaded lab.  Explicit targeting beats scoping: a repo naming a jump host it
  does not own must still be able to reach it.

### Merged labs and containers

A host is matched against the **component** lab it came from, never a
composite display name.  Under `otto --lab lab_a+lab_b` a repo declaring
`lab_patterns = ["lab_a"]` still applies — its hosts are stamped `lab_a` — and
it targets the `lab_a` hosts only.  A pattern written to match the merged name
(`lab_a\+lab_b`) matches nothing.

Docker containers created during a run inherit their parent host's lab, so a
container joins the same universes its parent is in.

## Docker-capable hosts

Mark hosts that can host containers.  `docker_capable` is a *host* field, so
it goes on the host entry inside its element:

```text
{
  "name": "test3",
  "labs": ["unix"],
  "hosts": [
    { "ip": "...", "creds": [...], "docker_capable": true }
  ]
}
```

See {doc}`../cli/docker/index` for the commands that read this.

## Declaring toolchain tools in lab data

Toolchain tools are host-wide artifacts (a cross-built `gdbserver`, a runtime
`.so`), so they *are* lab data — declared alongside the coverage toolchain in
the host's `lab.json` entry:

```json
"toolchain": {
    "sysroot": "/opt/arm-toolchain",
    "tools": [
        {
            "name": "gdb",
            "source": "build/arm-linux-gnueabihf-gdb",
            "dest": "/usr/local/bin",
            "user": "root",
            "mode": "755"
        }
    ]
}
```

`name` is a **rename target**, not a label: `put` lands every file under its
source basename, so a tool whose `name` differs is `mv`'d there before it is
chowned — which is how `arm-linux-gnueabihf-gdb` installs as plain `gdb`. Those
destinations are usually root-owned, hence the per-tool `user` and `mode`.

One toolchain serves every owner on a host, so placing and removing it is a
host-wide step: no repo's actions touch it (see
{doc}`../cli/run/defaults`), and the default `install_toolchain_tools` is the method
most likely to need project surgery — override it on the host class.
