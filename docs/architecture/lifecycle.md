# Composition root and context lifecycle

Two modules own "how an otto process comes to life and how it dies":
{mod}`otto.bootstrap` composes the process (what is registered), and
{mod}`otto.context` composes the invocation (which lab, which flags, which
hosts are open). Keeping them separate is deliberate — registration is
process-wide and idempotent, while a context is per-invocation and disposable.

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
`--list-*` flags, and shell completion never open `hosts.json`, and a missing
or malformed lab file only matters once a command that needs the lab runs.

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
({func}`~otto.configmodule.configmodule.all_hosts`, `get_host`,
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
`check_reservations` explicitly. See {doc}`../guide/library-usage` for the
user-facing walkthrough.

## The CLI preamble

For CLI commands, the same steps run in the invoke preamble
(`otto/cli/invoke.py`) just before the leaf callback: load and merge labs
(`--lab` may repeat), build and install the `OttoContext`, create the
per-command output directory and wire the log sinks
({doc}`results-and-logging`), and run the reservation gate — skipped for
commands registered with `gate=False` or `lab_free=True`
({doc}`registries`), or when the user passes `--skip-reservation-check`.
