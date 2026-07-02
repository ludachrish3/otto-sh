# Using otto as a library

otto is not limited to the `otto` CLI. You can use it directly in your own
async Python scripts — for example, one-off automation, CI tooling, or
integration scripts that operate on lab hosts without needing test suites or
instructions.

## Imports are side-effect-free; `open_context()` runs the composition root

`import otto` and `import otto.configmodule` do no I/O and run no project code.
`import otto` implements PEP 562 lazy exports (each public name resolves its
source module only on first attribute access), so a bare import stays cheap even
in a process that never touches a lab. `import otto.configmodule` is
side-effect-free (no repo discovery, no user code) but eagerly imports its
submodules. Nothing under `.otto/settings.toml` `init` is imported just because
`otto` is on `sys.path` — that happens in {func}`otto.bootstrap.bootstrap`.

The composition root — repo discovery plus importing every configured `init`
module and test file — is {func}`otto.bootstrap.bootstrap`, and it is
idempotent (repeated calls return the same cached result). `open_context()`
calls it for you before loading the lab, so any `@instruction`,
`@register_suite()`, `@cli_command()`, or `register_*_backend()` call in your
project's `init` modules has already run by the time the `async with` block
starts:

```python
async with otto.open_context(lab="mylab") as ctx:
    ...  # your project's registered components are all live here
```

If you're wiring up a custom embedding that bypasses `open_context()` — for
example, driving `OttoContext`/`set_context()` manually as shown below — call
`otto.bootstrap.bootstrap()` yourself first if you need those registrations
available. Skipping it isn't an error; it just means your script only sees
otto's own built-ins, not anything your project registers in `init`.

## Recommended: `open_context()`

`open_context()` is the single entry point for library use. It loads a lab,
installs the active context, enters the host lifecycle scope, yields the
context, and tears everything down on exit — even if your code raises.

```python
import asyncio
import otto

async def main():
    async with otto.open_context(lab="mylab", search_paths=[...]) as ctx:
        results = await ctx.run_on_all_hosts("uname -a")
        for host_id, result in results.items():
            print(host_id, result)
    # every host opened in the block is closed here, deterministically

asyncio.run(main())
```

Inside the block the context is the active one, so the zero-argument accessors
work without passing `ctx` around:

```python
async with otto.open_context(lab="mylab") as ctx:
    # explicit path
    for host in ctx.all_hosts():
        await host.run("uptime")

    # or the zero-argument bare accessors — same result
    for host in otto.all_hosts():
        await host.run("uptime")
```

`open_context` accepts:

| Parameter            | Type                        | Default | Description                               |
|----------------------|-----------------------------|---------|-------------------------------------------|
| `lab`                | `Lab \| str \| list[str]`   | —       | A `Lab` object, or lab name(s) to load    |
| `dry_run`            | `bool`                      | `False` | Log commands without executing them       |
| `log_command_output` | `bool`                      | `True`  | Stream command output to the otto logger  |
| `search_paths`       | `list[Path] \| None`        | `None`  | Paths to search for lab definitions       |

## Bring-your-own-CLI: lower-level primitives

otto's own CLI uses these three steps internally — `open_context` is just them
packaged across the callback/subcommand boundary:

1. Build an `OttoContext` with the chosen lab and runtime flags.
2. Install it as the active context with `set_context()`, which returns a reset
   token.
3. Enter `ctx.scope` as an async context manager; on exit it closes any
   still-connected hosts, then `reset_context(token)` restores the prior state.

```python
from otto.context import OttoContext, reset_context, set_context
from otto.configmodule import load_lab

lab = load_lab("mylab", search_paths=[...])
ctx = OttoContext(lab=lab, dry_run=False)
token = set_context(ctx)
try:
    async with ctx.scope:
        # your work here
        ...
finally:
    reset_context(token)
```

This is exactly what `open_context` does under the hood. Use this form when
you need fine-grained control — for instance, when a framework drives the
event loop and you cannot use `async with` at the top level.

## Host lifetimes

There are three patterns for managing individual host connections inside an
`open_context` block. All three are safe — the scope provides the backstop.

**(a) Tight scoping with `async with`:**

```python
async with otto.open_context(lab="mylab") as ctx:
    async with ctx.get_host("router1") as host:
        await host.run("show version")
    # host.close() was called here; connection is gone
```

**(b) Pass the host around; let the scope close it:**

```python
async with otto.open_context(lab="mylab") as ctx:
    host = ctx.get_host("router1")
    await configure(host)      # pass it wherever you like
# scope.close() sweeps host when the block exits
```

**(c) Explicit `await host.close()`:**

```python
async with otto.open_context(lab="mylab") as ctx:
    host = ctx.get_host("router1")
    await host.run("reboot")
    await host.close()         # early close — idempotent; scope sweep is a no-op
```

`close()` is idempotent: calling it multiple times is safe.

## FD-model caveat

A host you construct **directly** (e.g. `UnixHost(...)`) outside any context
has no scope backstop — it is yours to close, exactly like an explicitly-opened
file descriptor. Use `async with`, `await h.close()`, or register it manually
with `ctx.scope.register(h)` inside an active context.

Reservation checks are a CLI concern — `open_context` does not gate on them.
If your script needs to verify reservations before running, call
`otto.reservations.check_reservations(...)` explicitly before entering the
block.

## In-memory labs (no lab file)

You do not need a `hosts.json` on disk. Build a `Lab` from host dicts, install
it as the active context, and the zero-argument selectors (`all_hosts`,
`get_host`) operate on it directly — useful for tests and ad-hoc scripts.
Selection touches no network, so this runs as-is:

```{doctest}
>>> import re
>>> from otto.storage.factory import create_host_from_dict
>>> from otto.configmodule.lab import Lab
>>> from otto.context import OttoContext, set_context, reset_context
>>> from otto.configmodule import all_hosts, get_host
>>> hosts = [create_host_from_dict(spec) for spec in [
...     {"ip": "10.0.0.11", "element": "carrot", "creds": {"admin": "x"}, "labs": ["veg"]},
...     {"ip": "10.0.0.12", "element": "tomato", "creds": {"admin": "x"}, "labs": ["veg"]},
... ]]
>>> lab = Lab(name="veg", hosts={h.id: h for h in hosts})
>>> token = set_context(OttoContext(lab=lab))
>>> [h.element for h in all_hosts(re.compile("tomato"))]
['tomato']
>>> get_host("carrot").element
'carrot'
>>> reset_context(token)
```

The trailing `reset_context` restores the prior active context — always pair it
with `set_context` (or use `otto.open_context`, which does both for you).
