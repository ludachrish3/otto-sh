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
...     return r1.output.strip(), r2.output.strip()
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

## Periodic tasks

Otto provides {class}`~otto.host.repeat.RepeatRunner` for running commands
at a fixed interval.  Internally, it uses `asyncio.gather` with
`asyncio.sleep` to overlap execution and waiting:

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
    return run_result.statuses[0].output.strip()
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
    # results[1:] are the RunResult objects (one per host)
    return results[1:]
```

This ensures all hosts are polled at the same instant and the next
collection starts exactly `interval_secs` after the previous one.
