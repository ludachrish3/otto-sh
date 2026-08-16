# Dry runs (`--dry-run`)

**A dry run never runs a command on any device.** By default it validates and
stops at the CLI seam; a command may opt in to a deeper, configuration-only
preview; and `--probe` may permit a *connection* — never a command — so you can
see whether the hosts would answer.

That sentence is the whole contract. It is written so plainly because of what
the alternative looks like. When logic branches on a fact about a device there
are exactly three things a dry run can do: **fabricate the fact**, **decline
loudly**, or **never run the logic**. "Continue gracefully" is not a fourth
option — it is the first one wearing a kinder name. otto used to take it, and a
dry run would report every link clean, print the body of a file it had not
written, and accuse a host it had never contacted of missing `socat`.
Everything below is the consequence of removing that option.

## The default: validate, print, stop

Under `--dry-run` (`-n`), otto's dispatch layer — not the command — does this,
and exits **0 before the command body runs**:

- arguments parse and coerce, so a typo'd `--mode 789` still fails here
- the lab loads, and every host, link or tunnel the command names resolves
  against it
- the command's module imports; for `otto test`, the suite imports and its
  steps bind
- it prints what would run: the command, its target, and the arguments you gave

```console
$ otto --lab my_lab -n host dut1 run "systemctl restart nginx"
[DRY RUN] Commands and file transfers will be skipped. No device will be contacted.
dry run: no command body was run and no device was contacted
  would run: otto host dut1 run 'systemctl restart nginx'
  lab: my_lab (3 hosts); references resolve: host 'dut1'
```

No body executes, so nothing a body might do can happen. **A command author who
does nothing at all gets this, and cannot get it wrong** — which is the point.
The alternative, where every author has to keep dry runs in mind while writing
run-parse-branch logic, produces a feature that is subtly broken in a different
way in each command, and a consistently broken complex feature is worse than a
reliable simple one.

Resolution really happens, and its failures are still failures — the block is
printed *after* the references resolve, so a dry run never reports that a
command "would run" against a host that does not exist:

```console
$ otto --lab my_lab -n host nosuchbox run "uptime"
No host with ID 'nosuchbox'.
Available hosts:
  - router1
  - dut1
  - local
```

`otto test -n` is the same rule rather than a special case — the suite really
imports and its tests really bind, and then nothing runs:

```console
$ otto --lab my_lab -n test TestExample
dry run: no command body was run and no device was contacted
  would run: otto test TestExample
  suite: TestExample imported and bound; 1 test(s), no test body will run
    - test_logs_message
```

Making step bodies previewable is not a missing feature — steps not running
*is* the feature.

## The stop is uniform, and that will surprise you once

The seam applies to **lab-free** commands too. These print the block and exit 0
**without doing their work**:

```console
$ otto -n schema export
dry run: no command body was run and no device was contacted
  would run: otto schema export
  lab: not loaded (lab-free command)

$ otto -n init
dry run: no command body was run and no device was contacted
  would run: otto init
  lab: not loaded (lab-free command)

$ otto -n reservation whoami
dry run: no command body was run and no device was contacted
  would run: otto reservation whoami
  lab: not loaded (lab-free command)

$ otto --lab my_lab -n monitor --live
dry run: no command body was run and no device was contacted
  would run: otto monitor --live
  lab: not loaded (lab-free command)
```

So `otto schema export -n` writes no schemas and `otto init -n` scaffolds
nothing. This is deliberate, not an oversight.

`lab_free` means **"this command drives its own lifecycle"**, not "this command
touches no device". `otto monitor --live` is registered lab-free and collects
metrics from every host in the lab — so a carve-out letting lab-free commands
run their bodies under `-n` would exempt precisely the command that can still
reach a device while you believed nothing would. One rule with no exceptions is
also the only version a third-party author can hold in their head.

If a lab-free command of your own should genuinely run under `-n`, that is what
the opt-in below is for.

## Opting in: `dry_run_preview=True`

A command whose body can say something useful without touching a device
declares it **at registration**:

```python
register_cli_command(
    "link",
    "otto.cli.link:link_app",
    help="Inspect and impair the lab's static links.",
    output_dir=False,
    dry_run_preview=True,
)
```

```{important}
`dry_run_preview` is a **registration keyword, like `output_dir`** — not a CLI
option. Nobody types it, it appears in no `--help`, and an operator cannot turn
it on for a command that did not ask for it.
```

Two seams carry it, matching the two ways otto registers things (see
{doc}`extending-cli`):

- `register_cli_command(...)` and the `@cli_command(...)` decorator, for a
  whole command group
