# Repository Setup

Otto discovers your project through a `.otto/settings.toml` file at the
repository root.  This page explains every setting and what happens during
project initialization.

## The settings file

Create `.otto/settings.toml` in your repo root:

```toml
name = "my_project"
version = "1.0.0"

labs  = ["${sutDir}/../lab_data"]
libs  = ["${sutDir}/pylib"]
tests = ["${sutDir}/tests"]
init  = ["my_instructions", "my_shared_options"]

# Optional: connection defaults applied to every host this repo touches.
[host_defaults.ssh_options]
connect_timeout = 5.0
keepalive_interval = 30
```

### Variable expansion

`${sutDir}` is replaced with the absolute path to the repo root at load
time.  Use it to keep paths relative and portable.  Expansion runs
inside every settings table, including string values nested under
`[host_defaults]`.

### Field reference

name
: **Required.** Product or repository name.  Displayed in CLI panels and log
  output.

version
: **Required.** Semantic version string (e.g. `"1.0.0"`).

labs
: List of directory paths to search for lab JSON files.  When you pass
  `--lab my_lab`, otto looks in these directories for a file matching that
  name.  Defaults to `[]`.

libs
: List of Python package directories to add to `sys.path` at startup.
  This is where you put your instruction modules, shared options, and helper
  libraries.  Defaults to `[]`.

tests
: List of directories to scan for `test_*.py` files.  Each matching file
  is imported at startup, which triggers `@register_suite()` decorators and
  makes suites available as `otto test` subcommands.  Defaults to `[]`.

init
: List of Python module names (dot-separated) to import at startup.  Use
  this to register instructions (`@command()`) and shared option classes.
  These modules must be importable from one of the `libs` directories.
  Defaults to `[]`.

\[host_defaults\]
: Optional table of per-protocol option defaults applied to every host
  loaded from this repo's `labs`.  See
  {ref}`host-defaults` below for the full schema and precedence rules.

## What happens at startup

When you run any `otto` command, the following initialization sequence
occurs:

1. **Environment parsing** -- Otto reads `OTTO_SUT_DIRS` to find repo root
   directories.

2. **Repo discovery** -- For each path in `OTTO_SUT_DIRS`, otto creates a
   `Repo` object and reads its `.otto/settings.toml`.

3. **Apply settings** -- For each repo, otto:
   - Adds `libs` directories to `sys.path`
   - Imports modules listed in `init` (this registers instructions)
   - Auto-imports all `test_*.py` files from `tests` directories (this
     registers suites)

4. **Lab loading** -- Otto collects all `labs` search paths from every repo
   and loads the lab(s) specified by `--lab` or `OTTO_LAB`.  Multiple labs
   are merged, combining their hosts.

5. **Config module creation** -- The global `ConfigModule` is created with
   the loaded repos and lab, making hosts available to all commands.

## Multiple repos

Otto supports multiple repos simultaneously.  Set `OTTO_SUT_DIRS` to a
comma-separated list:

```bash
export OTTO_SUT_DIRS=/path/to/repo1,/path/to/repo2
```

Each repo has its own settings, libs, tests, and lab search paths.  They
are all merged at startup -- instructions and suites from every repo appear
in the CLI, and lab search paths from all repos are combined.

## Lab files

Each directory listed under `labs` may contain a single `hosts.json`
file holding every host known to that location.  Each entry carries a
`labs` field listing the lab names it belongs to, so a single
`hosts.json` can serve any number of labs and hosts can belong to more
than one lab at once.

The file is a JSON **array** of host objects:

```json
[
    {
        "ip": "10.10.200.11",
        "ne": "carrot",
        "board": "seed",
        "creds": { "vagrant": "vagrant" },
        "labs": ["veggies"]
    },
    {
        "ip": "10.10.200.12",
        "ne": "tomato",
        "board": "seed",
        "term": "telnet",
        "transfer": "nc",
        "creds": { "vagrant": "vagrant" },
        "labs": ["veggies"]
    },
    {
        "ip": "10.10.200.13",
        "ne": "pepper",
        "board": "seed",
        "hop": "carrot_seed",
        "creds": { "vagrant": "vagrant" },
        "labs": ["veggies"]
    }
]
```

