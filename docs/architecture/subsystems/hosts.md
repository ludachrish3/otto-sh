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

`BaseHost` and `RemoteHost` are `kw_only` dataclass bases, and each shared
field — its type, its docstring, its default — is declared exactly once, on
the base that owns it. A subclass re-declares a field only to change its value
policy, and the re-declaration carries no docstring, so the field's
documentation has one home. A test holds otto's own host classes to that
shape.

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
instructions and tests use; `Arg`/`Opt`/`Exclude` annotations fine-tune the
CLI projection without touching the Python call shape.

A verb's return value is rendered by one shared path: members of the
{class}`~otto.result.Result` family drive both output and the exit code
(`result.exit_code`, ssh-like semantics — {doc}`../utilities/results`);
`None` means side-effect-only success; a `bool` (the predicate verbs) prints
and exits `0`/`1` for `True`/`False`; any other value is the documented
third-party fallback, printed as-is with exit `0` ({ref}`cli-return-values`).
Command output itself streams live during execution, so a successful `exec`
verb prints nothing extra at the end — the user-facing exit-code table lives in
{doc}`../../cli/host/index`.

Tab completion mirrors the synthesis model at every position: host ids come
from the completion cache's snapshot, falling back to a live lab scan on a
cold cache (completion never runs user code, {doc}`registries`); once a host
id is typed, the verb candidates are that host's class menu — the same
definedness scoping that decides what is dispatchable; and option values
backed by registries complete from the registry, so a project-registered
term backend completes exactly like a built-in one. See these captured live
in {doc}`../../cli/host/index` and {doc}`../../cli/host/connections`.

What is unique about `host` among the first-party commands:

- The full preamble applies, but *read-only* verbs (`ls`, `exists`,
  `read-file`, …) are registered with `output_dir=False` — inspecting a host
  should not litter `--xdir` with empty run directories.
- Per-invocation `--term` / `--transfer` / `--hop` overrides apply option
  overlays to the one resolved host before the verb runs
  ({doc}`../../cli/host/connections`).

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

Both return the command's exact output, whichever route carried it;
{ref}`command-output-contract` says what that means and what otto undoes to
get there.

(host-exec-pool)=

### The exec pool: one free-list per user

Where an `exec` cannot have a channel of its own (telnet, a proxied login, a
`session_setup` hook, a call that needs a shell), it runs on a pooled shell
session, and `SessionManager` keeps one free-list of idle ones per user
(`_exec_pools`, keyed by `_pool_key`: the call's `user`, else the login, so
`exec()` and `exec(user=<the login>)` share one). A session for another user
is opened **for** them, in `SessionManager.open_session`'s order: their own
SSH login when they have a direct credential on an SSH host (no proxy hops),
otherwise the login's session with its proxy hops and then a switch to them;
then the session-setup hook, which therefore sets the session up as them.
Nothing is switched per call, and a switch that cannot be made fails the
open. On a `console` term there is no pool: `exec(user=...)` switches the
one session inside its line turn ({ref}`console-term`).

Retention: a session goes back to its user's free-list after every `exec`
that leaves it alive, still that user, and with its terminal record intact,
when it does not need an identity guard its frame cannot give (below);
anything else closes it. Nothing is evicted, so each free-list holds at most
that user's peak of concurrent `exec` calls, and `close_all` (host close)
closes them all.