- `@cli_exposed(dry_run_preview=True)`, for one host verb

A **leaf may opt in without its group**. `otto host` as a group keeps the safe
default, while `put`, `get` and `write-file` opt in individually — those three
have a real plan to show and `run` does not. For `run`, the echoed command line
*is* the whole announcement, so running the body would add nothing and widen
the surface for no gain.

What the opt-in buys, from `otto link impair` (abridged):

```console
$ otto --lab my_lab -n link impair core --delay 50ms --loss 2
dry run core: no device was contacted — nothing was read and nothing was changed
  would: a->b on router1/eth1: tc qdisc replace dev eth1 root netem delay 50ms loss 2%
  would: b->a on dut1/eth1: tc qdisc replace dev eth1 root netem delay 50ms loss 2%
  not checked: what is CURRENTLY applied to the netdev. A real run merges the given
    parameters over it per-param, so any command line above is the one a CLEAN netdev
    would get and nothing else …
  not checked: the two self-lockout refusals … BOTH refuse only on a positive match
    against the host's live `ip -o addr show`, which was not run. A dry run therefore
    CANNOT tell you this impairment would be allowed, let alone that it is safe
  not checked: the netdev's current SHAPE …
  not checked: live expire timers … and the post-apply verify …
```

Every preview has the same three parts, and the third is what makes the other
two safe to read:

1. **The plan** — concrete actions derived from configuration alone: the host,
   the netdev, the exact command line, wherever configuration can produce one.
2. **The pure refusals** — every check needing no device still fires, and fires
   early. A bash-less `--expire`, a local-link impair, an unparseable `--mode`:
   all rejected here, some of them *earlier* than a real run rejects them.
3. **The honest gaps** — everything that could not be checked without contact,
   named individually. `not checked:` is not boilerplate. On `link impair` it
   is the difference between "here is the command" and "here is the command,
   and otto cannot tell you it would be allowed to run it".

A preview names actions and targets; it never carries file bodies or secrets:

```console
$ otto --lab my_lab -n host dut1 write-file /etc/motd "hello there"
[DRY RUN] WRITE: 11 bytes -> /etc/motd
@dut1   | [DRY RUN] WRITE: 11 bytes -> /etc/motd
```

Eleven bytes, named destination, no eleven bytes. **Suppress the payload, never
the announcement** — a dry run with no output is a bug, because a preview with
no product is useless and one with an invented product is dangerous. (The
announcement appears twice on purpose: once from the library layer, which is
what a script or suite sees, and once from the renderer, which is what
guarantees a console product even for a host whose standing log mode is quiet.)

Verbs with nothing to preview keep the seam default and print the ordinary
block — `ls` among them, because "here is a directory listing" is precisely
what it cannot honestly produce:

```console
$ otto --lab my_lab -n host dut1 ls /var/log
dry run: no command body was run and no device was contacted
  would run: otto host dut1 ls /var/log
  lab: my_lab (3 hosts); references resolve: host 'dut1'
```

See {doc}`network/link` and {doc}`network/tunnel` for what each of those
previews can and cannot tell you.

## Reachability: `--dry-run --probe`

`--probe` is opt-in on top of a dry run. It lets otto **open a connection** to
each host the command names and report whether it answered.

```{important}
**`--probe` permits a connection, never a command.** A connection attempt
produces no command result, so it feeds no `if result.is_ok:` and no parser —
the fabrication hazard is untouched. Reachability is information, not a gate:
an unreachable host is reported and the dry run still exits 0.
```

```console
$ otto --lab my_lab -n --probe host router1 run "make install"
[DRY RUN] Commands and file transfers will be skipped. --probe will open a
connection to each named host, and run no command.
probe: a connection only -- no command was run
  router1: unreachable
@router1   | [DRY RUN] Connection FAILED: [Errno 111] Connect call failed ('127.0.0.1', 23) — a real connection; no command was run
dry run: no command body was run; --probe opened a connection only, and ran no command
  would run: otto host router1 run 'make install'
  lab: my_lab (3 hosts); references resolve: host 'router1'
```

Note the headline: it changes the moment a socket is opened, because the
default one ends "and no device was contacted" and that would then be false.

**`--probe` requires `--dry-run`.** On its own it is a usage error (exit 2) —
the only thing that makes dialing safe here is that no command can follow it:

```console
$ otto --lab my_lab --probe link list
╭─ Error ──────────────────────────────────────────────────────────────────────╮
│ Invalid value for --probe: --probe requires --dry-run/-n: it opens a         │
│ connection to each host the command names, which is only safe because a      │
│ dry run runs no command afterwards.                                          │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### It opens *and authenticates*

The probe opens the connections the invocation would have used, and opening
includes logging in:

- the terminal channel (ssh or telnet), and
- the FTP control channel as well, when the host's transfer backend is `ftp` —
  so one probe of an FTP-configured host opens **two** sockets, not one.

```{warning}
**For telnet and FTP, authenticating puts the login credentials on the wire.**
```

That is deliberate. The probe answers "would this run's connect phase succeed?",
and anything narrower — a bare TCP connect, or the terminal channel alone —
would report a reachability the real run is not going to get. A host that
accepts TCP and then refuses the login is not usable, and calling it
`reachable` would be this contract's own defect in a smaller font. It is also
exactly why the flag is opt-in rather than part of `--dry-run`.

`--term`, `--transfer` and `--hop` are honoured, so the probe dials the
transport the command would have dialed rather than the host's configured
default.

### Three states, not two

| state | meaning |
| ----- | ------- |
| `reachable` | a connection opened (and authenticated); the row carries `connect <N> ms` |
| `unreachable` | a connection was attempted and did not open — refused, timed out, or refused the login |
| `not probed` | no reachability question could be asked at all |

`not probed` is a separate state on purpose. A Docker container host is reached
through its parent's shell and has no transport of its own, so otto never asks;
reporting it `unreachable` would be a measurement nobody took. "We could not
ask" and "we asked and it said no" are different facts, and only one of them is
about the host.

The built-in `local` host is reachable without a socket — otto is already
running there — and says so:

```console
$ otto --lab my_lab -n --probe host local run "uptime"
probe: a connection only -- no command was run
  local: reachable -- no transport to open
dry run: no command body was run and no device was contacted
  would run: otto host local run uptime
  lab: my_lab (3 hosts); references resolve: host 'local'
```

The headline stayed at "no device was contacted", because none was: the block
counts **sockets, not rows**.

### The limit

**No link or tunnel command lends the probe a reference resolver today.** Only
`otto host` does. So `--probe` on a link or tunnel command dials nothing, and
says so rather than letting silence imply the hosts were found healthy:

```console
$ otto --lab my_lab -n --probe link impair core --delay 50ms
[DRY RUN] Commands and file transfers will be skipped. --probe will open a
connection to each named host, and run no command.
probe: this command names no host to dial
dry run core: no device was contacted — nothing was read and nothing was changed
  would: a->b on router1/eth1: tc qdisc replace dev eth1 root netem delay 50ms
  …
```

## The library contract, for suite and script authors

Everything above is about the CLI. If you write suites, instructions or
scripts, you call `host.run(...)` directly and the seam is not above you — this
section is the one that matters.

Under a dry run, `host.run` / `exec` / `put` / `get` still return a result
object rather than raising at the call site, and that object is built so that
it cannot be mistaken for a measurement.

### `Status.NotRun`, and `is_ok` is `False`

```python
result = (await host.run("uname -a")).only

result.status  # Status.NotRun
result.status.is_ok  # False
result.is_ok  # False
result.retcode  # -1   (otto's "never ran" sentinel)
result.exit_code  # 255  (ssh's "never connected")
```

`Status.NotRun` is a distinct member rather than a reuse of `Status.Skipped`,
because `Skipped.is_ok` is `True` and must stay that way for genuine skips — a
skipped test step, a folded transfer. `NotRun` means one thing only: *a dry run
declined this*.

`is_ok=False` is what a caller branching on the result acts on, and what the
CLI renderer keys on to announce a decline rather than parse one. It is worth
being precise about what it does **not** do:

```{important}
A hardened **return value** protects callers that **branch on it**. Only an
**early return** protects **actions below it**.
```

A caller that never looks at the status — one that discards the result and
carries on to the next step — is not protected by `is_ok=False` at all. That is
why otto's own `reboot`, `shutdown` and `power` verbs each carry an explicit
`is_dry_run()` arm at the *top* of the method rather than relying on the
declined result they would have received further down. If your own code
performs an action *after* a device call rather than *because of* it, it needs
the same treatment.

### Reading `.value` raises

The always-wrong case — parsing a non-measurement as if it were data — breaks
loudly, at the line that made the mistake:

<!-- doctest-lint: ignore -->
```python
>>> result.value
CommandNotRunError: 'uname -a' was not run on host 'localhost': this is a dry
run, which contacts no device. Build previews from configuration; if you need
the device's answer, drop --dry-run.
```

```python
from otto.result import CommandNotRunError
```

Fire-and-forget callers keep working; only the parse breaks. And *handling* the
object is always safe — `repr`, `str`, `==`, `hash` and containment never
raise, so logging a declined result, comparing two of them, or putting one in a
set does exactly what you expect:

<!-- doctest-lint: ignore -->
```python
>>> repr(result)
"NotRunResult(status=<Status.NotRun: 5>, value=<not run>, msg='', command='uname -a',
 retcode=-1, timed_out=False, host_name='localhost')"
