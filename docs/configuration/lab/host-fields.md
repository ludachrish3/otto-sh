# Host fields

## Per-host fields

A host entry is one addressable endpoint of its element.  `element`,
`element_id`, and `labs` are not host fields: the first is the element's
`name`, the second its `id`, and `labs` its membership.  A host entry
carrying any of them fails the load naming the key.  `resources` *is* a
host field — the third reservation level, beside the
[labs table](files-and-membership.md#the-labs-table)'s and the [element](elements.md#elements)'s.

### Required

| Field | Type | Description |
|-------|------|-------------|
| `ip` | string | IP address or DNS name otto will connect to. |

### Common optional

| Field | Type | Description |
|-------|------|-------------|
| `creds` | array of objects | Ordered list of `{"login": ..., "password": ...}` entries — a protocol logs in as the first entry that applies to it ({ref}`cred-protocols`).  Usually present, but optional.  With none, otto by default authenticates with an empty login (an ssh login can instead come from `username`/`password` in [`ssh_options.extra`](../host-options.md#ssh)): ssh is refused by the far end (the error names the address, not the otto host), and telnet and `console` still run their login step unless that term's `login` is `false` (see `login` under [Telnet](../host-options.md#telnet) and {ref}`Console <console-term>`).  So by default a host needs no creds only when its term has no login step and sets `login: false`, and even then file transfers (scp, sftp, ftp) authenticate on their own transport, so a `put` can still fail.  Embedded hosts always skip the login step (RTOS telnet shells typically have none).  A referenced host may instead get its creds from the inventory or the creds store.  An entry may also carry `proxy`/`via`/`params` to describe a login-proxy hop, or `protocols` to scope it to the services it is for.  On a **referenced** host the list layers over the inventory's and the creds store's entries by identity (login plus `protocols` scope); its order is the login order (see {ref}`credentials-layered`).  Defaults to empty. |
| `name` | string | Display-name override.  Otto derives a label from the element name, `board`, and `slot`, exactly as written; setting `name` replaces that label entirely.  It does **not** change the host id. |
| `metadata` | object | Opaque user data — the home for custom fields; any other unknown key fails the load.  Otto never reads it.  Surfaces as `host.metadata`; the element's as `host.element.metadata`; the lab's as `host.lab_info.metadata`. |
| `resources` | array of strings | This host's own reservation identifiers — a slot.  Combined with the element's and the lab's; see {doc}`../../cli/reservation/index`.  Defaults to empty. |
| `board` | string | Board type, included in the host id when set. |
| `term` | string | Terminal protocol lab pin — must be in the host's `valid_terms` menu; a pin outside it is refused when the lab file is validated (`otto init`'s doctor, host listing, load).  Product `[host_preferences]` and CLI `--term` can override; see the precedence chain in {ref}`host-preferences`. |
| `transfer` | string | File-transfer protocol lab pin — must be in the host's `valid_transfers` menu; a pin outside it is refused when the lab file is validated (`otto init`'s doctor, host listing, load).  Product `[host_preferences]` and CLI `--transfer` can override; see the precedence chain in {ref}`host-preferences`.  On a `console` term `nc` is never chosen: its remote listener needs a second command channel, and a console serves one client.  An `nc` entry in `[host_preferences]` falls through to the next one, and a pin of `nc` (or a menu holding only `nc`) fails the load. |
| `impairer` | string | Link-impairment backend lab pin — must be in the host's `valid_impairers` menu (Unix hosts only); a pin outside it is refused when the lab file is validated (`otto init`'s doctor, host listing, load).  Product `[host_preferences]` can override.  See {doc}`../../cli/link/index`. |
| `valid_terms` | array of strings | Ordered list of term backends that may be selected for this host (gates `--term` and `[host_preferences]`).  Defaults to `["ssh", "telnet"]` for Unix hosts and `["telnet"]` for embedded hosts.  Custom backends registered via `register_term_backend` also appear. |
| `valid_transfers` | array of strings | Ordered list of transfer backends that may be selected for this host (gates `--transfer` and `[host_preferences]`).  Defaults to `["scp", "sftp", "ftp", "nc"]` for Unix hosts and `["console"]` for embedded hosts.  Custom backends registered via `register_transfer_backend` also appear. |
| `valid_impairers` | array of strings | Ordered list of impairer backends that may be selected for this host (gates `impairer` and `[host_preferences]`).  Unix hosts only; defaults to `["netem"]`.  Custom impairers registered via `register_impairer` are valid entries too. |
| `slot` | integer | Physical slot number of the board to which this host belongs.  Appended to the host id, but only when `board` is also set — see {ref}`host-identity`. |
| `site` | integer or string | Site the host is installed at — a name or a number (a digit-only string like `"3"` reads as `3`).  Not part of the host id. |
| `rack` | integer or string | Rack within the site — a name or a number, coerced the same way as `site`.  Not part of the host id. |
| `shelf` | integer | Shelf / rack position.  Not part of the host id. |
| `hop` | string | Host id of an intermediate SSH jump host.  Otto opens an SSH tunnel through it and routes all subsequent connections automatically.  Hops can chain. |
| `inventory` | string | Key of the inventory record this host is resolved from — see [Referencing the inventory](inventory-references.md#referencing-the-inventory) and {doc}`../inventory`.  Inventory-owned fields must then be absent here. |
| `default_dest_dir` | string | Directory an empty or relative `put`/`get` destination resolves against.  Defaults to empty — SCP/SFTP then land in the login user's home directory (or the authenticated user's home when `--user` is given), and an embedded host with a mounted filesystem falls back to its mount point.  It doubles as the staging directory for a product or dev tool that declares no `stage_dir` of its own ({doc}`../declared-products-tools`), and in THAT role it must be **absolute**: staging has to name one directory to both the transfer that puts the artifact and the command that installs it afterwards, so a relative value is refused. Leave it empty and the login user's home is used instead. |
| `max_filename_len` | integer | Longest basename the host's filesystem accepts.  Defaults to `255` (the Linux `NAME_MAX`, and the typical LittleFS ceiling); lower it where the firmware enforces a tighter limit — e.g. `32` on a Zephyr build with a short-name FAT. |
| `debug_log_globs` | array of strings | Remote log paths `get_debug_logs` fetches off this host.  A pattern (`*`, `?`, `[`) is expanded on the device itself, so embedded hosts — which have no shell to expand with — declare concrete paths.  See {doc}`../../cli/host/capabilities/index`. |
| `is_virtual` | boolean | `true` when the host is a VM or emulator. |
| `log` | string | Standing log disposition for this host's command I/O, named by its `LogMode`: `"normal"` (the default) logs everywhere, `"quiet"` keeps it in `verbose.log` but off the console, `"never"` redacts it from every sink.  Composed with each command's own mode, the more restrictive winning.  Booleans are no longer accepted — write the mode name. |
| `docker_capable` | boolean | `true` when this host can run Docker containers (Unix hosts only). |
| `docker_priority` | integer | Lab intent: ranks this host as the docker verbs' default parent ({ref}`Which host <docker-which-host>`). Defaults to `0`. Strictly an integer (`true` is refused), and the key is allowed only on a docker-capable host, even as `0`. A host is docker-capable when `docker_capable` is set, or when the host class's own default says so. |
| `has_bash` | boolean | `true` when the host has a working `bash` to `exec -a`-tag processes through. Gates which hosts can host or be scanned for `otto tunnel` tunnels — see {doc}`../../cli/tunnel/index`. Defaults to `true` for Unix hosts (including `local` and Docker containers), `false` for embedded hosts. |
| `shell_history` | boolean | Whether otto's own commands are recorded in this host's shell history (Unix hosts only). Defaults to `false` — otto neutralizes `HISTFILE` on each shell it opens so automation traffic doesn't bury a human's history. Set `true` where otto's commands should stay visible in the history file. See {ref}`per-host-shell-history`. |
| `init_timeout` | number | Seconds a newly opened shell has to answer otto's readiness probe (Unix hosts only). A number greater than 0; leave it out for the default, 10 s. Raise it for a slow login or a heavily loaded lab. See {ref}`per-host-init-timeout`. |
| `command_frame` | string | Shell-framing dialect (e.g. `"bash"`, `"zephyr"`, `"zephyr-serial"`). `"raw"` is refused here — it is a landing-only dialect, valid only as `landing_frame`. On an embedded host it is required unless the `os_type`'s host class supplies one (`zephyr` does). See {ref}`per-host-session-setup`. |
| `session_setup` | string or object | A registered session-setup hook that runs once on every shell session, after the readiness handshake and every login-proxy hop, with a real session handle: export variables, provision through an app shell, or manoeuvre into the application the `command_frame` describes. A string names the hook; an object names it in `type` and passes every other key as the hook's params. See {ref}`per-host-session-setup`. |
| `landing_frame` | string | Dialect of the shell otto *lands* in when it is not the shell `command_frame` describes — `"bash"` for a Linux login shell in front of a vendor CLI, `"raw"` for a landing that answers no frame at all (a boot menu). Only valid with `session_setup`, which does the manoeuvring; with a proxied cred the landing must be bash-family. See {ref}`per-host-session-setup`. |