A session reached by a switch (any login-proxy hop, `su`, `sudo su` or
several, or the switch to the call's user) has another shell underneath it,
and that shell would answer if the switched one ended (an idle `TMOUT`, a
kill). So, after the setup hook and its frame entry, otto proves once who
the shell it will type into is (a different answer fails the open) and,
on that same probe line, gives it a mark: an alias named for a random
token (`identity_mark_statement`). A shell that cannot name itself (no
`id`) answers nothing: it gets no mark, runs its `exec` on the switch's
word alone, as an ordinary switch there does, and is closed, never kept. The shell underneath never has
it; no shell passes an alias to a program it starts; and the guard's query
(`alias NAME`, a builtin) answers only to that alias, never to a function
or an executable on `PATH` of the same name. A command run inside the
isolating exec frame cannot remove it from the session's shell; on a frame
that does not isolate `exec`, a command that removes it makes the next call
read the mark as missing. Every `exec` on the session is then typed with
the frame's guard (`identity_guard`, an `if` on that query: no extra round
trip, no fork). Where the mark is missing, the command is not run: the
session is closed and the call runs once on a fresh session for the user,
and a fresh session that has lost the user too raises
{class}`~otto.host.login_proxy.LoginProxyError`. A session whose frame has
no guard (an application dialect a hook entered) runs its `exec` and is
closed, never kept: it works, it is only not reused. A session on its own
direct connection (the login itself, the user's own SSH login) carries no
mark and pays nothing. Known gap: the guard heads its own physical line
before the command's, so a switched shell that ends after reading that
line and before the rest arrives leaves the rest to the shell underneath. The
guard also trusts the shell underneath to keep `alias` a builtin: a login
profile that defines a function of that name defeats it.

(host-line-turn)=

### Concurrency and the line turn

On most hosts an `exec` has a process or a channel of its own, so gathered
`exec` calls run at the same time. A host whose commands all share one line
cannot do that: a unix host on a `console` term (`run` and `exec` both type
on the console's one shell) and every embedded host (one console, nothing
beside it). One decision says which hosts these are,
`BaseHost._shares_one_line()`, and on them every call takes **the line
turn** (`BaseHost._line_turn()`) and holds it until it ends, so gathered
calls run one after another and each gets its own result. The
`exec-concurrent` contract checks that on every cell.

Every host-level entry point that types on the host enters it through one
sequence, `BaseHost._host_entry()`: `run`, `exec`, an embedded host's
`exists`, `ls` and `rm`, the commands its file transfer and binary loader
type, and the userland's probes. In order:

1. the loop claim (`_claim_loop`), which starts a new turn when the host
   moves to a new event loop;
2. the refusal of a hook's re-entry (below);
3. the turn itself, before anything opens the session, and so before the
   connection manager's own console lock.

The turn is re-entrant within its holder's context, while the holder
holds it. So `run(user=...)` holds one turn across the switch, its commands
and the switch back, and an `exec(sudo=True)` holds one across the userland
probes and the sudo probe it may make (each itself an exec on that session)
and its command: no other call runs on the line between them. A task
created inside the turn (the task `asyncio.wait_for` runs a probe in on
Python 3.10 and 3.11) re-enters it; a task that is still waiting when its
holder has left waits for the turn like any other caller. One lock order
holds everywhere: the line turn first, then the userland's lock, then the
connection manager's console lock.

What a caller holding the turn is responsible for: tasks it gathers inside
the turn all re-enter it together, so they are not serialised against each
other; and a task that re-entered and is still running when its holder
leaves keeps the line, beside the next holder. Inside a turn, await the
tasks you start one at a time, and before you leave it.
A call cancelled while it waits never takes the turn; one cancelled
while it holds it gives it up only after its command is stopped. The
commands an embedded host's file transfer and binary loader type take the
turn one command at a time, since a gathered transfer runs its files in
tasks of their own.

`send`, `expect` and a scoped `as_user` block do not take the turn (`send`
and `expect` do take the re-entry refusal): a
send/expect dialogue spans several calls, and an `as_user` block runs the
caller's own code, which may gather calls of its own. Calls made inside an
`as_user` block take the turn each, as anywhere else.

(session-setup-reentry)=

### A session-setup hook calling back into its host

A session-setup hook runs inside the call that opens its session. A hook
that calls back into its own host (`run`, `exec`, `send`, `expect`, or an
embedded host's file operations, transfers and loader) would wait for the
session it is opening (and, on a shared line, for the turn its caller
holds) forever. otto refuses that call at once instead, with a
{class}`~otto.host.errors.SessionSetupError` that says so: "session_setup
called host.exec() while the session it sets up is opening (directly or
through another host's hook); use the session handle the hook receives".
The same holds for any host still opening above the hook: host A's hook
calls host B, whose hook calls A, and the call into A is refused rather
than deadlocking. A task the hook starts is refused the same way while the
hook runs, and not after it has ended. The handle the hook receives is the
way to type into the session during setup.

The opening hosts are claims carried in the context (`ContextClaims` in
`otto.host.host`, the same mechanism the line turn's holds use): a claim is
shared by every task that copied it and reads released on every exit of the
hook, return, exception or cancellation.

A session opened for a user (an exec-pool session, a named session opened
with `as_user=`) is that user for its whole life. A hook that leaves such a
session as someone else is refused after it returns, before anything is
typed: the session is closed, no pool entry is kept, and the
`SessionSetupError` names the host, the hook, the user the session was
opened for and the user the hook left. Scope a switch inside the hook with
the handle's `as_user`, or ask for the user at the call (`exec(user=...)`).

Three pieces of per-session state matter architecturally:

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
  session. A frame's render half and parse half live on one object because
  they co-vary through *where the retcode lives*; splitting them would let
  mismatched halves combine.
- **Establishment.** A session is opened in a fixed order: the transport
  (through every `hop`), the readiness handshake in the *landing* frame,
  every login-proxy hop, an optional lab-declared session-setup hook
  ({mod}`otto.host.session_setup`), and **frame entry** — the
  `command_frame`'s own handshake, run after every hook unconditionally, so
  a hook that manoeuvred the console into an application hands over a shell
  that has been confirmed in that application's dialect.
  `ShellSession.open_transport()` and `ShellSession._handshake()` are the two halves;
  frame entry re-runs only the second.

Every Unix shell otto opens also suppresses its own shell history
({ref}`per-host-shell-history`), so automation traffic does not bury a human's
history on a shared lab box. otto neutralizes `HISTFILE` rather than clearing
`HISTSIZE`: `HISTSIZE=0` would make bash write its emptied history list *over*
the history file at exit, destroying the user's real history. Suppression is
best-effort and silent by design, and every part of it is guarded. The guards
are load-bearing rather than decorative — POSIX makes *both* an error in a
special builtin and a failed variable assignment abort the line, either of
which would strand the readiness probe that shares it and take the host offline
— so they must not be simplified away. `otto login` is excluded on purpose: it
hands the user a real shell, and silently losing up-arrow recall would be worse
than the noise. The cost is that a login-proxy resync probe, or a session-setup
hook's commands, land in that shell's history; the two cannot both be had,
since suppressing the probe means suppressing the user's history for the whole
session.

A login proxy declares the password prompt its mechanism can raise, and the
session engine — not the proxy — answers it, because whether `su` challenges
depends on **who is asking**, not on the cred: the same `mysql` entry prompts
when reached from an unprivileged account and stays silent when reached from
`root`, and a cred cannot know which hop it is on. A proxy that waited whenever
a password was configured would stall every switch the host was willing to
perform for free, for the whole of a command timeout. So each hop's resync
spends its settle *watching* for the declared prompt, and a password is sent
exactly once, only when wanted; a cred with no password gets an error naming
the account rather than letting otto's own probes spend authentication
attempts against it, which is how a lockout policy gets tripped by automation
that meant no harm.

Because the resync proves the identity each hop leaves the shell in, the
watch is armed only on hops that can be challenged (root is not), which
removes a wait from every uncontested switch. The watch is never merely
*shortened* instead: a prompt arriving after a too-short wait would meet
otto's first probe, and a probe typed at a live password prompt is itself a
failed authentication.

The built-in `"su"` proxy uses the login-shell form, `su -`, because the
accounts that need a proxy are usually service accounts whose own environment
is the point; plain `su` hands them the caller's `PATH`, `HOME` and `USER`
without sourcing their profile.

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
   projects) rank capabilities per host selector; the first preference present
   in the menu wins, and product preferences take precedence over lab
   preferences.
3. **Per-invocation overrides** — `--transfer` / `--term` on the CLI, or
   keyword overrides on `get_host()`, have the final word.

The same mechanism resolves per-protocol option tables (e.g. `ssh_options`),
so "prefer netcat on this board family, with these ports" is data, not code.
See {doc}`../../configuration/host-options` for the user-facing rules.

The netcat backend's built-in listener checks count listeners rather than
merely detecting one. Some netcats (OpenBSD `nc` among them) bind with
`SO_REUSEPORT`, so a second process that chose the same port at the same
moment gets a listener beside otto's instead of an address-in-use error, and
the kernel then decides which of the two receives the connection. That is why
a port held by more than one listener is refused and retried on a fresh one
({doc}`../../cli/host/netcat`).

The netcat backend's remote listener is ended by otto reaping it on every error
path and by the `timeout` prefix its spawn carries — never by `nc -w`, which
otto does not emit. Measured on 2026-08-25, `-w` bounds the idle time of an
*accepted* connection, so it kills a stalled transfer with a success code and a
partial file.

## From lab data to a host object

Host construction is a boundary crossing, described fully in
{doc}`data-boundary`. In brief: the `os_type` field selects an
{class}`~otto.host.os_profile.OsProfile` — a named bundle of field defaults
over a *base family* (`unix`, `embedded`) — the profile picks the host class
and its pydantic spec, defaults and host fields are merged (host fields win),
the spec validates (structurally, then in its context), and `to_host()`
builds the runtime object from the record that validation issued. Custom host
classes and profiles register through `register_host_class` /
`register_os_profile` ({doc}`../../configuration/os-profiles`).

Profiles are the **data** half of otto's customization split: they name a
bundle of defaults many hosts share. The **code** half is products —
{class}`~otto.host.product.Product` bundles stage/install/uninstall behavior,
and product projects attach products to hosts by registering a *provider
function* (`register_product_provider`) that otto applies to each host at
ingest. Declaring products in lab data is deliberately not supported: lab
data stays product-agnostic and the two evolve independently.

A provider is gated by its project's `[project]` declaration *before* it is
called, never filtered after: a provider that ran has already been handed a
machine its project never declared, and providers inspect hosts and keep their
own state. Where the gate cannot compute a narrowing it narrows nothing. A
host with no lab attribution predates scoping and is admitted, and a provider
whose project otto cannot resolve still runs, because refusing it would turn
"otto could not find its config" into "your host has no products" — the same
silent wrong answer scoping exists to prevent, pointed the other way.

Dev tools share the product shape but live in a separate registry rather
than behind a flag on one list, because their lifecycle differs: one shared
list would make an uninstall remove dev tools as if they were products, and
make a host carrying nothing but a debug probe read as
installed to `otto run status`. The two tooling kinds install with
asymmetric defaults on purpose: dev tools are small and wanted on nearly every
run, while toolchain artifacts are large and rarely needed, so asking for them
(`install-tools --toolchain`) is a decision.

A **kind** is a further split within products: the runtime knowledge behind
a `[[products]]` entry's verbs — install, check, uninstall — and how its
coverage counters are collected. The same naming rule governs every kind: a
kind is named for **what drives its verbs**, never for the artifact's
format. {doc}`../../configuration/declared-products-tools`'s kinds
table is the one home for the built-ins and how a project registers another.

A `docker_image` product's `pull` defaults to `false` because a lab is
commonly air-gapped by policy: a `docker pull` on every install would
routinely reach for a registry the lab cannot see, and hang behind a pull
that was never going to land instead of failing loud at `install`.

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