```

### Return types that cannot carry a status raise instead

Some verbs return a plain value with nowhere to put "I did not look". Those
raise rather than fabricate:

<!-- doctest-lint: ignore -->
```python
>>> await host.exists("/etc/hostname")
CommandNotRunError: "exists('/etc/hostname')" was not run on host 'localhost': …
>>> await host.ls("/etc")
CommandNotRunError: "ls('/etc')" was not run on host 'localhost': …
>>> await host.expect("prompt")
CommandNotRunError: "expect('prompt')" was not run on host 'localhost': …
```

A `bool` has only `True` and `False`, and both are lies: `exists` returning
`False` under a dry run reports a path absent that may well be there, and a
caller that then creates it has acted on a fact nobody measured. `ls` returning
`[]` is a fabricated empty directory. `expect` returning `""` is a fabricated
prompt. Raising is the only honest answer these signatures allow.

`write_file` is the shape in between: it returns
`Result(Status.NotRun, msg="[DRY RUN] WRITE: 11 bytes -> /etc/motd")` with
`value` still `None`. A write measures nothing, so there is no payload to
poison — but `is_ok` is `False`, so
`if (await host.write_file(...)).is_ok:` no longer tells a caller the file was
written.

### Adapting run-parse-branch code

The pattern needing attention is the one that reads a device fact and decides
something from it:

```python
# Before — correct on a real run, raises under --dry-run at the `.value` line.
result = (await host.run("rpm -q otto-agent")).only
installed = result.value.strip()
if installed.startswith("otto-agent-2."):
    await host.run("systemctl restart otto-agent")
```

```python
# After — the decline is a first-class outcome, and the branch never runs on a
# fact nobody measured.
from otto.utils import Status

result = (await host.run("rpm -q otto-agent")).only
if result.status is Status.NotRun:
    return  # a preview: there is nothing to decide yet
installed = result.value.strip()
if installed.startswith("otto-agent-2."):
    await host.run("systemctl restart otto-agent")
```

Branching on `result.is_ok` instead is equally good where a failed command and
a declined one should be handled the same way. The point is that the branch is
made *before* `.value` is read, not that it is made on a particular field.

## Sessions

`open_session` under a dry run returns a **declining session**: the full
`HostSession` surface, announcing what would have been sent and measuring
nothing.

```python
session = await host.open_session("demo")

session.alive  # False
session.current_user  # the configured login user — from config, not the device
await session.run(...)  # Results of declines, each announced
await session.send(...)  # announces, does nothing, returns None
await session.expect(...)  # RAISES CommandNotRunError
```

`run` returns a `Results` collection, which carries declines natively. `send`
returns `None` and so has nothing to harden — it announces and does nothing.
`expect` returns a `str`, which has nowhere to put "I did not look", so it
raises for the same reason `host.expect` does.

```{important}
**A session preview is inherently a PREFIX.** Session scripts are
send → expect → send → expect, and step N+1 depends on what step N's output
said. So a script announces its first command or two and then raises at the
first `expect`. **That is the honest outcome, not a bug** — the script has
reached the point where it needs the device's answer to choose what to send
next, and there is no answer.
```

The error says so, rather than leaving you to work it out:

```text
CommandNotRunError: "expect('# ')" was not run on host 'localhost': this is a
dry run, which contacts no device. … A session preview is a PREFIX: everything
announced above is what configuration alone can produce, and this wait is the
point where the script needs the device's answer to choose what to send next.
The preview ended here because it ran out of things it can know — nothing is
wrong with the script.
```

If you want a session-driven procedure to preview further than its first
`expect`, the shape that works is to build the plan from configuration and
announce it, then run it — the same split the `link` and `tunnel` previews use
— rather than to expect the dry run to guess what the device would have said.

## What a dry run still does

One exception, and it is deliberate: otto reads **its own** SUT checkout's git
HEAD under a dry run, to stamp the run's provenance. That is a local, read-only
query about the machine otto is already running on, not a command on a device,
so declining it would be a false positive of the contract rather than
enforcement of it. It is the only such exemption, it is declared at the one
call site that uses it, and it does not extend to anything else — including
`otto host local run`, which declines under `-n` exactly like every other host.
