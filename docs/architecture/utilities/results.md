# Results and exit codes

Every host verb — and, through the CLI, every command built on one — speaks
one vocabulary for "what happened": the {mod}`otto.result` family. There is
no separate exit-code logic anywhere in the CLI to drift out of sync; codes
are *derived* from results.

## The Result family

```{inheritance-diagram} otto.result.CommandResult otto.result.Results
:parts: 1
```

- {class}`~otto.result.Result` — status + optional payload (`value`) + human
  diagnostic (`msg`). Truthiness follows {attr}`~otto.result.Result.is_ok`
  (Success or Skipped), never the payload — `if result:` always asks "did it
  work?".
- {class}`~otto.result.CommandResult` — one shell command: adds the `command`
  string, the shell `retcode`, and `timed_out`. `retcode -1` no longer implies
  "never ran" on its own — it is shared by "never ran", "timed out", and
  "skipped: cumulative budget exhausted". `timed_out` is how a caller tells a
  timeout apart from an ordinary failure, since `retcode` alone cannot.
  Everything a timed-out command's result holds is under
  {ref}`Timed out <timed-out-command-result>` below.
- {class}`~otto.result.Results` — the aggregate `run()` returns: a `Result`
  that is also a `Sequence[CommandResult]`. Its status is the first non-ok
  entry's status; `only` asserts exactly one command ran and returns it;
  `first_failure` finds the culprit in a batch. Transfer verbs aggregate
  per-file results the same way.
- {class}`~otto.result.NotRunResult` — a `CommandResult` for a command a dry
  run declined to issue. The one member of the family that is deliberately
  *not* data: reading `value` raises
  {class}`~otto.result.CommandNotRunError` naming the command and the host,
  so the always-wrong class of caller — the one that parses a device's answer
  — breaks at the line that mistook a non-measurement for a measurement,
  while `status`, `retcode`, `timed_out` and `command` stay plain reads for
  fire-and-forget callers.

The shared vocabulary is {class}`~otto.utils.Status`: `Success`, `Failed`,
`Error`, `Unstable`, `Skipped`, `NotRun`. `Skipped` is ok and `NotRun` is not,
and the split is load-bearing: `Skipped` marks a *genuine* skip (a test step,
a transfer fold) that callers should sail past, so it cannot be made non-ok;
`NotRun` means a dry run declined to contact a device, and every `is_ok`
branch must take its failure arm rather than proceed on a fiction.

(command-output-contract)=

## What a command's `value` holds

Which hosts and routes keep this rule, and the declared exceptions to it, are
on {ref}`Command contracts <command-contracts>`.

A {class}`~otto.result.CommandResult`'s `value` is the command's output with
**exactly one trailing newline removed**: nothing stripped, nothing added.
That holds on the ssh `exec` channel; on a local host, for both `exec` and
`run` (a bash session on pipes, with no terminal); and on a shell session
whose frame calibrates its terminal (the `bash` and `ash` frames) on every
term (`ssh`, `telnet`, `console`) and both routes (`run` and `exec`). A
docker container's `exec` runs `docker exec` through its parent host, so its
`value` is designed to follow whichever of these routes the parent takes; the
command contract does not check the container route yet:

| The command prints | `value` |
| --- | --- |
| nothing (`true`) | `""` |
| `3` and a newline | `"3"` |
| `a`, then a blank line | `"a\n"` |
| a blank line, then `a` | `"\na"` |
| `abc` with no newline | `"abc"` |
| `a\r\nb\rc` and a newline | `"a\r\nb\rc"` |

Trailing spaces and tabs are kept, and so are the program's own carriage
returns. stderr arrives in the same stream as stdout. The bytes are decoded as
UTF-8, with any invalid byte replaced. (The ssh channel and local `exec` share
one line rule, {func}`otto.host.session.output_line`.)

