# The command lifecycle

Almost every `otto` invocation walks the same path before a first-party
command takes over: compose the process, dispatch one command, prepare the
invocation, run it, tear it down deterministically. This page covers that
shared path; the pages below cover what each command does once it has
control.

The exception is the console script's own front door. `[project.scripts]`
points at `otto._shim:main`, not at `entry()`: a bare `otto --version` is
answered there, off `otto.version` alone, and never enters the path below.
It exists because otto's framework import graph cost thousands of path
syscalls, every one paid at module import — before `entry()` runs a line,
so nothing inside `entry()` could have avoided them — see
{doc}`startup-performance` for the measured budget. Every other
argv, including `python -m otto`, is handed straight to `entry()` unchanged.

```{graphviz}
digraph lifecycle {
    rankdir=TB;
    node [shape=box];

    shim [label="otto._shim:main — console script\n--version answered here, CLI never imported", style=dashed];
    entry [label="entry() — the composition root"];
    completion [label="completion fast path\ncache hit → zero user code", style=dashed];
    discovery [label="bootstrap phase 1: discovery\nOTTO_* env + settings.toml\n(no user code runs)"];
    registration [label="bootstrap phase 2: registration\ninit modules + test files\n(per-file failures contained)"];
    dispatch [label="dispatch\nresolve only the target command;\nevery other command stays a help stub"];
    preamble [label="invoke preamble\nload + merge labs → OttoContext →\noutput dir + log sinks → reservation gate\n(lab_free commands skip lab and gate)"];
    body [label="command body\n(command-specific — pages below)"];
    teardown [label="teardown\nHostScope closes remaining hosts;\nexit code derived from the Result"];

    shim -> entry [label=" every argv but a bare --version"];
    entry -> completion [label=" completion request"];
    entry -> discovery;
    discovery -> registration;
    registration -> dispatch;
    dispatch -> preamble;
    preamble -> body;
    body -> teardown;
}
```

The teardown node's "exit code derived from the Result" is newer than the
rest of that picture: a command's return value became the process exit code
only once the leaf-invoke wrapper took over rendering. The seam is
`render_leaf_value` in `otto/cli/invoke.py`, which runs on the value the
lifecycle bridge awaited. A returned `Result` supplies its own `exit_code`.
`None` — what every side-effect-only first-party leaf returns — renders
**nothing** and exits `0`, unless the leaf installed a `RenderPolicy` with a
`none_message`; any other value prints as-is and exits `0`
({doc}`../library/extending-cli`).

The front door looks like this — and every terminal block in these docs is
**captured from the real CLI at build time** (a scaffolded demo repo, real
`--help` output, real completion candidates), so what you see here is what
the current code does:

```{raw} html
:file: ../_static/generated/termynal/help-otto.html
```

## Bootstrap: two phases, contained failures

{func}`otto.bootstrap.bootstrap` replaces what used to be import-time side
effects with an explicit composition root:

- **Phase 1 — discovery.** Parse the `OTTO_*` environment variables and every
  repo's `.otto/settings.toml` into an `OttoEnvSettings` plus a list of
  `Repo` objects. *No user code runs.* Environment-level failures raise —
  nothing can degrade gracefully if `OTTO_SUT_DIRS` itself is broken — but a
  single repo's malformed settings file is framed and skipped.
- **Phase 2 — registration.** Add each repo's `libs` to `sys.path`, import its
  `init` modules, and import its test files. Every user-module exec is wrapped:
  one broken file becomes a framed {class}`~otto.bootstrap.BootstrapError`
  in the returned {class}`~otto.bootstrap.BootstrapResult` instead of a
  traceback that bricks the process. The CLI prints one warning line per
  contained error; actually *dispatching* into broken code fails loud.

Test files are imported only from the top level of each directory a repo
lists under `tests`, never recursively, and the reason is blast radius rather
than speed: a test file that fails to import stops *every* command, so one
broken file under an unlisted subdirectory would take down `otto host list`.
Listing the directories keeps that surface one the repo chose. `otto test`
still hands the same directories to pytest, which recurses as usual.

`bootstrap()` is idempotent: the CLI entry point calls it before argv parsing,
{func}`~otto.context.open_context` calls it lazily for library users, and
repeated calls return the same result.

