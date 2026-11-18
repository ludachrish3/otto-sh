# Sessions and Periodic Tasks

## Named sessions

By default, `run` uses a single persistent shell session per host.
If you need to run commands concurrently on the *same* host while
preserving shell state in each stream, use
{meth}`~otto.host.host.Host.open_session`:

```{doctest}
>>> from otto.host import LocalHost
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
...     return r1.value, r2.value
>>> run(parallel_sessions())
('/tmp', '/var')
```

Each named session maintains its own working directory, environment
variables, and shell state — independent of the default session and all
other named sessions.

### Async context manager

Sessions support the async context manager protocol for automatic cleanup:

```python
async with await host.open_session("monitor") as mon:
    result = await mon.run("stat /tmp/file.bin")
# session is closed automatically
```

### Log modes on a named session

A named session logs its `run`, its `send` and what its `expect` reads under
the mode the host's own `run` would: the command's own `log` (a
`ShellCommand`'s) when it sets one, otherwise the call's `log`, composed with
the host's standing `log` as {ref}`log-mode-knob` defines. On a host built
with `log=LogMode.NEVER`, then, none of the three is logged, dry run or live;
on a `QUIET` host they stay off the console. A container host's named session
does not compose the standing mode yet; the container's own `send` and
`expect` (its default session, called on the host) do, as every family's do.

(named-sessions-vs-exec)=
### When to use named sessions vs exec

| | Named session | `exec` |
| --- | --- | --- |
| Shell state | Persistent (per session) | Carried to no later command; on a `console` route, `exec` starts in a clean login shell ({ref}`by route <exec-frame-shell-state>`; routes are listed in {ref}`Command contracts <command-contracts>`) |
| Setup cost | One connection, reused | New process per call |
| Use case | Multi-step workflows in parallel | One-off independent commands |

Some hosts give `exec` no process of its own: over `telnet`, and over `ssh`
when the call needs a shell (`expects=`, `sudo=True`, a user otto cannot log
in as directly), otto types the command into a **pooled shell**, a shell
session it keeps for `exec` beside the one `run` uses. Over `console`, `exec`
is typed into the console's one shell, the one `run` uses. Which route a call
takes is on {doc}`../command-contracts/exec`.

When otto types an `exec` into a shell, it wraps the command in an **exec
frame**: a line that runs the command in a subshell, then restores the
terminal and prints otto's **end record**, a marker line carrying the
command's exit status that tells otto the command is over.

#### What the command reads on stdin

Without `expects=`, an `exec`'s command reads end of file on stdin, so a
command that reads stdin does not wait for input. The ssh exec channel (the
`ssh-channel` route of {doc}`../command-contracts/exec`) and local `exec`
have no stdin to give; the exec frame redirects the command's
stdin away from the terminal.

With `expects=`, the command's stdin is the terminal, since otto types its
answers there.

File descriptor 3 exists only for an `exec` typed into a shell for which otto
answers a sudo or su password. That `exec` starts with fd 3 open on the
session's terminal, the password is read from fd 3, and fd 3 is closed before
the command runs, so the command still reads end of file on stdin. Every other
`exec` opens no fd 3, so its command never sees one. On BusyBox 1.16.1, whose
shell refuses to open fd 3 this way, an `exec` for which otto answers a
password fails ({ref}`Undefined <command-contracts-undefined>` says why).

sudo runs the command in one of two forms, chosen per host
({ref}`sudo-form`): by default one call,
`sudo -S "${SHELL:-/bin/sh}" -c 'exec </dev/null; <cmd>'`, whose `sudo` reads the password on
fd 3's terminal; or, under `validate`, `sudo -S -v` on fd 3 and then
`sudo -n <cmd>`. Either way a failed authentication runs nothing. su runs
`su -c 'exec </dev/null; <cmd>'`. With `expects=`, the shell form's
`"${SHELL:-/bin/sh}" -c` and `su -c` drop the redirect, and the command reads
the terminal.

#### What the exec frame restores

The frame puts the terminal's settings back after the command, so an `stty`
in the command, or a program that left the terminal raw, does not reach the
next command.

On a pooled shell, the settings put back are the shell's **terminal record**:
the terminal settings otto saved for that shell when it opened the session, in
its **handshake** (the first line otto types into a new shell,
{ref}`described here <per-host-session-setup>`). That record is
taken after otto's own `stty`, so a `session_setup` hook's terminal changes
are part of it.

On a console, `exec` shares the shell `run` uses. There it puts back the
terminal settings it found when it started, so a change a `run` made stays.

Where the settings cannot be read (no `stty`, or a pipe rather than a
terminal), nothing is restored and the command runs as before.

The record lives in a shell variable (listed below). If a command removes it,
otto logs one WARNING, restores nothing more in that shell, and does not reuse
a pooled one. After a user switch, or its undo, a shell that has no record
yet saves the terminal's current settings as its record. A shell that already
has one keeps it. How such a command ends, times out or is stopped is on
{ref}`the results page <command-ends-abnormally>`.

(exec-frame-shell-state)=
#### Shell state, by route

Where an `exec` starts, and what it can leave behind:

- **The ssh exec channel and local `exec`** start a process of their own, from
  the login's environment; nothing they change outlives them.
- **A pooled shell** runs nothing but `exec`s, each in a subshell. Each one
  starts from the state the session was set up with (its handshake and any
  `session_setup` hook); no `run` touches it, and nothing an `exec` changes
  reaches the next one.
- **A console's `exec`** runs in a clean login shell that the console's one
  shell (the shell `run` uses) starts for it: `env -i` keeps only `HOME`,
  `TERM`, `USER`, `LOGNAME` and `PATH`, and sets `SHELL` to the program it
  starts (`"${SHELL:-/bin/sh}"`, so the sudo `shell` form inside it names the
  same shell), then `"${SHELL:-/bin/sh}" -l -c`
  reads the login profile, changes to `$HOME` (`/` when it is unset or
  empty, as a login with no home starts) and runs the command. So it
  starts in the login's home directory with the login's environment and
  options, not from what a `run` left, and its own changes reach no later
  `run` or `exec`. What differs from a pooled shell, and what the console
  promises, is on {ref}`the console's promise <command-contracts-narrower>`.
  Anything the profile prints comes before otto's BEGIN marker, which the
  login shell prints, so it is not part of the command's output, and no
  `expects=` pattern is answered to it (on every frame, nothing before BEGIN
  is matched). What a logout file prints is: bash runs `~/.bash_logout` when
  a command ends the login shell with the `exit` builtin, after BEGIN. A login
  shell that never gets as far as the command (no `env`, no such `$SHELL`, a
  `$SHELL` that does not take `-l -c`, a profile that exits or `exec`s)
  prints no BEGIN marker, and the `exec` is a failed result whatever status
  its end record carries ({py:data}`~otto.host.session.NEVER_RAN`); the raw
  text is in the session's DEBUG log. otto has no fallback for a device
  without `env` or with a csh-family `$SHELL`.

A pooled shell's subshell inherits the shell's `set -e`: under it, the
command stops at its first failing statement and reports that status, while
the shell itself does not exit and still prints otto's end record. A
console's login shell starts with the login's own options, so the console
shell's `set -e` is neither inherited nor changed.

When an `exec` times out, has its password refused, or is cancelled, the
frame's own end never ran. otto then types one line of its own once the shell
is back, before the call returns or the cancellation reaches you: it puts the
terminal record back and turns `set -e` on again where the shell had it. A
session that cannot confirm that line is dead
({ref}`closing-a-session`), and a pooled one is discarded instead of handed
out again.

The same holds for a `run` that times out or is cancelled: otto types its
Ctrl-C before the call returns. If your own session has `set -e` on, that
interrupt can end the shell itself. otto then cannot confirm the shell, the
session is dead, and the next use opens a new session or fails as
{ref}`closing-a-session` describes.

To do this the shell keeps a few unexported variables of otto's:

- `__otto_base`: the terminal record of the handshake, kept for the
  session's life;
- `__otto_tty`: the terminal as a console `exec` found it;
- `__otto_e`: whether `set -e` was on;
- `__otto_rc` and `__otto_f`: the status and flag of the end record;
- `__otto_probe`: the handshake's readiness marker.

Each restore unsets `__otto_e` and `__otto_tty` once it has applied them, so
an `exec` whose frame never ran (`timeout=0`, or a frame cut before it saved
anything) restores nothing an earlier `exec` saved. A command that changes
these variables changes what otto restores.

On a host declared as an ash shell (`command_frame: "ash"`, as BusyBox
devices are), a line the terminal would cut is refused before anything is
typed, for `run` and a shell-routed `exec` alike. To send such a command,
split it into shorter lines, put it in a script file and run that, or, over
`ssh`, use an `exec` that takes its own channel, which types nothing. The
limits are a
{ref}`declared exception <exception-how-long-one-line-of-a-command-can-be-on-the-pooled-shell-and-default-session-routes>`.

#### Which shells this was measured on

Measured on the dev machine with bash 5.2, dash 0.5.12, BusyBox 1.36 `sh`,
ksh93, zsh 5.9 and mksh, on 2026-11-13 and 2026-11-14. The lab's devices run
bash and BusyBox ash only; zsh and mksh are not lab shells, and zsh is not
claimed at all.

The subshell frame, its status and its `set -e` rule:

- measured on bash, dash, BusyBox ash, ksh93, zsh and mksh.

Putting the terminal back, including after a timeout:

- measured on bash, dash, BusyBox ash and ksh93; on mksh by otto's unit
  tests on a local terminal (2026-11-14);
- cannot be told apart on zsh, whose line editor resets the terminal at each
  prompt.

A sudo or su password answered with the command still reading end of file:

- measured on bash, dash, BusyBox ash, ksh93 and mksh, against a stand-in
  sudo and su on local shells;
- known failing on zsh: the command reads the password's line ending instead
  of end of file;
- password sudo over `ssh` measured on the lab's test1 (sudo 1.9.15p5,
  default timestamp) and test3 (`timestamp_timeout=0`) by otto's lab
  integration tests, on 2026-11-14;