The Zephyr frames (`zephyr` and `zephyr-serial`, and the `zephyr-inline`
example frame for Zephyr 2.7) follow the same rule. They remove exactly what
the Zephyr shell adds — its prompt lines, the `retval` (or 2.7's
`retCode = <n>`) line, the rejected marker lines, the colour the shell puts
around each error and warning line (the three sequences are listed on the
command contracts overview, [Where a device gets a narrower promise](../../command-contracts/index.md#where-a-device-gets-a-narrower-promise)), and the `\r`
of each `\r\n` line ending — and keep every byte the command wrote, other
escape sequences (such as the cursor moves in `help`) and blank lines
included: a Zephyr command's `value` is its exact output, with the one final
newline removed as on every host. Because those three colour sequences are
removed wherever they appear, a Zephyr command that writes one of them itself
loses it from its `value`. This changed in the release after 0.20.0; before it, the Zephyr frames
deleted every escape sequence and every `\r` and trimmed both ends, and the
2.7 example frame also dropped blank lines. The model each frame follows is
recorded from the bed in `tests/unit/host/shell_transcripts/README.md`.

A Zephyr command's `retcode` is the integer the shell's own status line
reports right after the command: the stock `retval` builtin's output (2.7's
`retCode = <n>` line). The values are the firmware's, in negative-errno style
(`-8`, `-ENOEXEC`, for an unknown command), not POSIX shell exit codes.

To return the exact output, otto undoes two things that sit between the program and otto,
and nothing else:

- **Telnet's own encoding.** A telnet server sends a bare carriage return as
  `CR NUL` (RFC 854). otto decodes that on every telnet and console stream
  before it reads anything, unless the server negotiated BINARY.
- **The terminal's newline translation.** `run` runs on a shell session, and
  so does every shell-routed `exec` (see the note below). A shell session runs
  on a terminal. By default a Linux terminal sends each `\n` as `\r\n` (`onlcr`).
  otto never changes that setting. The session's readiness handshake measures
  it instead with a **calibration line**, a `printf` of known bytes that runs
  just before the handshake's READY marker ({ref}`the session setup section of the host options page <per-host-session-setup>`),
  and the session undoes exactly that translation. The measurement is the
  session's `terminal_mapping`, and `otto host <id> probe` reports it per
  term:
  - `identity`: nothing is translated, so nothing is undone;
  - `onlcr`: each `\n` arrives as `\r\n`, so each `\r\n` is folded back to
    `\n`, the exact inverse;
  - `unknown(<what was seen>)`: some other translation that can't be undone
    exactly, or no calibration line came back at all (a target with no
    `printf`). The output comes back as it arrived, and the session logs one
    WARNING saying which;
  - `unmeasured`: the session's dialect (the shell language its command
    frame speaks, such as `bash`, `ash` or `zephyr`) sends no calibration
    line, so the terminal mapping undoes nothing. That is Zephyr's case, and
    it does not mean Zephyr output arrives untouched: the Zephyr frame itself
    removes the `\r` of each `\r\n`, as described above.

  The ssh `exec` channel has no terminal, so there is nothing to undo there,
  and `terminal_mapping` always reports the shell session's terminal.
