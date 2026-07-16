# The command lifecycle

Every `otto` invocation walks the same path before a first-party command
takes over: compose the process, dispatch one command, prepare the
invocation, run it, tear it down deterministically. This page covers that
shared path; the pages below cover what each command does once it has
control.

```{graphviz}
digraph lifecycle {
    rankdir=TB;
    node [shape=box];

    entry [label="entry() — console script"];
    completion [label="completion fast path\ncache hit → zero user code", style=dashed];
    discovery [label="bootstrap phase 1: discovery\nOTTO_* env + settings.toml\n(no user code runs)"];
    registration [label="bootstrap phase 2: registration\ninit modules + test files\n(per-file failures contained)"];
    dispatch [label="dispatch\nresolve only the target command;\nevery other command stays a help stub"];
    preamble [label="invoke preamble\nload + merge labs → OttoContext →\noutput dir + log sinks → reservation gate\n(lab_free commands skip lab and gate)"];
    body [label="command body\n(command-specific — pages below)"];
    teardown [label="teardown\nHostScope closes remaining hosts;\nexit code derived from the Result"];

    entry -> completion [label=" completion request"];
    entry -> discovery;
    discovery -> registration;
    registration -> dispatch;
    dispatch -> preamble;
    preamble -> body;
    body -> teardown;
}
```

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
| {doc}`monitor <subsystems/monitoring>` | yes | yes | self-gated per branch: `--live` collection gates, reviewing a saved source doesn't |
| {doc}`docker <subsystems/docker-hosts>` | yes | yes | no — containers ride the parent's reservation |
| {doc}`cov <subsystems/coverage>` | yes | no — reads existing run dirs | no |
| {doc}`reservation <subsystems/reservations>` | no (`lab_free`) — `check` loads lab data itself | no | no — it *is* the gate, made inspectable |
| {doc}`schema <subsystems/data-boundary>` | no (`lab_free`) | no | no |
| {doc}`init <subsystems/bootstrap>` | no (`lab_free`) | no | no |

`--lab` itself tab-completes — the lab names are tags on hosts in the
`lab.json` files, read data-only (no host construction, no user code), and
the option is `+`-separated so each segment completes in turn:

```{raw} html
:file: ../_static/generated/termynal/complete-lab-names.html
```

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
async with ctx.get_host("router1") as h:   # 1. tight, early scoping
    await h.run("uptime")

h = ctx.get_host("router1")                # 2. no ceremony — the scope
await h.run("uptime")                      #    closes it at command end

await h.close()                            # 3. explicit manual control
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