- password sudo on a `telnet` or `console` route, and password su on any
  route, checked only against the stand-in
  ({ref}`Undefined <command-contracts-undefined>` says why).

Whether one `exec` changes the state the next one sees:

- not measured on a Zephyr board (the `exec-isolated` promise of
  {doc}`../command-contracts/exec`).

To share one host connection across many tests, see {doc}`../setup/host-scopes`.

(closing-a-session)=
### Closing a session and a given-up session

A session whose timed-out command could not be stopped is given up: it is
marked unusable, and its working directory and variables are lost. A given-up
session that is the host's default session is closed by the next operation
that uses that session; any other is closed when it or its host is closed.
On the local machine, which commands can leave a session given up is a
{ref}`known gap of run <command-contract-run-known-gaps>`.

Closing a local shell session sends TERM to its process group, and KILL to the
group only if the shell outlives TERM. A process that ignores TERM survives
when the shell exits on TERM; a process that left the group is never
signalled; and if the shell had already exited before the close, nothing is
signalled.

The local shell runs in a session of its own, with no controlling terminal, so
a Ctrl-C typed at otto's terminal does not reach the commands running in it.
Cancelling a `run` stops its command the way a timeout does, before the
cancellation reaches the caller: otto sends INT to the shell's own child
processes and checks that the shell answers again. A process those children
started is not signalled; it is stopped when the session or its host is
closed, which the CLI does as it exits.

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
{class}`~otto.result.ShellResult` instead of a raw string. Its `value` is
everything the app printed between your line and its next prompt, so a REPL's
own trailing newline stays in it:

```{doctest}
>>> import re
>>> from otto import AppShell
>>> from otto.host import LocalHost
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
...             return result.value
...     finally:
...         await appshell_host.close()
>>> run(repl_demo())
'otto_test\n'
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
  {doc}`Login proxies <extending-backends>`), launches the shell, and
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

(session-transport-hooks)=
## Implementing a session transport

A new way to reach a shell (a transport other than ssh, telnet or a local
process) is a {class}`~otto.host.session.ShellSession` subclass. The session
owns the framing, the handshake and recovery; the subclass implements the
transport's hooks, and the class cannot be constructed until every one is
implemented:

| Hook | What it does |
|------|--------------|
| `open_transport()` | Bring the transport up; the session calls it before its handshake. |
| `write_transport(data=...)` | Write raw text; a `SecretStr` is revealed inside the transport call itself. |
| `read_transport_until(pattern=...)` | Read until `pattern` matches; return the data, match included. |
| `_take_unread()` | Return what has arrived and no read has returned yet, and consume it. |
| `close()` | Close the transport. |

`_take_unread()` is called once a command's budget has run out, before
anything is written to interrupt it, so the timed-out result carries what the
command had printed ({ref}`what a timed-out value holds <timed-out-command-result>`). It must:

- **never wait**: return only what has already arrived, `""` when nothing has;
- **consume**: a piece it returns is never returned again, by it or by
  `read_transport_until`;
- **include held state**: text the transport keeps between reads (data read
  past the last match, a buffered line, a partly decoded character once it is
  whole) is part of what it returns.

A wrong implementation shows up in the timed-out result and in the next
command: one that waits holds the timeout past its budget; one that returns
nothing drops the command's last lines from the result; one that does not
consume hands the same text to the next read, where it is taken for that
command's output or makes it miss its BEGIN marker.

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
    return run_result.only.value
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
