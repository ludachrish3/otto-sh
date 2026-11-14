<!-- GENERATED FILE -- do not edit by hand.
     scripts/render_support_matrix.py (render_command_contracts) renders this
     from the `entries` marker on each command contract and the limitations
     declared on otto's built-in command frames, exec routes and console settings.
     Regenerate with `uv run python -m scripts.render_support_matrix`;
     edits are overwritten. -->

(command-contracts)=

# Command contracts

A **command contract** is a promise otto makes about the result of a
command you run on a host -- its exit code, its output, its timeout --
together with the test that checks it. This page holds what every command
shares. Each command's own page, listed below, says which promises are
checked through it, and where.

## Three words

| Word | Means | Examples |
| --- | --- | --- |
| **term** | how otto reaches the host | `ssh`, `telnet`, `console` |
| **entry** | which call you made | `run`, `exec`, shell-routed `exec` |
| **route** | which path the command then took | listed in [Which route your command takes](#which-route-your-command-takes) |

A host's term is set in its record in the lab file
({doc}`Host options </configuration/host-options>`). The route follows from
the term, the entry and the call's arguments; each command's page says how.
Other words these pages use:

- **lab**: the hosts a project describes in its lab file, which otto connects to.
- **test bed**: the virtual machines and emulated boards otto's own lab tests against.
- **ash**: BusyBox's shell.
- **BusyBox applets**: the small commands BusyBox provides, one program standing in for many.
- **echo**: a terminal sending back each character typed, so it shows on screen.
- **IAC**: the telnet byte 0xFF, which starts a telnet command; `escapes_iac` in a console's settings says whether its server doubles a 0xFF the command wrote.
- **proxied login**: otto logs in as one account, then switches to the one it runs as ([Credentials and login proxies](../configuration/host-sources.md#credentials-and-login-proxies)).
- **session-setup hook**: project code otto runs on every shell session it opens ({ref}`Session setup <per-host-session-setup>`).

## What you see

From the CLI, `otto host <id> exec` shows the command's output as it runs and
exits with the command's exit code, or 255 when the command never finished.
A timed-out command prints no message of its own: you see the output it wrote
before the budget expired, and the exit code 255
({doc}`/cli/host/exec`, {ref}`the host exit codes <host-exit-codes>`). From
Python, `run` and `exec` hand back a {class}`~otto.result.CommandResult` whose
fields hold the same answers: `retcode` is the command's own return code,
`exit_code` is what the CLI exits with, and `value` is the output. For
example (the result built here stands for what `await host.exec(...)`
returns):

```{doctest}
>>> from otto.result import CommandResult
>>> from otto.utils import Status
>>> result = CommandResult(
...     status=Status.Success, retcode=0, value="hello", route="ssh-channel"
... )
>>> result.route, result.timed_out, result.value
('ssh-channel', False, 'hello')
```

## The commands covered

- {doc}`run <run>`: sends a command to the host's one persistent shell session.
- {doc}`exec <exec>`: runs a one-off command, with no state carried from one call to the next.

A command not listed here has no contract yet: nothing on these pages is
promised for it.

```{toctree}
:hidden:

run
exec
```

(command-contracts-checked)=

## How otto checks a contract

Each promise has a test in otto's conformance suite. The suite runs the test
once through each entry it declares, on each **kind of host** below. Some
need no lab and run on the machine running the suite; the rest are on the
lab's **test bed**, the virtual machines and emulated boards otto's own lab
tests against.

Needing no lab:

- **The local machine**: the machine otto itself runs on.
- **The local machine logged in to over SSH**: the same machine, reached over SSH as if it were remote.
- **BusyBox's applets run on the local machine**: BusyBox's commands (its applets) put on the `PATH` of a `run` session on the local machine, standing in for a BusyBox device without a lab.

On the lab's test bed:

- **A GNU/Linux machine**: a full Linux system, with the GNU tools and bash. Reached over `console`, `ssh` and `telnet`.
- **A BusyBox device**: a small Linux system whose commands and shell (ash) all come from BusyBox. Reached over `console`, `ssh` and `telnet`.
- **A Zephyr board**: a small board running the Zephyr operating system, driven through its own shell. Reached over `console` and `telnet`.

Whether a promise held, device by device, is in the
{doc}`support matrix </architecture/support-matrix>`.

(command-contracts-promise-id)=

Each promise has a **Promise id**: a fixed name, shared by every command's
page and by the support matrix. The `exec-` at the start of most ids is
historical; it does not mean the `exec` command.

## Which route your command takes

otto picks the route for each call. A command takes one of these:

- `default-session`: the host's one persistent shell session, the one `run` uses.
- `pooled-shell`: a shell session otto keeps for `exec` beside the one `run` uses.
- `ssh-channel`: a bare SSH channel of the command's own, with no terminal.
- `local-subprocess`: a fresh process on the local machine.
- `container`: a docker container's command, run as `docker exec` through its parent host; its result reports `container/` and the route the parent took.

Each command's page says when it takes each. From Python, read the result's
`route` field
([Which route ran a command](../architecture/utilities/results.md#which-route-ran-a-command));
`otto host <id> exec` does not print it.

(command-contracts-not-promised)=

## How to read what is not promised

A promise holds where it is checked, unless a declared exception says
otherwise. Anything else is not promised, in one of three ways:

1. **Not checked.** The promise exists, but no test covers this kind of host
   or this entry, so it is not promised there. A command's page says where,
   beside each promise, with a numbered note saying why. For example,
   `exec-exit-code` is checked through `exec-shell` only on a GNU/Linux machine over `ssh`, a BusyBox device over `ssh` and the local machine logged in to over SSH.
2. **A declared exception.** The device, or a library otto is built on (such
   as telnetlib3), makes the promise impossible, and otto says so in its
   code. Each is listed [below](#declared-exceptions) with its measurements.
   For example, a long line typed into a terminal is cut.
3. **Undefined.** There is no promise at all, because no device has been
   measured behaving this way. Each is listed under
   [Undefined](#undefined-never-measured-so-not-promised). For example, what
   a tab looks like on a terminal that turns tabs into spaces.

## What the output and timeout promises mean

What a command's output holds, line by line, is defined in
{ref}`What a command's value holds <command-output-contract>`.

The time budget is the call's `timeout`: 30 seconds by
default ({data}`~otto.host.host.DEFAULT_COMMAND_TIMEOUT`), for `run` and
`exec` alike, and `--timeout` on the CLI. A command that outlives it does
not raise. Its result has `timed_out` set to `True`, `status`
`Status.Error`, `retcode` `-1` (the command
never finished, so it has no exit code of its own) and `exit_code`
`255`; `otto host <id> exec` exits 255.
Its `value` begins with the timeout line (for a 5-second budget,
`Command timed out after 5.0s`); the rest of its shape is in
{ref}`Timed out <timed-out-command-result>`. `retcode` `-1` also means
other things, so `timed_out` is the field that tells a timeout apart
([The Result family](../architecture/utilities/results.md#the-result-family)).

On a Zephyr board a command's `retcode` is the firmware's own return value,
in negative-errno style (`-8` for an unknown command), not a POSIX exit
status, and a failed command's `exit_code` is that same negative value
([Exit codes](../architecture/utilities/results.md#exit-codes) says the one
value it cannot carry).

A command that fails has `status` `Status.Failed`, and its `exit_code`
is its `retcode`. One killed by signal N fails the same way, with `retcode`
128+N: `kill -TERM` reads `143`, and `exit_code`
`143`
({ref}`A command killed, timed out or interrupted <command-ends-abnormally>`).

(command-contracts-narrower)=

## Where a device gets a narrower promise

On these devices the promise a test can check is narrower than a command's
page says, and only the narrower one is promised there, for every command:

- On a console whose host record sets `console_options.escapes_iac: false`, `exec-output-exact` does not cover a 0xFF byte in a command's output.
  - Otherwise you can read a command's output exactly as the command wrote it -- blank lines, spaces, tabs and carriage returns intact -- with only the one final newline removed.
  - What is lost: {ref}`A 0xFF byte in a command's output <exception-a-0xff-byte-in-a-command-s-output-on-a-console-whose-host-record-sets-console-options-escapes-iac-false>`.
- On a Zephyr board, `exec-output-exact` holds in a narrower form.
  - You can read a command's output exactly as the device's firmware wrote it, with only the one final newline and the colour codes the shell puts around its error and warning lines removed (checked against recordings of the firmware's own commands, because the Zephyr shell has no command that prints text you choose).
  - The colour codes removed are exactly `\x1b[1;31m`, `\x1b[1;33m` and `\x1b[m`, wherever they occur in the output, so a command that writes one of those sequences itself loses it.

(command-contracts-not-covered)=

## What is not covered at all

No host the conformance suite reaches has a **proxied** login, so the
route a proxied login takes is covered by unit tests only, and none of
the promises on these pages is checked through it.

A docker container's `exec` is designed to follow its parent host's route
({ref}`What a command's value holds <command-output-contract>`), but no
host the conformance suite reaches is a container, so nothing on these
pages is promised for one.

## Declared exceptions

Where a device's firmware or shell, or a library otto is built on, makes a
promise impossible, otto declares the exception in its code, next to what
it applies to. Each is described once, here; a command's page links the ones
that apply to it.

| Exception | In short |
| --- | --- |
| {ref}`How long one command can be, on the ssh-channel route <exception-how-long-one-command-can-be-on-the-ssh-channel-route>` | 9000 characters ran whole on dropbear 2022.83; other ssh servers were not measured |
| {ref}`How long one line of a command can be, on the pooled-shell and default-session routes <exception-how-long-one-line-of-a-command-can-be-on-the-pooled-shell-and-default-session-routes>` | in a shell session (the `pooled-shell` and `default-session` routes), 4095 characters per line; 1022 on a BusyBox shell whose terminal still echoes |
| {ref}`A 0xFF byte in a command's output, on a console whose host record sets console_options.escapes_iac: false <exception-a-0xff-byte-in-a-command-s-output-on-a-console-whose-host-record-sets-console-options-escapes-iac-false>` | a 0xFF byte the command writes is lost, and sometimes the bytes after it |

Each section below gives the exception's **kind**: device: the device cannot do it; library: a library otto builds on behaves this way.

(exception-how-long-one-command-can-be-on-the-ssh-channel-route)=

### How long one command can be, on the `ssh-channel` route

Where: the `ssh-channel` route. Kind: device: the device cannot do it.

On this route the command travels as the ssh exec request itself, and the ssh server decides how long a request it accepts. On dropbear 2022.83, the one server measured, a command of 9000 characters ran whole and one of 9001 failed; other servers and versions were not measured.

Measured (the lab detail, for the evidence):

- 2026-08-13, BusyBox 1.35.0 over dropbear 2022.83: a command on its own exec channel arrives intact at 9000 characters and fails at 9001

(exception-how-long-one-line-of-a-command-can-be-on-the-pooled-shell-and-default-session-routes)=

### How long one line of a command can be, on the `pooled-shell` and `default-session` routes

Where: the `pooled-shell` and `default-session` routes. Kind: device: the device cannot do it.

On these routes otto types the command into a terminal, and the characters otto adds around the command count against the line's length. A Linux terminal cuts a typed line past 4095 characters, with no error: the kernel holds a typed line in a 4096-byte buffer, its newline included (measured on BusyBox guests). BusyBox's shell, ash, cuts a line at 1022 characters, but only while the terminal echoes what is typed back. otto turns echo off when it opens a session, so the limit is 4095 where that worked and 1022 where it did not (the device has no `stty`, or its `stty` refuses). otto also uses 1022 when it sizes a file transfer's lines before it has opened any session to the host, since it cannot yet know whether echo is off. `otto host <id> probe` prints the terminal mapping it measured, with `, echo on` after it where echo stayed on, so the limit there is 1022 on a BusyBox shell.

On an ash shell `run` counts each line before typing it, and refuses a line over the limit with an error naming it. Elsewhere the terminal cuts it without an error.

Not limited this way: a local host's session, which reads the command from a pipe, not a terminal; and `exec` on the `ssh-channel` route, which has its own limit. A Zephyr board's own shell buffer is a separate limit, never measured.

Measured (the lab detail, for the evidence):

- 2026-08-13, BusyBox 1.35.0 over dropbear 2022.83, OpenSSH and a local pty, typed by a raw harness into a shell whose terminal still echoed: 1022 characters arrive intact; at 1023 the line is cut, with no error
- 2026-08-13, BusyBox 1.16.1, 1.21.1, 1.28.1, 1.31.0 and 1.35.0 through a local pty, the same harness: the same 1022/1023 boundary on every release
- 2026-11-13, the five BusyBox QEMU guests over telnet, and BusyBox 1.35.0 over dropbear ssh and its serial console, through otto's own session after its handshake (echo off): 4095 characters arrive intact; at 4096 the line is cut to 4095, with no error
- 2026-11-13, the same sessions after `stty echo`: cut at 1022 on every release and route, the 2026-08-13 boundary
- 2026-11-13, BusyBox 1.35.0 over telnet, dropbear ssh and its serial console, through otto's session under the bed's normal (bash) frame after a hook turned echo back on and made `stty` fail: the handshake read `onlcr, echo on`, and a raw line of 1023 characters was cut to 1022; GNU bash on test1 over ssh, the same way, cut nothing
- 2026-11-13, the same three routes with the `ash` frame declared and the same hook: the line budget (the 1022-character limit less the characters otto adds around the command) read 984 and a 1023-character `run` was refused naming 1022

(exception-a-0xff-byte-in-a-command-s-output-on-a-console-whose-host-record-sets-console-options-escapes-iac-false)=

### A 0xFF byte in a command's output, on a console whose host record sets `console_options.escapes_iac: false`

Where: a console whose host record sets `console_options.escapes_iac: false`. Kind: device: the device cannot do it.

In the telnet protocol a 0xFF byte starts a command to the other end, so a server must send a 0xFF the command wrote as two. This console's server sends it as one, so otto reads it as the start of a telnet command and loses it, with some of the bytes after it:

- before an ordinary byte, only the 0xFF is lost;
- two 0xFF arrive as one;
- before a byte from 0xF1 to 0xF9, that byte is lost too;
- before a byte from 0xFB to 0xFE, that byte and the next are lost;
- before 0xFA, all output after it, the prompt included, is lost until a 0xFF 0xF0 arrives, so the command can seem to hang until its timeout.

When the output must be byte-exact, reach the host over ssh or telnet where it has one, or serve its console through a telnet server that doubles 0xFF.

Measured (the lab detail, for the evidence):

- 2026-11-13, BusyBox 1.35.0 guest bb1350, its ttyS1 bridged by QEMU's `-serial telnet:` on test1:2450: printf 'SA\377BE' arrives on the wire as 53 41 ff 42 45 (not ff ff), which telnetlib3 5.0.1 reads as 'SABE'; \377\377 arrives as ff ff and reads as one 0xFF; `run` and `exec` of printf '\377\n' answer ''
- 2026-11-13, same console: 0x00, 0x0D, 0xFB, 0xFD and 0xFE each arrive intact between two ASCII bytes
- 2026-11-13, replayed through telnetlib3 5.0.1 and otto's reader behind the captured QEMU greeting (no wire capture): after 'SA', ff 42 reads 'B', ff 00 reads 0x00, ff f0 reads 0xF0, ff f1 to ff f9 lose both bytes, ff fb to ff fe lose those and the option byte after them, ff fa 01 42 43 44 ff f0 45 reads only 'E', and ff fa with no ff f0 after it reads nothing more at all

(command-contracts-undefined)=

## Undefined: never measured, so not promised

These are not declared exceptions, because no device has been measured
producing them:

- A terminal that changes line endings in a way otto cannot undo exactly.
  The session reports its line-ending mapping as `unknown(...)`
  (`otto host <id> probe` prints it), returns the output as it arrived, and
  logs a warning once. What a command's output looks like there is not
  promised.
- A tab a command writes, on a terminal that turns tabs into spaces, where
  otto could not switch that off (with `stty tab0`; the device has no `stty`,
  or its `stty` refuses). The session reports its mapping with
  `, tabs expanded` after it, logs a warning once, and the tab arrives as
  spaces.
- Output after a command changes its own terminal settings on a shell
  session, whether `run` or a shell-routed `exec` reached it: for example
  `stty -onlcr`, which stops the terminal turning a newline into `\r\n`, or
  a program that leaves the terminal in raw mode, passing every byte through
  unchanged. The session measured its terminal when it opened and does not
  measure again until a new session opens
  ({ref}`What a command's value holds <command-output-contract>`).
- On a Zephyr board, output that no recording of a firmware command shows (`tests/unit/host/shell_transcripts/README.md`): a `\r` the program itself writes; output that does not end with a newline; empty output on Zephyr 3.7 and 4.4 (on Zephyr 2.7 a command with no output was recorded, and it arrives as nothing); spaces at the end of a line (spaces at the start of a line, as `help` writes, are recorded and kept); an info-level coloured line; plain output followed by an error line; a `\r\n` the command writes itself (seen once, not recorded: `log status` arrives as `\r\r\n`). Exact output is promised there only for the firmware's own commands, as above.
- How long a line the Zephyr shell itself accepts. Its command buffer is a
  firmware build setting (`CONFIG_SHELL_CMD_BUFF_SIZE`) and has never been
  measured. Where otto sizes its own lines on a Zephyr board it uses the
  Linux terminal's limit ({ref}`How long one line of a command can be, on the pooled-shell and default-session routes <exception-how-long-one-line-of-a-command-can-be-on-the-pooled-shell-and-default-session-routes>`), which is not a
  measurement of any Zephyr board.

## For contributors: which test checks each promise

By promise id:

- `exec-exit-code`: `tests/conformance/test_exec_contract.py::test_exec_reports_the_documented_exit_code`
- `exec-framing`: `tests/conformance/test_exec_contract.py::test_exec_frames_output_without_prompt_noise`
- `exec-output-exact`: `tests/conformance/test_output_contract.py::test_a_commands_value_is_its_exact_output`
- `exec-failure-in-sequence`: `tests/conformance/test_exec_contract.py::test_a_failing_command_is_not_reported_as_success`
- `exec-signal-exit`: `tests/conformance/test_signal_contract.py::test_a_command_killed_by_a_signal_reports_128_plus_n`
- `timeout`: `tests/conformance/test_timeout_contract.py::test_a_command_exceeding_its_budget_fails_the_documented_way`

The suite checks shell-routed `exec` (the `exec-shell` entry) on an SSH host
whose `exec` would otherwise get a channel of its own, by declaring a
session-setup hook that does nothing: declaring any hook sends `exec` to a
shell session.