The host field `roles` is removed and refused by name; which host runs docker is `--parent` or `docker_priority` ({ref}`Which host <docker-which-host>`).

### Host type / OS

| Field | Type | Description |
|-------|------|-------------|
| `os_type` | string | Profile selector.  Defaults to `"unix"`.  Resolves to a registered host class and optional defaults bundle — see {doc}`../os-profiles`. |
| `os_name` | string | Human-readable OS name (e.g. `"Linux"`, `"Zephyr"`). |
| `os_version` | string | OS or kernel version string (e.g. `"3.7"`, `"4.4"`). |
| `hw_version` | string | Free-form hardware version description.  Informational — otto never parses it. |
| `sw_version` | string | Free-form software version description — what the host is *declared* to run, never what a probe observed.  Informational — otto never parses it. |

### Option tables

Each of the following keys accepts an object that overrides individual
fields.  They merge per-key with hardcoded dataclass defaults;
product `[host_preferences]` values are then applied on top (product wins
over the host's own values).  See {doc}`../host-options` for the full
connection-options reference.

| Key | Covers |
|-----|----------|
| `ssh_options` | SSH (term and hop) |
| `telnet_options` | Telnet (term, and the embedded console) |
| `console_options` | Console (term; addresses the telnet server fronting a serial console, not this host).  Read from this entry only — not a `[host_preferences]` table |
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