Lab loading is deliberately **not** part of bootstrap. `otto --help`,
`--list-*` flags, and shell completion never open `lab.json`, and a missing
or malformed lab file only matters once a command that needs the lab runs.

## The preamble, and who opts out

For CLI commands, the invoke preamble (`otto/cli/invoke.py`) runs just before
the leaf callback: load and merge labs (`--lab` may repeat), build and
install the {class}`~otto.context.OttoContext`, create the per-command output
directory and wire the log sinks ({doc}`utilities/logging`), and run the
reservation gate. Each first-party command declares what it needs on its
{class}`~otto.cli.registry.CommandSpec` ({doc}`subsystems/registries`):

| Command | Needs a lab | Output dir | Reservation gate |
| --- | --- | --- | --- |
| {doc}`run <subsystems/execution>` | yes | yes | yes |
| {doc}`test <subsystems/execution>` | yes | yes | yes |
| {doc}`host <subsystems/hosts>` | yes | yes | yes |
| {doc}`monitor <subsystems/monitoring>` | no (`lab_free`) — `--live` loads the lab itself; reviewing a saved source doesn't | yes | self-gated per branch: `--live` collection gates, reviewing a saved source doesn't |
| {doc}`docker <subsystems/docker-hosts>` | yes | yes | no — containers ride the parent's reservation |
| {doc}`cov <subsystems/coverage/index>` | yes | no — reads existing run dirs | no |
| {doc}`reservation <subsystems/reservations>` | no (`lab_free`) — `check` loads lab data itself | no | no — it *is* the gate, made inspectable |
| {doc}`schema <subsystems/data-boundary>` | no (`lab_free`) | no | no |
| {doc}`init <subsystems/bootstrap>` | no (`lab_free`) | no | no |

`--lab` itself tab-completes — the lab names come from the `labs` tables the
`lab.json` files declare, read data-only (no host construction, no user code),
and the option is `+`-separated so each segment completes in turn:

```{raw} html
:file: ../_static/generated/termynal/complete-lab-names.html
```

## Dry runs: one seam, no fabrication

The user-facing contract is on {doc}`../cli/dry-run`: a dry run never runs a
command on any device. It is stated that plainly because of what the
alternative looks like. When logic branches on a fact about a device there are
exactly three things a dry run can do: **fabricate the fact**, **decline
loudly**, or **never run the logic**. "Continue gracefully" is not a fourth
option — it is the first one wearing a kinder name. otto used to take it, and a
dry run would report every link clean, print the body of a file it had not
written, and accuse a host it had never contacted of missing `socat`. The
design below is the consequence of removing that option; the result type that
carries a declined command is {class}`~otto.result.NotRunResult`
({doc}`utilities/results`).

**The default stop lives in dispatch, not in each command.** Under `-n` the
dispatch layer validates, prints what would run, and exits before the command
body runs, so a command author who does nothing at all gets a correct dry run
and cannot get it wrong. The alternative, where every author has to keep dry
runs in mind while writing run-parse-branch logic, produces a feature that is
subtly broken in a different way in each command, and a consistently broken
complex feature is worse than a reliable simple one. For `otto test -n` the
suite imports and binds and no step runs: steps not running *is* the feature,
not a missing preview.

**The stop applies to `lab_free` commands too, with no carve-out.**
`lab_free` means "this command drives its own lifecycle", not "this command
touches no device": `otto monitor --live` is registered lab-free and collects
metrics from every host in the lab, so a carve-out letting lab-free commands
run their bodies under `-n` would exempt precisely the command that can still
reach a device while the user believed nothing would. One rule with no
exceptions is also the only version a third-party author can hold in their
head. A command that genuinely should run under `-n` opts in instead, with
`dry_run_preview=True` ({doc}`../cookbook/dry-run-contract`).

**`--probe` permits a connection, never a command.** A connection attempt
produces no command result, so it feeds no `if result.is_ok:` and no parser —
the fabrication hazard is untouched. The probe opens *and authenticates*
because it answers "would this run's connect phase succeed?": anything
narrower — a bare TCP connect, or the terminal channel alone — would report a
reachability the real run is not going to get, and calling a host that refuses
the login `reachable` would be the contract's own defect in a smaller font.
Authenticating puts telnet and FTP credentials on the wire, which is why the
flag is opt-in rather than part of `--dry-run`, and it requires `--dry-run`
because dialing is only safe when no command can follow it. `not probed` is a
state of its own because "we could not ask" and "we asked and it said no" are
different facts, and only one of them is about the host.

