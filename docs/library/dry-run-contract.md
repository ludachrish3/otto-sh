# The dry-run contract

What `--dry-run` means to code you write — how a declined operation is
reported, why reading its value raises, and how to adapt
run-parse-branch code. For the CLI behaviour, see
{doc}`../guide/cli/dry-run`.

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

See {doc}`../guide/cli/link/index` and {doc}`../guide/cli/tunnel/index` for what each of those
previews can and cannot tell you.

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
>>> await host.glob("/var/log/messages*")
CommandNotRunError: "glob('/var/log/messages*')" was not run on host 'localhost': …
>>> await host.toolchain_tools_absent()
CommandNotRunError: "toolchain_tools_absent('gdb')" was not run on host 'localhost': …
>>> await host.is_clean()
CommandNotRunError: "toolchain_tools_absent('gdb')" was not run on host 'localhost': …
>>> await host.expect("prompt")
CommandNotRunError: "expect('prompt')" was not run on host 'localhost': …
```

That last line is the **example host's** answer, not a universal one.
`is_clean` asks this host's products and dev tools before it asks the
toolchain, and those two questions run project-supplied `is_installed` hooks —
what they do under a dry run is the hook's business, so a host carrying them
can refuse earlier and name something else. The example host carries neither
and declares a `gdb` toolchain tool, which is why the toolchain probe is both
the first thing it asks and the only thing it can refuse on.

A `bool` has only `True` and `False`, and both are lies: `exists` returning
`False` under a dry run reports a path absent that may well be there, and a
caller that then creates it has acted on a fact nobody measured. `ls` returning
`[]` is a fabricated empty directory, and `glob` returning `[]` a fabricated
"nothing matched" — the shape a log collector reads as "this host has no logs".
`toolchain_tools_absent` returning `True` — and `is_clean`, which asks it —
would report a host clean that nobody looked at, and send a converge into a
cleanup on a fact nobody established. `expect` returning `""` is a fabricated
prompt. Raising is the only honest answer these signatures allow.

The *acting* verbs beside them decline rather than raise, because a `Result` has
somewhere to put "I did not look": `install`, `uninstall`, `cleanup`,
`get-logs`, `install-tools`, `install-dev-tools`, `install-toolchain-tools` and
`remove-toolchain-tools` each hand back the `Status.NotRun` of the first
transfer or command they would have made. `get-debug-logs` is the exception in
the other direction — a `debug_log_globs` entry that is a *pattern* has to be
expanded by `glob` first, and that is a raise. It is not only reachable by
name: `uninstall`, `cleanup` and `get-logs` all gather debug logs by default,
so on a host whose `debug_log_globs` carries a pattern they raise where the
rest of this paragraph would have you expect a decline. `uninstall` and
`cleanup` always get that far — they are best-effort, so a declined product-log
haul is recorded and the walk continues to the debug half regardless.
`get-logs` is the one that may not: it returns as soon as its product half comes
back **non-ok**, so a host whose product-log haul declines hands back that
decline and never reaches the glob. Carrying products is not by itself enough to
get that decline — `get_product_logs` contacts nothing on its own, and
`Product.get_logs`'s default retrieves nothing *successfully*, so a host whose
products leave that hook alone still passes an ok product half through to the
raise. What declines is a hook that actually attempts a transfer or a command,
which is the same "it is the hook's business" point made two paragraphs above.
Turn the debug half off, or declare concrete paths, to get the decline back
everywhere.

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
