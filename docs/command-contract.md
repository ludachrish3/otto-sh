<!-- GENERATED FILE -- do not edit by hand.
     scripts/render_support_matrix.py renders this from the `entries` marker on
     each command contract and the limitations declared on otto's built-in
     command frames and exec routes, on every Sphinx build (docs/conf.py,
     builder-inited). Edits are overwritten. -->

(command-contract)=

# Command contract: what a command's result means

This page is for anyone who runs a command on a host, from the CLI
(`otto host <id> exec`) or from Python
({meth}`Host.run <otto.host.host.Host.run>`,
{meth}`Host.exec <otto.host.host.Host.exec>`).
It says what otto promises about the result of a command -- its exit code,
its output, its timeout -- on each kind of host it is checked on, through
each way of issuing it that is checked there. Where a promise is not
checked, it is not promised, and the page says so. Where a device's
firmware or shell, or a library otto is built on (such as telnetlib3),
makes a promise impossible, the page lists the
[declared exception](#declared-exceptions) with its measurements.

An **entry** is the way a command is issued: `run` (the host's persistent
session), `exec` (a one-off command), or `exec-shell` (`exec` on an SSH
host sent through a shell session instead of a channel of its own, for one
of the reasons the next section lists).
A **route** is what carried the command; the next section says which one
your command takes. A host's **term** is how otto reaches it: `ssh`,
`telnet` or `console`. Its **lab entry** is the host's entry in the lab's
host file ({doc}`Host options <configuration/host-options>`).

Every field this page talks about is on the
{class}`~otto.result.CommandResult` a call hands back, for example
(the result built here stands for what `await host.exec(...)` returns):

```{doctest}
>>> from otto.result import CommandResult
>>> from otto.utils import Status
>>> result = CommandResult(
...     status=Status.Success, retcode=0, value="hello", route="ssh-channel"
... )
>>> result.route, result.timed_out, result.value
('ssh-channel', False, 'hello')
```

## Which route your command takes

otto picks the route for each call from the host's configuration and the
call's arguments:

- `run` always uses the host's one persistent shell session, the
  `default-session` route.
- `exec` on an SSH host gets a bare SSH channel of its own, the `ssh-channel`
  route, unless one of the cases below applies.
- `exec` goes through a shell session instead when:
  - the host's term is `console`. A console serves one client, so `exec`
    uses the same session as `run` (`default-session`);
  - the host's term is `telnet` (`pooled-shell`, a shell session kept for
    `exec` beside the one `run` uses);
  - the login is **proxied**: the account otto runs as is reached by logging
    in as another account first and then becoming it, through the login
    proxy named in its cred (the credentials entry otto logs in with)
    ([Credentials and login proxies](configuration/host-sources.md#credentials-and-login-proxies))
    (`pooled-shell`);
  - the host declares a **session-setup hook**: project code that runs on
    every shell session otto opens, before the session is used
    ({ref}`Session setup <per-host-session-setup>`) (`pooled-shell`);
  - the call needs a shell: it passes `expects`, `sudo=True`, or a `user`
    otto cannot log in as directly over SSH (`pooled-shell`).
- On an SSH host, `exec(user=...)` for a user that has SSH credentials of its
  own, with no `expects` and no `sudo`, logs in as that user on an SSH
  connection of its own (`ssh-channel`), even when the host's login is
  proxied or it declares a session-setup hook.
- On an embedded host, such as a Zephyr board, `run` and `exec` both use
  its one console session (`default-session`).
- On the local host, `exec` runs a fresh local process (`local-subprocess`).
- A docker container's `exec` runs `docker exec` through its parent host.

Every result carries the answer in its `route` field
([Which route ran a command](architecture/utilities/results.md#which-route-ran-a-command)):
from Python, read `result.route`. `otto host <id> exec` does not print it.

## What otto promises, and where it is checked

Each promise below has a test, and the conformance suite runs it, once
through each entry the table lists, on these kinds of host:

- a GNU/Linux machine
- a BusyBox device
- a Zephyr board
- the local machine
- the local machine logged in to over SSH
- BusyBox's applets run on the local machine

Whether a promise held, device by device, is in the
{doc}`support matrix <architecture/support-matrix>`, which says how the run
behind it covers the test bed.
A promise holds where it is checked, through the entries checked there,
unless a [declared exception](#declared-exceptions) says otherwise. Where it
is not checked, it is not promised: the **Not checked** column says where,
and its [notes](#where-a-promise-is-not-checked) say why. The **Promise id**
is the name the support matrix uses for the promise.

| You can | Checked through | Not checked | Promise id |
| --- | --- | --- | --- |
| run a command and trust the exit code otto hands back | `run`, `exec`, `exec-shell` | `exec` not on BusyBox's applets run on the local machine (note 1); `exec-shell` only on a GNU/Linux machine over `ssh`, a BusyBox device over `ssh` and the local machine logged in to over SSH (note 2) | `exec-exit-code` |
| read a command's output without otto's own prompts, echo and markers mixed into it | `run`, `exec`, `exec-shell` | `exec` not on BusyBox's applets run on the local machine (note 1); `exec-shell` only on a GNU/Linux machine over `ssh`, a BusyBox device over `ssh` and the local machine logged in to over SSH (note 2) | `exec-framing` |
| read a command's output exactly as the command wrote it -- blank lines, spaces, tabs and carriage returns intact -- with only the one final newline removed | `run`, `exec`, `exec-shell` | `exec` not on BusyBox's applets run on the local machine (note 1); `exec-shell` only on a GNU/Linux machine over `ssh`, a BusyBox device over `ssh` and the local machine logged in to over SSH (note 2) | `exec-output-exact` |
| trust that a command that failed part-way through a sequence is reported as a failure rather than as success | `run` | -- | `exec-failure-in-sequence` |
| read `retcode` 128+N for a command killed by signal N, as a shell reports it, and keep using the host afterwards | `run`, `exec` | not on a Zephyr board (note 3); `exec` not on BusyBox's applets run on the local machine (note 1); `exec` only on a GNU/Linux machine over `ssh`, a BusyBox device over `ssh`, the local machine and the local machine logged in to over SSH (note 4) | `exec-signal-exit` |
| give a command a time budget and have a breach reported the documented way, with the session still usable afterwards | `run`, `exec`, `exec-shell` | not on a Zephyr board (note 5); `exec` not on BusyBox's applets run on the local machine (note 1); `exec-shell` only on a GNU/Linux machine over `ssh`, a BusyBox device over `ssh` and the local machine logged in to over SSH (note 2) | `timeout` |

### Where a device gets a narrower promise

On these devices the promise a test can check is narrower than the
table says, and only the narrower one is promised there:

- On a console whose lab entry sets `console_options.escapes_iac: false` (`exec-output-exact`): you can read a command's output exactly as the command wrote it -- blank lines, spaces, tabs and carriage returns intact -- with only the one final newline removed, except for a 0xFF byte in a command's output: see {ref}`A 0xFF byte in a command's output <exception-a-0xff-byte-in-a-command-s-output-on-a-console-whose-lab-entry-sets-console-options-escapes-iac-false>`.
- On a Zephyr board (`exec-output-exact`): you can read a command's output exactly as the device's firmware wrote it, with only the one final newline and the colour codes the shell puts around its error and warning lines removed (checked against recordings of the firmware's own commands, because the Zephyr shell has no command that prints text you choose). The colour codes removed are exactly `\x1b[1;31m`, `\x1b[1;33m` and `\x1b[m`, wherever they occur in the output, so a command that writes one of those sequences itself loses it.

### What the output and timeout promises mean

What a command's output holds, line by line, is defined in
{ref}`What a command's value holds <command-output-contract>`.

The time budget is the call's `timeout`, which defaults to
30 seconds
({data}`~otto.host.host.DEFAULT_COMMAND_TIMEOUT`) for `run` and `exec` alike.
A command that outlives it does not raise. Its
{class}`~otto.result.CommandResult` has `timed_out` set to `True`, `status`
`Status.Error`, `retcode` `-1` (the command never finished, so it has no
exit code of its own), `exit_code` `255`, and a `value` that starts
`Command timed out after <timeout>s`; `otto host <id> exec` exits 255
({ref}`the host exit codes <host-exit-codes>`). `retcode` `-1` also means
other things, so `timed_out` is the field that tells a timeout apart
([The Result family](architecture/utilities/results.md#the-result-family)).

Of the devices otto reaches over a term, the timeout promise is checked on a GNU/Linux machine over `console`, `ssh` and `telnet` and a BusyBox device over `console`, `ssh` and `telnet`: a device reached over its console is checked too.

On a Zephyr board a command's `retcode` is the firmware's own return value,
in negative-errno style (`-8` for an unknown command), not a POSIX exit
status, and a failed command's `exit_code` is that same negative value
([Exit codes](architecture/utilities/results.md#exit-codes) says the one
value it cannot carry).

## Where a promise is not checked

1. `exec` is not checked on BusyBox's applets run on the local machine: there the applets are put on the `PATH` of the `run` session only, which a one-off `exec` process never sees.
2. `exec-shell` is checked only on a GNU/Linux machine over `ssh`, a BusyBox device over `ssh` and the local machine logged in to over SSH: only there does `exec` otherwise get an SSH channel of its own, which `exec-shell` sends through a shell session instead.
3. Not checked on a Zephyr board, so not promised there: the Zephyr shell runs each command on its own thread rather than as a process, so there is no command a signal can kill there.
4. `exec` is checked only on a GNU/Linux machine over `ssh`, a BusyBox device over `ssh`, the local machine and the local machine logged in to over SSH: on every other host `exec` types the command into otto's own shell session, and a killed command would take that session down with it, so otto does not check it there yet.
5. Not checked on a Zephyr board, so not promised there: otto does not test what happens when a Zephyr command runs longer than its timeout, so it promises nothing about it. The Zephyr shell runs one command at a time, on its own thread, so a command made to outlive its budget would block the very shell the test then has to find still answering.

No host the conformance suite reaches has a **proxied** login, so the
route a proxied login takes is covered by unit tests only, and none of
the promises above is checked through it.

A docker container's `exec` is designed to follow its parent host's route
({ref}`What a command's value holds <command-output-contract>`), but commands
run on the container route are not checked by these tests yet, so nothing on
this page is promised for them.

## Declared exceptions

Where a device's firmware or shell, or a library otto is built on, makes a
promise impossible, otto declares the exception in its code, next to what
it applies to. Each one is listed here
with the reason and the measurements behind it.

| Where | Promise affected | Kind | Why |
| --- | --- | --- | --- |
| The `ssh-channel` route | how long one command can be | device: the device cannot do it | On this route the command travels as the ssh exec request itself, and the ssh server decides how long a request it accepts. On dropbear 2022.83, the one server measured, a command of 9000 characters ran whole and one of 9001 failed; other servers and versions were not measured. |
| The `pooled-shell` and `default-session` routes | how long one line of a command can be | device: the device cannot do it | On these routes otto types the command into a terminal, and the characters otto adds around the command count against the line limit. Through a Linux terminal one line is cut past 4095 characters with no error: the kernel holds a typed line in a 4096-byte buffer, its newline included (measured on BusyBox guests). BusyBox ash's line editor cuts a line at 1022 characters, but only while the terminal echoes. otto turns echo off when it opens a session and records whether that took, so an ash shell's limit is 4095 where echo is off and 1022 where it is still on (no `stty`, or one that refuses it), and for a transfer planned before any session has opened. On an ash shell `run` counts each line before typing it and refuses one over that limit, with an error naming it. The measurements below show both. A Zephyr board's own shell buffer is a separate limit, not measured. A local host's session reads the command from a pipe, not a terminal, and is not limited this way. `exec` on the `ssh-channel` route, a channel of its own, is not limited this way either; its own limit is declared on that route. |
| A console whose lab entry sets `console_options.escapes_iac: false` | a 0xFF byte in a command's output | device: the device cannot do it | This console's server sends a 0xFF byte the command wrote without doubling it, so otto reads it as the start of a telnet command and loses it together with that command's bytes: followed by an ordinary byte only the 0xFF is lost, two 0xFF arrive as one, followed by 0xF1-0xF9 that byte is lost too, followed by 0xFB-0xFE that byte and the next, and followed by 0xFA all output after it, the prompt included, is swallowed until a 0xFF 0xF0 arrives, so the command can appear to hang until its timeout. When the output must be byte-exact, reach the host over ssh or telnet where it has one, or serve its console through a telnet server that doubles 0xFF. |

(exception-how-long-one-command-can-be-on-the-ssh-channel-route)=

### How long one command can be, on the `ssh-channel` route

Measured:

- 2026-08-13, BusyBox 1.35.0 over dropbear 2022.83: a command on its own exec channel arrives intact at 9000 characters and fails at 9001

(exception-how-long-one-line-of-a-command-can-be-on-the-pooled-shell-and-default-session-routes)=

### How long one line of a command can be, on the `pooled-shell` and `default-session` routes

Measured:

- 2026-08-13, BusyBox 1.35.0 over dropbear 2022.83, OpenSSH and a local pty, typed by a raw harness into a shell whose terminal still echoed: 1022 characters arrive intact; at 1023 the line is cut, with no error
- 2026-08-13, BusyBox 1.16.1, 1.21.1, 1.28.1, 1.31.0 and 1.35.0 through a local pty, the same harness: the same 1022/1023 boundary on every release
- 2026-11-13, the five BusyBox QEMU guests over telnet, and BusyBox 1.35.0 over dropbear ssh and its serial console, through otto's own session after its handshake (echo off): 4095 characters arrive intact; at 4096 the line is cut to 4095, with no error
- 2026-11-13, the same sessions after `stty echo`: cut at 1022 on every release and route, the 2026-08-13 boundary
- 2026-11-13, BusyBox 1.35.0 over telnet, dropbear ssh and its serial console, through otto's session under the bed's normal (bash) frame after a hook turned echo back on and made `stty` fail: the handshake read `onlcr, echo on`, and a raw line of 1023 characters was cut to 1022; GNU bash on test1 over ssh, the same way, cut nothing
- 2026-11-13, the same three routes with the `ash` frame declared and the same hook: the line budget read 984 and a 1023-character `run` was refused naming 1022

(exception-a-0xff-byte-in-a-command-s-output-on-a-console-whose-lab-entry-sets-console-options-escapes-iac-false)=

### A 0xFF byte in a command's output, on a console whose lab entry sets `console_options.escapes_iac: false`

Measured:

- 2026-11-13, BusyBox 1.35.0 guest bb1350, its ttyS1 bridged by QEMU's `-serial telnet:` on test1:2450: printf 'SA\377BE' arrives on the wire as 53 41 ff 42 45 (not ff ff), which telnetlib3 5.0.1 reads as 'SABE'; \377\377 arrives as ff ff and reads as one 0xFF; `run` and `exec` of printf '\377\n' answer ''
- 2026-11-13, same console: 0x00, 0x0D, 0xFB, 0xFD and 0xFE each arrive intact between two ASCII bytes
- 2026-11-13, replayed through telnetlib3 5.0.1 and otto's reader behind the captured QEMU greeting (no wire capture): after 'SA', ff 42 reads 'B', ff 00 reads 0x00, ff f0 reads 0xF0, ff f1 to ff f9 lose both bytes, ff fb to ff fe lose those and the option byte after them, ff fa 01 42 43 44 ff f0 45 reads only 'E', and ff fa with no ff f0 after it reads nothing more at all

## Undefined: never measured, so not promised

These are not declared exceptions, because no device has been measured
producing them:

- A terminal that maps line endings in a way otto cannot undo exactly. The
  session reports its mapping as `unknown(...)` (`otto host <id> probe` prints
  it), returns the output as it arrived, and logs a warning once. What a
  command's output looks like on such a terminal is not promised.
- A tab a command writes, on a terminal that expands tabs where `stty tab0`
  did not take (no `stty`, or one that refuses it). The session reports its
  mapping with `, tabs expanded` after it, logs a warning once, and the tab
  arrives as spaces.
- Output after a command changes its own terminal settings (`stty -onlcr`, or
  a program that leaves the terminal in raw mode) on a shell session, whether
  `run` or a shell-routed `exec` reached it. The session measured its terminal
  when it opened and does not measure again until a new session opens
  ({ref}`What a command's value holds <command-output-contract>`).
- On a Zephyr board, output that no recording of a firmware command shows (`tests/unit/host/shell_transcripts/README.md`): a `\r` the program itself writes; output that does not end with a newline; empty output on Zephyr 3.7 and 4.4 (on Zephyr 2.7 a command with no output was recorded, and it arrives as nothing); spaces at the end of a line (spaces at the start of a line, as `help` writes, are recorded and kept); an info-level coloured line; plain output followed by an error line; a `\r\n` the command writes itself (seen once, not recorded: `log status` arrives as `\r\r\n`). Exact output is promised there only for the firmware's own commands, as above.
- How long a line the Zephyr shell itself accepts. Its command buffer is a
  firmware build setting (`CONFIG_SHELL_CMD_BUFF_SIZE`) and has never been
  measured; the line-length measurements above are of BusyBox devices. otto
  refuses a long line only on an ash shell. Where otto sizes its own lines on
  a Zephyr board it uses 4095 characters, less what otto adds
  around the command: that is the Linux terminal's line bound, measured on
  BusyBox guests, not a measurement of any Zephyr board.

## For contributors: which test checks each promise

By promise id:

- `exec-exit-code`: `tests/conformance/test_exec_contract.py::test_exec_reports_the_documented_exit_code`
- `exec-framing`: `tests/conformance/test_exec_contract.py::test_exec_frames_output_without_prompt_noise`
- `exec-output-exact`: `tests/conformance/test_output_contract.py::test_a_commands_value_is_its_exact_output`
- `exec-failure-in-sequence`: `tests/conformance/test_exec_contract.py::test_a_failing_command_is_not_reported_as_success`
- `exec-signal-exit`: `tests/conformance/test_signal_contract.py::test_a_command_killed_by_a_signal_reports_128_plus_n`
- `timeout`: `tests/conformance/test_timeout_contract.py::test_a_command_exceeding_its_budget_fails_the_documented_way`