**A declined result cannot pass for a measurement.** A declined command
carries `Status.NotRun` rather than reusing `Status.Skipped`, because
`Skipped.is_ok` is `True` and must stay that way for genuine skips — a skipped
test step, a folded transfer. Reading a declined result's `.value` raises, so
the always-wrong case, parsing a non-measurement as data, breaks at the line
that made the mistake. A verb whose return type has nowhere to put "I did not
look" raises rather than fabricate: a `bool` has only `True` and `False`, and
both are lies — `exists` returning `False` reports absent a path that may well
be there, `ls` or `glob` returning `[]` is a fabricated empty directory or
"nothing matched" (the shape a log collector reads as "this host has no
logs"), `toolchain_tools_absent` returning `True` would report clean a host
nobody looked at, and `expect` returning `""` is a fabricated prompt.

**Suppress the payload, never the announcement.** A dry run with no output is
a bug: a preview with no product is useless, and one with an invented product
is dangerous. The announcement is printed from the renderer as well as the
library layer so that a console product is guaranteed even for a host whose
standing log mode is quiet. A verb opts into `dry_run_preview` only when it
has a real plan to show. `otto host run` does not opt in — running its body
would widen the surface for no gain — and neither does `ls`, because a
directory listing is precisely what a dry run cannot honestly produce.

**One declared exemption.** Reading otto's own SUT checkout's git HEAD to
stamp a run's provenance is a local, read-only query about the machine otto is
already running on, so declining it would be a false positive of the contract
rather than enforcement of it. It is declared at the one call site that uses
it and extends to nothing else.

## Interrupts: two stages, one exit code

Ctrl-C is decided in one place. Every command body reaches the event loop
through {func}`otto.lifecycle.run_command`, and the first SIGINT or SIGTERM
it sees is **graceful**: the body task is cancelled, a status line goes to
stderr, and the `HostScope` sweep closes remote sessions properly. The
teardown deadline (`OTTO_TEARDOWN_DEADLINE`, 10 seconds by default) starts
counting at that *first signal*, so it is one budget shared by the body's
cancellation unwind and the sweep that follows it — not an allowance granted
to the sweep alone. A body that is slow to unwind spends the sweep's time.
A second signal, or that deadline expiring,
**abandons** it: whatever teardown was still awaiting is dropped, the
registered force-exit hooks run once the loop has closed, and the process
raises `SystemExit(128 + signum)` — 130 for SIGINT, 143 for SIGTERM. Both
stages land on the same exit code; only how much cleanup happened differs, so
a supervisor reads the interrupt identically either way.

Registration is the whole opt-in. A leaf never mentions `otto.lifecycle`: the
invoke wrapper detects the coroutine a plain `async def` leaf returned and
bridges it through `run_command()`, so a third-party command gets host-scope
entry, the two-stage policy, and the bounded teardown for free — the same
policy the first-party commands run under
({doc}`../library/extending-cli`).

A phase that owns its *own* event loop cannot be wrapped that way, and
{func}`otto.lifecycle.sync_phase` is its sibling: the same two stages, but
delivered by a real signal handler that raises `KeyboardInterrupt` into the
phase and a watchdog thread that force-exits `128 + signum` if the phase
does not finish inside the deadline. The watchdog is **armed by the first
signal, not by phase entry** — its pre-arm read blocks with no timeout — so
this is not a wall-clock cap on the phase. An uninterrupted `otto test` runs
as long as its suite takes; the deadline only bounds how long an
*interrupted* one may keep unwinding. It has exactly one caller — the
in-process pytest session behind `otto test` (`otto/suite/run.py`), where
stage one is pytest's own fixture unwind, which releases the suite's host
connections. That *composes* the primitive rather than escaping it: the exit
contract is identical, only the graceful path belongs to pytest instead of
to the scope.

Commands nested inside a phase **defer to it**. `run_command()` installs its
handlers with `add_signal_handler`, and asyncio restores
`default_int_handler`/`SIG_DFL` when it removes them — never the handler it
displaced — so an installing nested command would hand the enclosing phase's
signals to asyncio's defaults on its way out and delete both of its stages.
Since a pytest session inside `otto test` reaches `run_command()` from any
suite that touches a host, that is the ordinary case, not a corner. So a
command that finds a phase in force leaves the dispositions alone and lets
the phase's own handler deliver the interrupt: a `KeyboardInterrupt` raised
into a live nested command unwinds its loop — which cancels the body exactly
as the loop callback would have — and surfaces in the phase's teardown.

The deadline runs from the arming signal and cannot be pushed out. Foreign
signals ride the same wakeup fd while a phase owns it, so the watchdog counts
them as deliveries it must ignore (a resized terminal must not force-exit a
command) without letting them re-time the bound — otherwise anything periodic,
that recurs — a held-down terminal resize, the suite re-arming SIGALRM once
per retried attempt — would postpone an interrupted phase's only guarantee of
termination for as long as it kept arriving.

A nested command has no separate teardown budget of its own: deferring means
its loop callback never arms one, and the phase's deadline bounds the whole
interrupted unwind, sweep included.

`async def` is necessary but not sufficient. The async handler is installed
with `loop.add_signal_handler`, which makes it a loop callback — so a body
that blocks the loop (a bare `subprocess.run`, a `time.sleep`) is exactly as
uninterruptible as a synchronous one, and neither stage can fire until it
returns. Local blocking work belongs in {func}`asyncio.to_thread`.

## OttoContext: the per-invocation runtime

{class}`~otto.context.OttoContext` is a plain dataclass holding exactly what
one invocation needs: the active `lab`, the `dry_run` and
`log_command_output` flags, the invocation's `output_dir`, and a
{class}`~otto.context.HostScope`. Its methods are the canonical host
accessors:

- {meth}`~otto.context.OttoContext.get_host` — look up one host by id, apply
  per-call option overrides, register it with the scope.
- {meth}`~otto.context.OttoContext.all_hosts` — iterate the fleet. The
  built-in `local` host and Docker container hosts are excluded unless opted
  in (`include_local=True` / `include_containers=True`): deploy, monitor, and
  coverage sweeps must never silently operate on the runner itself.
- {meth}`~otto.context.OttoContext.do_for_all_hosts` /
  {meth}`~otto.context.OttoContext.run_on_all_hosts` — fan a call out across
  the fleet (concurrently by default) with per-host error isolation: the
  returned dict maps host id to either the result or the exception that host
  raised, so one dead host cannot abort the sweep.

The context is installed in a
{class}`~contextvars.ContextVar` via `set_context()` and read back with
{func}`~otto.context.get_context` (raising) or
{func}`~otto.context.try_get_context` (returning `None`). The context-variable
is *plumbing*, not a global: explicit `ctx` passing is first-class, and the
zero-argument convenience accessors
({func}`~otto.config.fleet.all_hosts`, `get_host`,
`run_on_all_hosts`, …) simply delegate to the active context's
method of the same name. Anything that wants its dependency visible takes a
`ctx` parameter — CLI commands can declare `ctx: OttoContext` and have it
injected.

## HostScope: deterministic teardown, no `__del__`

Hosts hold real resources — SSH connections, telnet consoles, docker exec
channels. otto deliberately has **no** `__del__`-based cleanup: garbage
collection is non-deterministic, and relying on it caused resource churn.
Instead every host handed out by a context is registered (deduplicated by
identity) with the context's {class}`~otto.context.HostScope`, and the scope
closes anything still connected when the invocation ends.

That yields three equally valid usage modes, mirroring file descriptors:

```python
# 1. tight, early scoping
async with ctx.get_host("router1") as h:
    await h.run("uptime")

# 2. no ceremony — the scope closes it at command end
h = ctx.get_host("router1")
await h.run("uptime")

# 3. explicit manual control
await h.close()
```

`close()` is idempotent, so an early per-host close and the end-of-scope sweep
never collide.

## Library use: `open_context()`

Scripts and notebooks get the same lifecycle without the CLI:

```python
import otto

async with otto.open_context(lab="my_lab") as ctx:
    result = await ctx.run_on_all_hosts("uname -a")
```

{func}`~otto.context.open_context` runs `bootstrap()` (lazily, idempotently),
loads and merges the requested lab(s), installs the context, and tears
everything down — scope included — on exit. It does *not* run the reservation
gate; that is a CLI-preamble concern, and scripts that want it call
`check_reservations` explicitly. See {doc}`../library/index` for the
user-facing walkthrough.
