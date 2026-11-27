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

Which hosts and sessions keep this rule, and the declared exceptions to it, are
on {ref}`Command contracts <command-contracts>`.

A {class}`~otto.result.CommandResult`'s `value` is the command's output with
**exactly one trailing newline removed**: nothing stripped, nothing added.
That holds on a shell session without a terminal (an ssh session that has
none, and every local session, a bash session on pipes) and on a shell
session whose frame calibrates its terminal (the `bash` and `ash` frames) on
every term (`ssh`, `telnet`, `console`), for both `run` and `exec`. A
docker container's `exec` runs `docker exec` through its parent host, so its
`value` is designed to follow whichever of these sessions the parent's `exec`
takes; the command contract does not check a container's `exec` yet:

| The command prints | `value` |
| --- | --- |
| nothing (`true`) | `""` |
| `3` and a newline | `"3"` |
| `a`, then a blank line | `"a\n"` |
| a blank line, then `a` | `"\na"` |
| `abc` with no newline | `"abc"` |
| `a\r\nb\rc` and a newline | `"a\r\nb\rc"` |

Trailing spaces and tabs are kept, and so are the program's own carriage
returns. stderr arrives in the same stream as stdout, in the order the
command wrote the two: a session's shell writes both to one pipe or one
terminal, an ssh session without a terminal included. In a container the
command's two streams pass through the docker daemon, which can forward them
in its own order. The bytes are decoded as UTF-8, with any invalid byte
replaced.

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
  so does every `exec` ({ref}`exec-session`). Where that session
  has a terminal, a Linux terminal by default sends each `\n` as `\r\n` (`onlcr`).
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

  A session without a terminal (on ssh, one asked for with `terminal=False`
  and the one otto's own commands take; every local session) has no
  translation to undo; `terminal_mapping` always reports the terminal of a session that has one.
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
  before its close, which logs out or, where the shell outlives the session,
  leaves otto's work shell
  ({ref}`console-shell-outlives-session`). The settings are printed behind a marker of the
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

The log is a separate view of the same output: what it shows is in
{ref}`command-output-log`. `value` is the record.

`send()` writes to the raw stream and `expect()` reads it raw, with `\r\n`
kept: the fold applies only to a command's `value`.

(typed-command-text)=
A command string is text, not keystrokes. Where otto types it into a shell
on a terminal (`run()` and `exec()` by default, {ref}`terminal-switch`), a
TAB and a newline arrive as written (two exceptions: a BusyBox shell whose
terminal echo could not be turned off still takes a TAB as its completion
key, and on a BusyBox serial console a TAB arrives with a Ctrl-V before it,
[`typed-tab-literal-next`](../subsystems/busybox-support.md#typed-tab-literal-next)). Any other control character, such as
Ctrl-C, ESC or a carriage return that is not part of a CRLF line ending, raises
{class}`~otto.host.errors.UntypableCommandError` before the command is sent:
a terminal would act on it as a keystroke. To send a keystroke, use
`send()`. To give a command such a byte as data, spell it in the command
(`printf '\003'`).

```{note}
**Upgrading.** Before this, every command typed into a shell trimmed blank lines
from both ends of the output, deleted every `\r`, and left telnet's NUL bytes
in. That is `run()`, and `exec()` wherever it runs on a shell session: on
telnet and console, and on ssh when a session-setup hook, a proxied login or a
call that needs a terminal (*expects*, `sudo`, a switched user) gives it a
pooled session with a terminal. If you compare `value` exactly for a command that prints
blank lines or carriage returns, expect those now. `.strip()` callers are
unaffected by the blank-line change; output that contains its own carriage
returns (CRLF files, progress lines) now keeps them.

NVT decoding also changes what `expect()` patterns see on telnet and console:
a pattern that matched telnet's padding NULs (`\r\x00`) must drop them.
```

(command-output-log)=

## What the host log shows of a command's output

The host log receives the lines of the command's `value`
({ref}`command-output-contract`): `value` split on its newlines and nothing
else, one `@<host> > |` record per line. A blank line is a record of its own,
leading and trailing spaces are kept, and a program's own carriage return
stays inside its line (it never splits a record). A command whose `value` is
empty logs no output line. The rule is
{func}`otto.logger.relay.log_lines`, and it is the same for the same command
and `log=` mode on every shell session. What {meth}`~otto.host.session.ShellSession.expect` read is logged
by the same rule: the terminal mapping undone, one trailing newline removed,
one record per line. Under an unknown terminal mapping there is no mapping to
undo, so such a record keeps the terminal's `\r`.

What the record holds and what a sink shows can differ:

- **The record** holds the line as the program printed it: its message and
  its `relayed` attribute keep every `\r`, tab, control character and ANSI
  escape sequence. A {class}`logging.Handler` or filter of your own reads
  this.
- **A sink** (the console, `console.log`, `verbose.log`, and `otto login`'s
  `session.log`) cannot overwrite a line, so it shows each `\r`-separated
  part of a record as a line of its own, every one with the `@<host> > |`
  prefix. An empty part beside a non-empty one shows no line: `10%\r20%\r`
  and `\r10%\r20%` show as `@h > | 10%` and `@h > | 20%`, and `line\r\r`
  as `@h > | line`. A line with no non-empty part (a blank line, or a lone
  `\r`, which is how a blank line arrives under an unknown terminal mapping)
  shows as one prefixed blank line. `session.log` records the terminal of
  `otto login`, not a record, and its line reader drops a line that is empty
  once its ANSI escape sequences and final `\r` are removed: a blank output
  line leaves no line in `session.log`. A record that is not host output (a warning quoting a tool's
  progress, say) is shown the same way, without a prefix. Rich then expands
  tabs to spaces and drops BEL, backspace, vertical tab and form feed when it
  renders the line, and an ANSI escape sequence is rendered as the sink
  renders it. {func}`otto.logger.formatters.display_text` is the rule.

When a line is logged depends on the frame. A shell frame that streams (bash,
ash) logs each line as it completes. On a shell whose
terminal mapping is unknown, the newest line waits for the next line or the
end of the command, since only then is it known whether its final `\r` is the
terminal's (cut from `value`) or the program's (kept). A frame that does not
declare its output clean line by line buffers: it logs the lines once, when
the command ends. otto's raw frame buffers. otto's Zephyr frames stream with
their tail held back: the prompt and exit-code lines the shell sends after the
output look like output until the END record arrives, so the newest lines
(three on 3.7 and later, two on 2.7) wait for it, and at END the rest of
`value`'s lines are logged. An output that ends without a newline arrives
with the prompt glued to its last line, which is one of those held, so that
line is logged without the prompt: a synthetic transcript shows this, not one
recorded on a device. Prompts a plain
expect answered are output like any other and appear inside their line.

Three things are delimited on purpose:

- **A timed-out command** logs the complete lines it printed before the
  deadline, and nothing from its END record on. There is one gap: a timeout
  that lands before otto has read the command's first output logs nothing,
  though `value` may hold lines. The `Command timed out after <N>s` line and the unfinished last
  line are in its `value` ({ref}`timed out <timed-out-command-result>`), not in the log. A line that was waiting
  for the next one is logged when the command stops, and the log is then the
  complete lines of what its timed-out `value` shows, each once, on every
  frame (a buffered one, or one that held its tail back). On Zephyr a
  timed-out `value` can show the shell's prompt and exit-code lines, when they
  arrived before the deadline but END did not; the log shows them too. On a shell whose
  terminal mapping is unknown, that line keeps its final `\r`: with no end
  marker, otto cannot tell the terminal's CR from the program's.
- **A credential answer** stays out of the log: the read a
  {class}`~otto.host.host.CredentialExpect`'s prompt was matched in is cut
  from its line, so text before the prompt in that same read is cut too
  (`[sudo] password for u: ` read at once, with the pattern
  `password for \w+: `, loses `[sudo] ` as well); what follows the read on
  that line is logged, and a line left empty is not. Only a
  `CredentialExpect` is cut: a plain `(pattern, response)` expect is output
  like any other, even when its response is a password.
- **A command that never ran** (its end record came with no BEGIN marker)
  logs no output line on any frame: its `value` is otto's sentence, not
  output.

```{admonition} Upgrading
The log of a command typed into a shell used to drop blank lines, strip
leading and trailing spaces, delete carriage returns and leave out what an
expect had matched; and a carriage return split a record on every session. The log is now
the lines of `value`, as above, and a sink shows each carriage-return part of
a line as a line of its own.
```

(exec-session)=

## Which session an exec takes

Every `exec` is typed into a shell session, wrapped in a line otto adds
around it (the {ref}`exec frame <named-sessions-vs-exec>`). On most hosts
that session is one otto keeps for `exec` alone, beside the one `run` uses
({ref}`host-exec-pool`); on a console or an embedded host it is the one
session `run` uses. Which session each call takes, and whether it has a
terminal, is on {ref}`the exec contract page <command-contract-exec>`. What
the `exec` shares with `run` depends on the host and, where the host has one
session, on its frame (`CommandFrame.isolates_exec`):

| Host | Starts from what `run` left | What the `exec` changes reaches later commands |
| --- | --- | --- |
| ssh, telnet, the local host | no | no |
| a console, or an embedded host whose frame isolates (bash, ash) | yes: `run`'s directory, variables and shell options | no: a `cd`, a variable, an option or an `exit` stays inside the `exec` |
| an embedded host whose frame does not isolate (a Zephyr shell) | yes | yes: its shell runs an `exec` as it runs a `run` (not measured on a board) |
| a docker container | no | no: each `exec` is a new `docker exec` through the parent host ({doc}`../subsystems/docker-hosts`) |

On a console `$$` inside an `exec` is the console's one shell, not a shell
of the `exec`'s own. What the exec frame isolates and restores is
{ref}`exec-frame-shell-state`.

(exit-codes)=

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

Every `run` and every `exec` runs on a shell session
({ref}`host-exec-on-sessions`), so the shell reports how its command ended.

**Killed by a signal.** A command killed by signal `N` reports `retcode`
`128 + N`, the number a POSIX shell reports for the same death, so
`kill -TERM` of the command reads `143`. An `exec` runs its command in a
subshell. Of the shells measured, only ksh93 reports a killed subshell's
status differently, as `256 + N`; otto reports that as `128 + N` too
(`otto.host.exit_status.subshell_status_of`), and the session's shell
outlives the command.

On the local host, a command that kills the session's shell itself
(`kill $$`: in an `exec`'s subshell `$$` is still the session's shell) ends
the session rather than the command: the call gives an error result,
`retcode` `-1` and the `value` `Session died unexpectedly`, and the next call
runs on a fresh session. A child the command started that is killed by a
signal still reports `128 + N`.

(incomplete-or-invalid-command)=
A command that is not complete or valid shell, on a session whose shell
reads a pipe (an ssh `exec(..., terminal=False)`, every local `exec`), is typed
into that shell as it is. An incomplete one (an unclosed quote, here-document
or `if`) takes in the end of otto's frame, so the call waits out its whole
timeout and gives the timed-out result (below). An invalid one
(`echo a; echo )`) runs what comes before the error, then the shell exits on
it: the call gives the error result above, `retcode` `-1`, `Session died
unexpectedly`, without the shell's diagnostic. The next call runs on a fresh
session either way. A caller that builds a command and wants the shell's
verdict checks it first with `sh -n`.

`otto.host.exit_status.gave_no_exit_status` is what otto's own code asks
before it parses a `value`: it holds for a timed-out result and for one with
`retcode` `-1`. A `retcode` of `-1` from a shell session is otto's
own (the session or its transport died before the command gave a status),
and its `value` is otto's text. Two such results carry the command's own
output all the same: a timed-out result's `value` holds what the command had
written (below), and on a Zephyr board a firmware command that returns
`-EPERM` has `retcode` `-1` from the firmware ({ref}`Exit codes
<exit-codes>` above).

(timed-out-command-result)=
**Timed out.** A command that is still running when its budget expires gives
the timed-out result on every session: `timed_out` `True`, `status` `Error`,
`retcode` `-1` and `exit_code` `255`. Where that is checked is on
{ref}`Command contracts <command-contracts>`. The budget bounds the command,
not only its output: a command that closes its output and keeps running times
out too. The `value`'s first line is `Command timed out after <N>s`, and so
is the `msg`, which the command line prints. When the command had printed
anything, the next line starts what it had written when
the budget expired, by the same rule as a finished command's value
({ref}`command-output-contract`): one final newline removed and nothing else,
an unfinished last line included. When it had printed nothing, that first line
is the whole `value`. A `timeout` of zero, or an `exec` whose time ran out
before its command was typed, types nothing and gives this result; its
session is still stopped as below, so on an ssh session without a terminal
whatever earlier commands on that session left running is stopped too.
Nothing the shell printed after otto interrupted the command (the echoed
`^C`, the next prompt) is in it. It is built in one place,
{func}`otto.host.session.timed_out_result`; the frame says what of the
unfinished buffer is the command's
({meth}`~otto.host.command_frame.CommandFrame.parse_partial_output`).

**How the command is stopped.** A timed-out command, one whose password was
refused, and a cancelled one are stopped before the call returns or the
cancellation is re-raised. A cancellation landing during the stop is held
until it ends. How depends on the session:

- **`run` on a session that is kept** (one with a terminal, or the local
  host's), **and an `exec` on a console or an embedded host** (the host's one
  session): otto types an interrupt (`^C`), checks that the
  shell answers again under one bound (the settle after the interrupt, the
  recovery deadline and one probe wait, added together) and, for an `exec`,
  puts the terminal settings and `set -e` back
  ({ref}`what the exec frame restores <exec-frame-shell-state>`). Once the
  call returns, either the shell is answering again, as it was before the
  command, or the session is dead and will not be used again
  ({ref}`when a dead session is closed <closing-a-session>`). On the local
  host the interrupt is a `SIGINT` to the shell's children, since `^C` does
  not cross a pipe.
- **`run` on an ssh session without a terminal** ends the session
  ({ref}`the ssh stop <terminal-switch-stop>`).
- **An `exec` on a pooled session** (ssh, telnet, the local host): the
  command is stopped as the table below says, nothing is restored, and the
  session is closed; it is never
  handed out again, and the next `exec` runs on a fresh session
  ({ref}`host-exec-pool`).

(exec-stop-reach)=
**What the stop reaches.** Only the command's own end is waited for, never
its output. What else the stop reaches depends on the session the command
ran on:

| Session | What is stopped | What is left running |
| --- | --- | --- |
| ssh, without a terminal (`run` or `exec` with `terminal=False`, and otto's own commands) | `STOP`, then `KILL`, with no `TERM` or `HUP` first, so a command gets no chance to flush or clean up (a `tcpdump -w` loses what it buffered), to every process started with the session's `OTTO_SESSION` token in its environment, whether or not it was detached with `setsid`: the shell, the command, what it started, and a background process an earlier command on the session left ({ref}`the ssh stop <terminal-switch-stop>`) | a process started with `OTTO_SESSION` unset, a process running as another user, and one that removed the token from its environment |
| a session with a terminal (on ssh and telnet, `run` and `exec` by default) | the command is interrupted with `^C`, which reaches its foreground processes, and the call returns once the shell answers again (or the bound above ends); a pooled `exec` session is then closed, and what the command left gets whatever the transport does when a terminal hangs up | what the interrupt does not reach (a background process, an earlier command's included) or ignores, and what the hang-up does not end |
| the local host | when the session is closed, its shell's process group gets `TERM`, and whatever is still in the group 5 seconds later gets `KILL`, the shell included: so the command and every process left in the group, a `nohup` child and a process that ignores `TERM` included; a group that is empty when the shell exits ends the stop at once | a process that moved into a session of its own (`setsid`) with its output redirected |
| a docker container | the `docker exec` client on the parent | the command inside the container, which keeps running ({ref}`docker-exec-stop` has the workaround) |

When a local `exec` ends normally, otto ends its shell's input and waits up
to 2 seconds for the shell to exit; a shell that does exit gets no signal, so
a background process the command left keeps running, and one that did not is
stopped by the close as above. Either way the close releases the shell's
pipes, so a background process that still writes to them gets `EPIPE` (or
`SIGPIPE`): redirect the output of anything you leave running.

(exec-detached-launch)=
To leave a process running past a stop on ssh or the local host, start it in
a session of its own with its output redirected, and on ssh with the token
unset (only a session without a terminal sets it, and unsetting it elsewhere
does nothing): `(unset OTTO_SESSION; setsid my-daemon </dev/null >/dev/null 2>&1 &)`.
`otto.host.daemon.launch_command` starts otto's own daemons that way.

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
