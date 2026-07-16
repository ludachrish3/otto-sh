# The host subsystem

`otto.host` is the largest package: it turns a lab-data entry into a live
object you can run commands on, move files to, elevate privileges on, and
power-cycle — over SSH, Telnet, a serial console, or `docker exec`.

## Class hierarchy

The concrete host classes and every base between them and
{class}`~otto.host.host.BaseHost` — generated from the live classes at build
time, so this diagram tracks the code (each node links to its API page):

```{inheritance-diagram} otto.host.unix_host.UnixHost otto.host.embedded_host.ZephyrHost otto.host.local_host.LocalHost otto.host.docker_host.DockerContainerHost
:parts: 1
:top-classes: otto.host.host.BaseHost
```

What each layer adds:

| Class | Adds |
| --- | --- |
| `Host` (Protocol) / {class}`~otto.host.host.BaseHost` | the structural contract; shared verb logic, dry-run + log gates |
| {class}`~otto.host.remote_host.RemoteHost` | lazy-connect `ConnectionManager`, products |
| {class}`~otto.host.unix_host.UnixHost` | SSH/Telnet sessions, file ops, privilege, kernel modules, toolchains |
| {class}`~otto.host.embedded_host.EmbeddedHost` | console-only exec semantics, binary load/unload, on-device filesystems |
| {class}`~otto.host.embedded_host.ZephyrHost` | Zephyr RTOS defaults |
| {class}`~otto.host.local_host.LocalHost` | subprocess on the machine otto runs on |
| {class}`~otto.host.docker_host.DockerContainerHost` | `docker exec` via a parent `UnixHost` |

{class}`~otto.host.local_host.LocalHost` exists so instructions can mix local
build steps with remote deployment through one interface; every lab gets a
built-in `local` host, excluded from fleet iteration by default
({doc}`../lifecycle`). {class}`~otto.host.docker_host.DockerContainerHost`
delegates everything to a parent `UnixHost` rather than duplicating the
transport stack — that design has its own page: {doc}`docker-hosts`.

## The CLI layer: verbs from methods

`otto host <id> <verb>` is not a hand-written command set: every verb is
synthesized from a `@cli_exposed` coroutine method on the resolved host's
class. The Python API is the source of truth and the CLI is a projection of
it — add a method, get a subcommand.

The group behind `otto host` (`HostGroup` in `otto/cli/expose.py`) collects
`@cli_exposed` methods across every registered host class — built-in and
project-registered alike — and filters the visible, dispatchable set to the
verbs defined on the class of the host actually named on the command line.
Scoping falls out of method *definedness*: `UnixHost` defines `lsmod`, so
`otto host router1 lsmod` exists; an `EmbeddedHost` doesn't, so the verb
isn't offered there. A project that registers `MyHost` with a `@cli_exposed`
method gets its verb with no extra wiring — the same first/third-party
symmetry as everywhere else ({doc}`registries`). Each verb's flags come from
the method's own signature, via the same options-to-parameters machinery
instructions and suites use; `Arg`/`Opt`/`Exclude` annotations fine-tune the
CLI projection without touching the Python call shape.

A verb's return value is rendered by one shared path: members of the
{class}`~otto.result.Result` family drive both output and the exit code
(`result.exit_code`, ssh-like semantics — {doc}`../utilities/results`);
`None` means side-effect-only success; any other value is the documented
third-party fallback, printed as-is with exit `0`. Command output itself
streams live during execution, so a successful `run` verb prints nothing
extra at the end — the user-facing exit-code table lives in
{doc}`../../guide/hosts/index`.

Tab completion mirrors the synthesis model at every position: host ids come
from the completion cache's snapshot, falling back to a live lab scan on a
cold cache (completion never runs user code, {doc}`registries`); once a host
id is typed, the verb candidates are that host's class menu — the same
definedness scoping that decides what is dispatchable; and option values
backed by registries complete from the registry, so a project-registered
term backend completes exactly like a built-in one. See these captured live
in {doc}`../../guide/hosts/index` and {doc}`../../guide/hosts/connections`.

What is unique about `host` among the first-party commands:

- The full preamble applies, but *read-only* verbs (`ls`, `exists`,
  `read-file`, …) are registered with `output_dir=False` — inspecting a host
  should not litter `--xdir` with empty run directories.
- Per-invocation `--term` / `--transfer` / `--hop` overrides apply option
  overlays to the one resolved host before the verb runs
  ({doc}`../../guide/hosts/connections`).

## Sessions: persistent `run` vs stateless `exec`

{meth}`~otto.host.host.Host.run` executes on the host's **persistent shell
session** ({class}`~otto.host.session.HostSession`): working directory,
environment, and elevation state survive across calls, and the session's
`expect`/`send` primitives are available for interactive flows. Named
sessions can be opened explicitly (`open_session`) for parallel stateful
streams.

