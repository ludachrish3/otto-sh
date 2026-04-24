# Async Patterns

Otto uses `asyncio` throughout for managing concurrent operations on remote
hosts.  This page demonstrates the most common patterns.

## Running a single command

The simplest pattern: run one command and inspect the result.

```{doctest}
>>> host = LocalHost()
>>> result = run(host.run("echo hello")).only
>>> result.status
<Status.Success: 0>
>>> result.output.strip()
'hello'
```

{meth}`~otto.host.host.Host.run` uses a persistent shell session, so
state like the working directory persists between calls:

```{doctest}
>>> host = LocalHost()
>>> run(host.run("cd /tmp"))
RunResult(status=<Status.Success: 0>, statuses=[CommandStatus(command='cd /tmp', ...)])
>>> result = run(host.run("pwd")).only
>>> result.output.strip()
'/tmp'
```

## Running multiple commands sequentially

Pass a list of commands to {meth}`~otto.host.host.Host.run` to run them
in order and get back a {class}`~otto.host.host.RunResult` with an
aggregate status plus individual per-command results:

```{doctest}
>>> host = LocalHost()
>>> result = run(host.run(["echo first", "echo second"]))
>>> result.status
<Status.Success: 0>
>>> [cs.output.strip() for cs in result.statuses]
['first', 'second']
```

## Running commands concurrently with asyncio.gather

Use {meth}`~otto.host.host.Host.oneshot` for concurrent-safe execution.
Unlike `run`, each `oneshot` call opens an independent process and does
not share state:

```{doctest}
>>> import asyncio
>>> host = LocalHost()
>>> async def concurrent_oneshot():
...     results = await asyncio.gather(
...         host.oneshot("echo one"),
...         host.oneshot("echo two"),
...         host.oneshot("echo three"),
...     )
...     return [r.output.strip() for r in results]
>>> run(concurrent_oneshot())
['one', 'two', 'three']
```

### When to use run vs oneshot

| | `run` | `oneshot` |
| --- | --- | --- |
| Session | Persistent (state carries over) | Fresh process per call |
| Concurrent-safe | No (shares one shell) | Yes |
| Use case | Sequential steps that depend on shell state | Independent commands in parallel |

## Multi-host concurrent operations

A common real-world pattern is running the same command on every host in the
lab concurrently.  Otto ships two helpers for this so you don't have to
hand-roll `asyncio.gather` every time:

- {func}`~otto.configmodule.configmodule.run_on_all_hosts` — the simplest
  case: run one or more shell commands on every matching host.
- {func}`~otto.configmodule.configmodule.do_for_all_hosts` — the general
  form: call any async `RemoteHost` method (including user-defined
  coroutines that take a host as their first argument).

Both return a `dict[host_id, result | BaseException]`, with
`return_exceptions=True` baked in so one failing host cannot cancel the
others.  Both accept a compiled regex `pattern=` filter that is matched
against each host's `id`, so you can target a subset of the lab without
pre-filtering yourself.

### `run_on_all_hosts` — one or more commands, everywhere

```python
import re
from otto.configmodule.configmodule import run_on_all_hosts

async def check_all_hosts():
    """Run 'uname -a' on every host concurrently."""
    results = await run_on_all_hosts("uname -a")
    for host_id, result in results.items():
        if isinstance(result, BaseException):
            print(f"{host_id}: ERROR - {result}")
        else:
            print(f"{host_id}: {result.statuses[0].output.strip()}")

async def check_routers_only():
    """Target just hosts whose id matches /router/."""
    results = await run_on_all_hosts(
        ["uname -a", "uptime"],
        pattern=re.compile(r"router"),
    )
    ...
```

Pass `concurrent=False` to execute serially instead — useful when hosts
share a resource you don't want hammered in parallel.

### `do_for_all_hosts` — any async callable

`do_for_all_hosts` takes an unbound async method (or any async callable
whose first argument is a host) and dispatches it across the lab.  This
is the right tool when the operation you need isn't just a shell command
— for example, a file transfer, a multi-step workflow, or a helper of
your own.

```python
from pathlib import Path
from otto.configmodule.configmodule import do_for_all_hosts
from otto.host.remoteHost import RemoteHost

async def deploy_firmware():
    """Push a firmware file to all hosts concurrently."""
    results = await do_for_all_hosts(
        RemoteHost.put,
        src_files=[Path("firmware.bin")],
        dest_dir=Path("/tmp"),
    )
    for host_id, result in results.items():
        match result:
            case BaseException():
                print(f"{host_id}: transfer failed - {result}")
            case (status, _) if status.is_ok:
                print(f"{host_id}: transfer succeeded")
            case (status, error_str):
                print(f"{host_id}: {status} - {error_str}")
```

You can also pass a user-defined coroutine that takes a host as its first
argument — handy for multi-step workflows:

```python
async def install_and_verify(host: RemoteHost, package: str) -> str:
    await host.oneshot(f"sudo apt-get install -y {package}")
    result = await host.oneshot(f"dpkg -s {package}")
    return result.output

results = await do_for_all_hosts(install_and_verify, "nginx")
```

### When to fall back to raw `asyncio.gather`

The helpers cover the overwhelming majority of cases.  Drop down to
`asyncio.gather` directly only when you need something they don't
express — e.g. dispatching *different* commands to different hosts, or
coordinating cross-host synchronization inside the same task graph.

```python
import asyncio
from otto.configmodule.configmodule import all_hosts

async def mixed_workload():
    hosts = list(all_hosts())
    # Each host runs a different command
    cmds = {"switch-a": "show vlan", "switch-b": "show mac"}
    results = await asyncio.gather(
        *(h.oneshot(cmds[h.id]) for h in hosts if h.id in cmds),
        return_exceptions=True,
    )
```

## Handling CommandStatus results

{class}`~otto.utils.CommandStatus` is a named tuple with four fields:

```{doctest}
>>> result = CommandStatus(command="echo hi", output="hi", status=Status.Success, retcode=0)
>>> result.command
'echo hi'
>>> result.status.is_ok
True
>>> result.retcode
0
```

Check the {attr}`~otto.utils.Status.is_ok` property to determine if a
command succeeded:

```{doctest}
>>> Status.Success.is_ok
True
>>> Status.Failed.is_ok
False
>>> Status.Error.is_ok
False
```