- **The terminal's tab expansion.** A terminal with `tab3` sends each tab as
  the spaces up to the next tab stop, and that has no exact inverse: `a\tb`
  and `a       b` arrive the same. The pty BusyBox telnetd allocates has it
  on (measured on every BusyBox release in otto's lab). So, unlike line
  endings, otto changes this setting: the handshake runs `stty tab0` on the
  session it opened, in a statement of its own so a `stty` that refuses it
  still turns echo off, and the calibration line carries a tab to measure
  whether it took. Where it did not (no `stty`, or one that rejects `tab0`),
  `terminal_mapping` reads, for example, `onlcr, tabs expanded`: the line
  endings are still folded exactly, a tab a command writes arrives as spaces,
  and the session logs one WARNING. On an ssh or telnet login the change
  lasts only as long as that login's pty: handing the session to a person
  (`otto host <id> login` after a session-setup hook) turns echo back on and
  leaves tabs unexpanded, which a terminal emulator draws itself. A serial
  console's line outlives otto's session, so there otto reads the settings
  (`stty -g`) on the same handshake line before changing anything, and puts
  them back, echo and tabs together, when it hands the line to a person and
  before it logs out. The settings are printed behind a marker of the
  session's own, so other text on the console can't pass for them. A
  session that ends without a clean close (otto killed, the link cut)
  leaves them changed, and so does a close while a timed-out command still
  holds the line or the session is inside an application shell: otto types
  nothing into either. The module docstring of
  `otto.host.terminal_mapping` has the details.
- **Whether the terminal still echoes.** The handshake turns echo off
  (`stty -echo`) and the calibration line shows whether that took. Where it
  did not (no `stty` on the target, or one that refuses it),
  `terminal_mapping` gains `, echo on` (for example `onlcr, echo on`) and the
  session logs one WARNING on an `ash` host and only a debug line elsewhere.
  The `, echo on` suffix is an observation: it only limits line length on an
  `ash` shell, where `run` then refuses a line over 1022 characters instead of
  4095 ({doc}`the line-length record <../subsystems/busybox-support>`).

A command that changes its own terminal (`stty -onlcr`, or a program that puts
the terminal in raw mode and leaves it that way) changes what later output on
that session looks like. The session measured its terminal when it opened, and
it doesn't measure again until a new session opens. What otto leaves
undefined is listed once, on {ref}`Command contracts <command-contracts>`.

The log is a separate view. Live output lines are cleaned for reading, so
carriage returns are removed and the shell route skips blank lines. `value` is
the record.

`send()` writes to the raw stream and `expect()` reads it raw, with `\r\n`
kept: the fold applies only to a command's `value`.

```{note}
**Upgrading.** Before this, every shell-routed command trimmed blank lines
from both ends of the output, deleted every `\r`, and left telnet's NUL bytes
in. That is `run()`, and `exec()` wherever it runs on a shell session: on
telnet and console, and on ssh when a session-setup hook, a proxied login or a
call that needs a shell (*expects*, `sudo`, a switched user) routes it through
the pooled shell. If you compare `value` exactly for a command that prints
blank lines or carriage returns, expect those now. `.strip()` callers are
unaffected by the blank-line change; output that contains its own carriage
returns (CRLF files, progress lines) now keeps them.

NVT decoding also changes what `expect()` patterns see on telnet and console:
a pattern that matched telnet's padding NULs (`\r\x00`) must drop them.
```

## Which route ran a command

A {class}`~otto.result.CommandResult`'s `route` names the route that ran the
command, so a caller (or a test) can assert how a call was carried out and not
only what it returned. It is one of the {class}`~otto.host.exec_route.ExecRoute`
values:

| `route` | The command ran on |
| --- | --- |
| `ssh-channel` | a bare ssh exec channel, no pty |
| `pooled-shell` | a pty shell session kept for `exec` beside the one `run` uses |
| `default-session` | the host's one default session; every `run` reports this |
| `local-subprocess` | a local process |
| `container/<route>` | a container, prefixed to its parent host's route |

`route` is `""` for a result no host route produced: a dry run's
{class}`~otto.result.NotRunResult`, a command on a named session
({meth}`~otto.host.session.SessionManager.open_session`), or a docker build. It is informational
and takes no part in equality or hashing, so two results that differ only in
route compare equal.

Which route a given call takes is decided by the host's configuration and the
call's arguments; the routes are listed in
[Which route your command takes](../../command-contracts/index.md#which-route-your-command-takes),
and when each command takes each is on its own page ({ref}`run <command-contract-run>`,
{ref}`exec <command-contract-exec>`).

## Exit codes

`Result.exit_code` is `0` when ok, else the status value.
`CommandResult.exit_code` follows the ssh convention users already know
(on which hosts it holds is on each command's page under
{ref}`Command contracts <command-contracts>`):

| Situation | Exit code |
| --- | --- |
| Command succeeded | `0` |
| Command ran and failed | the shell's own `retcode` |
| Command never ran (connection/timeout) | `255` |
| Failed without a retcode | the `Status` value |

On a Zephyr board the shell's own `retcode` is the firmware's return value,
in negative-errno style, so a failed command's `exit_code` is negative (`-8`
for an unknown command). `-1` is the one value it cannot carry: `retcode -1`
is also how otto records a command that never ran, so a firmware command that
returns `-1` (`-EPERM`) reads as `exit_code` `255`, and `status` and
`timed_out` are what tell the two apart.

A `@cli_exposed` host verb returning any `Result` gets these semantics on the
CLI for free; returning a `bool` exits `0`/`1` for `True`/`False`, and any
other plain value exits `0` ({ref}`cli-return-values`). See
{doc}`../subsystems/hosts` for how verbs become CLI commands.

(command-ends-abnormally)=

## A command killed, timed out or interrupted

The process routes are the ones that run a command as a process of its own:
the ssh exec channel (`ssh-channel`) and local `exec` (`local-subprocess`).
Below, the *leader* is the process otto started for the command, and its
*process group* is that process together with the children it started that
did not move to a group of their own.

**Killed by a signal.** A command killed by signal `N` reports `retcode`
`128 + N` on every process route, the number a shell reports for the same
death, so `kill -TERM` of the command reads `143` whichever route ran it. That
holds for every signal number the platform has, the real-time signals
included. A process that ends with no return code at all (an ssh channel that
closed with neither an exit status nor a signal, or with a signal name the ssh
library does not know) gives one named error result instead: `status`
`Error`, `retcode` `-1` and the `value` `Process did not provide a return
code`. The rule is `otto.host.exit_status.exit_status_of`. A shell route (`run`,
and an `exec` that otto types into a shell session) reports what the shell
itself reports for its command, which is the same `128 + N` on a POSIX shell.

(timed-out-command-result)=
**Timed out.** A command that is still running when its budget expires gives
the timed-out result on every route: `timed_out` `True`, `status` `Error`,
`retcode` `-1` and `exit_code` `255`. Where that is checked is on
{ref}`Command contracts <command-contracts>`. The budget bounds the command,
not only its output: a command that closes its output and keeps running times
out too. The `value`'s first line is `Command timed out after <N>s`. When the
command had printed anything, the next line starts what it had written when
the budget expired, by the same rule as a finished command's value
({ref}`command-output-contract`): one final newline removed and nothing else,
an unfinished last line included. When it had printed nothing, that first line
is the whole `value`. On a shell route nothing the shell printed after otto
interrupted the command (the echoed `^C`, the next prompt) is in it. It is built in one place,
{func}`otto.host.session.timed_out_result`; on a shell route the frame says
what of the unfinished buffer is the command's
({meth}`~otto.host.command_frame.CommandFrame.parse_partial_output`).

**How long the stop takes.** After a timeout or an interrupt, a process route
stops its command. The first signal is `TERM` on a timeout and `HUP` on an
interrupt; a command that obeys it ends the sequence there, and `KILL` follows
only when the command has not exited 5 seconds after it, so the stop takes at
most 10 seconds (`EXEC_CANCEL_REAP_BOUND` in `otto.host.host`). An interrupted
stop is also cut by the teardown deadline ({doc}`../lifecycle`), counted from
the interrupt, so a deadline under 5 seconds ends it before `KILL` is sent. A
further cancellation while the stop runs is held until it ends, on both
routes. A stop that fails (the channel already gone) is logged, and the exec
still returns the timed-out result or re-raises what interrupted it.

**What survives a stop.** Only the command's own exit is waited for, never its
output: a member that still holds the output once the command has exited does
not hold the stop. What else is left running depends on the route:

| A process the command started | Local `exec` | ssh channel |
| --- | --- | --- |
| Signalled | the signal goes to the command's process group (it runs in a new session), so the command and its members all get it; `KILL` goes to the group too when the leader has not exited in the bound | the signal goes to the server, which decides whether it reaches the command's descendants; that is the server's, and otto does not measure it |
| Left running | a member that outlives the leader is left alone: once the leader has exited nothing signals the group again, so a member that ignored the first signal keeps running; when the leader had already exited at the timeout (a member still holding its output), nothing is signalled at all | whatever the server did not signal |

## The convention, and its two gates

Public otto API says what happened in exactly one of three ways:

1. it **returns a `Result`-family value** ({class}`~otto.result.Result`,
   {class}`~otto.result.CommandResult`, {class}`~otto.result.Results`);
2. it **raises** — a named {class}`~otto.errors.OttoError` subclass when the
   failure is one otto has a name for, otherwise the plain stdlib exception
   that fits (rejecting an argument with a bare `ValueError` is ordinary and
   is not going to change); or
3. it returns a plain value and **documents why** — the predicate and
   accessor verbs (`exists`, `is_installed`, `is_uninstalled`, `ls`,
   `read_file`) plus `login` (which returns `None`) are the whole list.

The naming, not the raising, is what the convention governs: of otto's 330
bare stdlib raises, 152 sit in a public function today, and `except
OttoError` is a question about otto's *named* failures rather than a
catch-all. {mod}`otto.errors` says what each clause does and does not reach.

The extension surfaces follow it too: a {class}`~otto.host.product.Product`'s
`stage` / `install` / `uninstall` return a `Result` — usually the one
`host.run` or `host.put` already produced, so the command's retcode and output
reach the process exit code rather than being flattened on the way out.

`Status` is the vocabulary carried *inside* a result, **never a return type
of its own**: a caller handed a bare `Status` cannot see the exit code, the
command, or the output that explains it.

Both halves are gated rather than documented-and-hoped:

- **returns** — `.ast-grep/rules/no-bare-status-return.yml` fails any public
  function in `src/otto` whose return annotation mentions `Status` anywhere,
  including inside a composite: `tuple[Status, str]`, `Status | None`,
  `dict[str, tuple[Status, str]]` (run by `make lint-arch`).
- **raises** — a test sweeps every public exception class and fails on one
  that does not reach `OttoError`, on one that is not declared in its table
  of expected roots, and on one whose only declared stdlib root is bare
  `Exception` without being listed as deliberately rootless (that row would
  otherwise assert nothing, since every exception is an `Exception`).

  Note the scope: the gate is about the exceptions otto *defines*. It says
  nothing about raise sites, and deliberately so — see above.

## Where the code lives

- {mod}`otto.result` — `Result`, `CommandResult`, `Results`, and the
  exit-code derivation
- {mod}`otto.errors` — `OttoError`, the root of the raised half
- {mod}`otto.utils` — the shared `Status` vocabulary