Pass `--lab veggies` (or set `OTTO_LAB=veggies`) and otto loads every
host with `"veggies"` in its `labs` field.  The host **id** used by
`get_host()`, `--list-hosts`, and the rest of the CLI is derived from
the `ne`, `board`, and `neId` fields — `carrot` plus board `seed`
becomes `carrot_seed`.

### Per-host fields

Required:

ip
: IP or DNS name otto will connect to.

ne
: Network-element name.  Combined with `board` and `neId` to form the
  host's id.

creds
: Object mapping usernames to passwords.  At least one user must be
  present.  When `user` is unset, the first entry is used.

labs
: List of lab names this host belongs to.  Without this, the host is
  invisible to `--lab`.

Common optional:

board
: Board type, included in the host id when set.

neId
: Numeric identifier for disambiguating multiple instances of the same
  NE.

user
: Pin a specific user from `creds`.

term
: Terminal protocol — `"ssh"` (default) or `"telnet"`.

transfer
: File-transfer protocol — `"scp"` (default), `"sftp"`, `"ftp"`, or
  `"nc"`.

hop
: Host id of an intermediate jump host.  Otto opens an SSH tunnel
  through it and routes all subsequent connections — SSH, telnet, SFTP,
  FTP, netcat — through that tunnel automatically.  Hops can chain.

resources
: List of free-form resource tags used by the reservation backend.

is_virtual
: `true` when the host is a VM.  Read by callers that want to skip
  bare-metal-only operations.

log, log_stdout
: Both default to `true`.  Set to `false` to silence per-host output.

toolchain
: Object describing per-host gcov/lcov binaries for coverage runs.  See
  {ref}`per-host-toolchain`.

Per-protocol option tables (each optional):

ssh_options, telnet_options, sftp_options, scp_options, ftp_options, nc_options
: Override individual protocol fields without restating defaults.  Per-key
  merge with repo-level `[host_defaults]`; per-host values win.  See the
  {ref}`connection options reference <connection-options>` for every
  field, and the
  [connection-options cookbook](../cookbook/connection-options.md) for
  worked examples.

A fuller entry exercising the common options:

```json
{
    "ip": "10.10.200.13",
    "ne": "pepper",
    "board": "seed",
    "neId": 2,
    "user": "vagrant",
    "term": "ssh",
    "transfer": "nc",
    "hop": "carrot_seed",
    "creds": { "vagrant": "vagrant", "test": "Password1" },
    "resources": ["pepper", "gpu"],
    "labs": ["veggies", "smoke"],
    "is_virtual": true,
    "ssh_options": {
        "port": 2222,
        "connect_timeout": 5.0,
        "local_forwards": [
            { "listen_host": "localhost", "listen_port": 8080,
              "dest_host": "web.internal", "dest_port": 80 }
        ]
    },
    "nc_options": {
        "exec_name": "ncat",
        "port_strategy": "proc"
    },
    "toolchain": {
        "sysroot": "/opt/arm-toolchain"
    }
}
```

(host-defaults)=

### Repo-level host defaults

Most labs share a common set of connection conventions — a
non-standard SSH port, a longer connect timeout, an alternate `nc`
binary, etc.  Restating those values on every host entry is repetitive
and error-prone.  Move the shared values into
`[host_defaults]` in `.otto/settings.toml` and let the per-host
`*_options` tables override only the values that genuinely differ.

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

Merging happens **per key** at every layer.  Setting only `port` on a
host still inherits `connect_timeout` from the repo default; setting
only `connect_timeout` in the repo default still inherits `port` from
the dataclass default.

The merge is performed at host construction time, so the resulting
`RemoteHost` carries the fully-resolved `*_options` instances —
nothing has to be re-resolved at use time, and every storage backend
that goes through `create_host_from_dict()` (JSON today, anything
implementing `LabRepository` tomorrow) gets the behavior for free.

### Merging labs

Pass multiple lab names to combine them:

```bash
otto --lab lab_a,lab_b test TestDevice
```

Hosts from all labs are merged into a single lab.  If two labs define the
same host ID, the later lab's definition wins.

### Exploring labs

```bash
otto --lab my_lab --list-labs      # list all available lab names
otto --lab my_lab --list-hosts     # list host IDs in the loaded lab
otto --lab my_lab --show-lab       # full lab details (use -v for expanded output)
```
