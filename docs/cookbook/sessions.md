# Sessions and Periodic Tasks

## Named sessions

By default, `run` uses a single persistent shell session per host.
If you need to run commands concurrently on the *same* host while
preserving shell state in each stream, use
{meth}`~otto.host.host.Host.open_session`:

```{doctest}
>>> host = LocalHost()
>>> async def parallel_sessions():
...     s1 = await host.open_session("worker1")
...     s2 = await host.open_session("worker2")
...     await s1.run("cd /tmp")
...     await s2.run("cd /var")
...     r1 = (await s1.run("pwd")).only
...     r2 = (await s2.run("pwd")).only
...     await s1.close()
...     await s2.close()
...     return r1.value.strip(), r2.value.strip()
>>> run(parallel_sessions())
('/tmp', '/var')
```

Each named session maintains its own working directory, environment
variables, and shell state — independent of the default session and all
other named sessions.

### Async context manager

Sessions support the async context manager protocol for automatic cleanup:

```python
async with (await host.open_session("monitor")) as mon:
    result = await mon.run("stat /tmp/file.bin")
# session is closed automatically
```

### When to use named sessions vs oneshot

| | Named session | `oneshot` |
| --- | --- | --- |
| Shell state | Persistent (per session) | None (fresh process) |
| Setup cost | One connection, reused | New process per call |
| Use case | Multi-step workflows in parallel | One-off independent commands |

## Send and expect

For interactive programs that don't follow a simple command/response
pattern, use {meth}`~otto.host.host.Host.send` and
{meth}`~otto.host.host.Host.expect`:

```python
# Drive an interactive Python REPL
await host.send("python3 -i -c ''\n")
await host.expect(r">>> ", timeout=5.0)
await host.send("print('otto_test')\n")
output = await host.expect(r">>> ", timeout=5.0)
assert "otto_test" in output
await host.send("exit()\n")
```

`send` writes raw text to the session; `expect` blocks until the given
regex pattern appears in the output stream (or the timeout expires).

### AppShell: a higher-level REPL wrapper

Hand-rolling `send`/`expect` works, but for a REPL you drive more than
once — mysql, a vendor CLI, `python3` — wrapping the same loop in an
{class}`~otto.host.app_shell.AppShell` subclass is more ergonomic: declare
the launch command and the prompt once, then call
{meth}`~otto.host.app_shell.AppShell.cmd` for each line and get back a
{class}`~otto.result.ShellResult` instead of a raw string:

```{doctest}
>>> import re
>>> from otto import AppShell
>>> class PyRepl(AppShell):
...     """The stock CPython REPL as an AppShell."""
...     launch = "python3 -u -i"
...     prompt = re.compile(r">>> \Z")
...     quit_cmd = "exit()"
>>> async def repl_demo():
...     appshell_host = LocalHost()
...     try:
...         async with appshell_host.app_shell(PyRepl) as py:
...             result = await py.cmd("print('otto_test')")
...             return result.value.strip()
...     finally:
...         await appshell_host.close()
>>> run(repl_demo())
'otto_test'
```

`cmd()` also takes a `parse=` argument — a {class}`~otto.host.app_shell.Parsed`
subclass, a `list[Parsed]`, or a plain callable — that turns the answer into a
typed object (`result.value` becomes that object instead of a string), with
composite REPL output ("a bordered table *and* its trailing stats line", say)
parsed recursively into nested objects. See `otto.examples.app_shell`
(`src/otto/examples/app_shell.py`) for a full worked example, including
nested parsing.

Two entry points reach the same shell state machine:

- {meth}`~otto.host.host.BaseHost.app_shell` (used above) — provisions a
  dedicated, auto-named session, optionally switches user first (`user=`,
  login-proxying if that cred is proxied — see
  {doc}`Login proxies <../guide/extending-backends>`), launches the shell, and
  tears the session down again on exit.
- {meth}`~otto.host.app_shell.AppShell.attach` — a classmethod that layers onto
  a `HostSession` you already have open (e.g. one from
  {meth}`~otto.host.host.Host.open_session`), leaving it open for reuse once
  the shell exits:

  ```python
  session = await host.open_session("db")
  async with PyRepl.attach(session) as py:
      ...
  # session is still open here — attach() doesn't close what it didn't open
  ```

While a shell is attached (either entry point), the session's sentinel-framed
{meth}`~otto.host.session.HostSession.run` raises
{class}`~otto.host.app_shell.AppShellActiveError` — the command frame must
never be typed into the app itself. Raw `send`/`expect` stay available for
power users (`cmd()` is built on them), so the low-level pattern above is
still there when a REPL's quirks don't fit the `AppShell` mold.

## Periodic tasks

To run a command at a fixed interval, pair `host.run(...)` with
`asyncio.sleep(...)` under `asyncio.gather` so execution and waiting overlap:

```python
import asyncio

async def poll_status(host):
    """Run 'uptime' every 10 seconds, gather with sleep."""
    results = await asyncio.gather(
        host.run(["uptime"]),
        asyncio.sleep(10.0),
        return_exceptions=True,
    )
    run_result = results[0]
    return run_result.only.value.strip()
```

The key insight is that `asyncio.gather` runs the command and the sleep
*concurrently* — so if the command takes 3 seconds and the interval is 10
seconds, the total wall time is 10 seconds (not 13).

### Multi-host polling

The monitor collector extends this pattern to poll multiple hosts
simultaneously:

```python
import asyncio

async def collect_from_all(hosts, interval_secs):
    """Collect metrics from all hosts, then sleep for the remainder."""
    results = await asyncio.gather(
        asyncio.sleep(interval_secs),
        *(host.run(["cat /proc/stat", "free -b"]) for host in hosts),
        return_exceptions=True,
    )
    # results[0] is the sleep (None)
    # results[1:] are the Results objects (one per host)
    return results[1:]
```

This ensures all hosts are polled at the same instant and the next
collection starts exactly `interval_secs` after the previous one.