{meth}`~otto.host.host.Host.exec` runs each call independently of the
persistent session *and* of other concurrent `exec` calls, which is what
makes `asyncio.gather()` fan-out safe. Embedded hosts are exec-only —
a serial console has no multiplexed channels to hold a session on.

Two pieces of per-session state matter architecturally:

- **`current_user` and elevation.** Privilege changes (`su`, `sudo`,
  `switch_user`) are session state, tracked per session rather than per host —
  two sessions on one host can run as different users. The host's
  `current_user` property reports its default session's effective user.
- **Command framing.** How a command is wrapped, echoed, and its completion
  detected is a shell-dialect concern, factored into
  {class}`~otto.host.command_frame.CommandFrame` — a small **stateless value
  object** the session *holds* rather than *is* (`BashFrame` for POSIX
  shells, `ZephyrFrame` for the Zephyr shell). Per-session sentinels are
  passed in as values, keeping frames pure and unit-testable without a live
  session.

## Connections, terms, and hops

{class}`~otto.host.connections.ConnectionManager` owns a host's network
resources and builds them lazily — constructing a host object opens nothing;
the first verb that needs a connection does. The *term* (interactive
transport: `ssh` or `telnet`) is pluggable through the `TERM_BACKENDS`
registry, with `TermContext` — a frozen dataclass of construction inputs —
as the public seam a custom backend implements against.

Hops are first-class: a host whose `hop` field names another lab host is
reached by tunneling through that host's SSH connection
({class}`~otto.host.transport.HopTransport`), and hops chain for multi-hop
paths. The hop chain lives *below* the term/transfer layer, so every backend
— including file transfers and netcat streams — works through hops without
special-casing.

## File transfer and capability resolution

All transfer backends live in `otto.host.transfer`, one class per selector,
sharing {class}`~otto.host.transfer.base.BaseFileTransfer`. Each backend
declares which `host_families` it serves (`sftp`/`scp`/`ftp`/`nc` for Unix
hosts; `console`/`tftp` for embedded targets).

Which backend a host actually uses is resolved from three inputs:

1. **The host's menu** — `valid_transfers` / `valid_terms` in lab data
   declare what the machine supports.
2. **Preferences** — `[host_preferences]` tables (from lab data and product
   repos) rank capabilities per host selector; the first preference present
   in the menu wins, and product preferences take precedence over lab
   preferences.
3. **Per-invocation overrides** — `--transfer` / `--term` on the CLI, or
   keyword overrides on `get_host()`, have the final word.

The same mechanism resolves per-protocol option tables (e.g. `ssh_options`),
so "prefer netcat on this board family, with these ports" is data, not code.
See {doc}`../../guide/hosts/configuration` for the user-facing rules.

## From lab data to a host object

Host construction is a boundary crossing, described fully in
{doc}`data-boundary`. In brief: the `os_type` field selects an
{class}`~otto.host.os_profile.OsProfile` — a named bundle of field defaults
over a *base family* (`unix`, `embedded`) — the profile picks the host class
and its pydantic spec, defaults and host fields are merged (host fields win),
the spec validates, and `to_host()` builds the runtime object. Custom host
classes and profiles register through `register_host_class` /
`register_os_profile` ({doc}`../../guide/hosts/os-profiles`).

Profiles are the **data** half of otto's customization split: they name a
bundle of defaults many hosts share. The **code** half is products —
{class}`~otto.host.product.Product` bundles stage/install/uninstall behavior,
and product repos attach products to hosts by registering a *provider
function* (`register_product_provider`) that otto applies to each host at
ingest. Declaring products in lab data is deliberately not supported: lab
data stays product-agnostic and the two evolve independently.

## Embedded strategies

Embedded hosts compose three more stateless strategy objects, each with its
own registry ({doc}`registries`):

- {class}`~otto.host.command_frame.CommandFrame` — the shell dialect.
- {class}`~otto.host.binary_loader.BinaryLoader` — how a binary payload gets
  onto the target and verified (`llext-hex` drives Zephyr's LLEXT loader over
  the console).
- {class}`~otto.host.embedded_filesystem.EmbeddedFileSystem` — what on-device
  filesystem (if any) transfers and file ops may assume: FAT on a RAM disk,
  LittleFS, or none, with graceful degradation.

Power control ({class}`~otto.host.power.PowerController`) and privilege
escalation (`otto.host.privilege`) follow the same pattern: an abstract
strategy, a registry, and per-host selection from lab data.

## Where the code lives

- `otto.host` — host classes, sessions, connections, transfer, and the
  embedded strategy objects
- `otto.cli.expose` — `HostGroup` and the `@cli_exposed` verb synthesis and
  completion behind `otto host`
- {mod}`otto.result` — the `Result` family that renders verb output and
  derives exit codes
