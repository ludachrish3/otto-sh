<!-- GENERATED FILE -- do not edit by hand.
     scripts/render_support_matrix.py (render_command_contracts) renders this
     from the `entries` marker on each command contract checked
     through `run`.
     Regenerate with `uv run python -m scripts.render_support_matrix`;
     edits are overwritten. -->

(command-contract-run)=

# `run`: a command in the host's session

`run` sends a command to the host's one persistent shell session, so what
one command changes there (the working directory, a variable) is still
there for the next. You call it from Python, as
{meth}`Host.run <otto.host.host.Host.run>`; the CLI has no `run`.
The rules every command shares -- what a contract is, what its promises
mean, and how to read what is not promised -- are on
{ref}`Command contracts <command-contracts>`.

## Which route `run` takes

`run` always uses the host's one persistent shell session, the
`default-session` route, whatever the host's term. On an embedded host,
such as a Zephyr board, that session is the board's one console session.
So `result.route` is `default-session` for every `run`.

## What `run` promises

Each promise below is checked through `run` on every kind of host the
conformance suite reaches ({ref}`the list <command-contracts-checked>`), except
where its **Not checked** column says otherwise; the numbered notes below say why.

| With `run` you can | Not checked | {ref}`Promise id <command-contracts-promise-id>` |
| --- | --- | --- |
| run a command and trust the exit code otto hands back | -- | `exec-exit-code` |
| read a command's output without otto's own prompts, echo and markers mixed into it | -- | `exec-framing` |
| read a command's output exactly as the command wrote it -- blank lines, spaces, tabs and carriage returns intact -- with only the one final newline removed | -- | `exec-output-exact` |
| trust that a command that failed part-way through a sequence is reported as a failure rather than as success | -- | `exec-failure-in-sequence` |
| read `retcode` 128+N for a command killed by signal N, as a shell reports it, and keep using the host afterwards | not on a Zephyr board (note 1) | `exec-signal-exit` |
| `cd`, `export` or `set` in one `run` and have the next `run` on that host start there | not on a Zephyr board (note 2) | `run-keeps-state` |
| read the host log and find the lines of each command's `value`, one record per line, blank lines and a program's own `\r` kept, under `log=NORMAL` and `log=QUIET`, and nothing under `log=NEVER` | -- | `exec-log` |
| run a command under a dry run and have it reach no device, getting back a `NotRun` result with `retcode == -1`, no route, `command` the line a live call would type, and a `value` that raises `CommandNotRunError` | not on a Zephyr board (note 3) | `exec-dry-run` |
| give a command a time budget and have a breach reported the same way on every route, with the process otto started stopped, the call returned once the budget and otto's bounded cleanup have passed, even when the command closed its output, and the session usable afterwards | not on a Zephyr board (note 4) | `timeout` |

On some devices a promise above is narrower: see
{ref}`Where a device gets a narrower promise <command-contracts-narrower>`.

## Where a promise is not checked

1. Not checked on a Zephyr board, so not promised there: the Zephyr shell runs each command on its own thread rather than as a process, so there is no command a signal can kill there.
2. Not checked on a Zephyr board, so not promised there: otto has not measured how the Zephyr shell's state carries from one `run` to the next, so this is not checked there.
3. Not checked on a Zephyr board, so not promised there: the Zephyr shell has no stock command that leaves an effect a later command can read back, so nothing on the device could show whether a command ran; a unit test checks that a dry run writes nothing to an open Zephyr session.
4. Not checked on a Zephyr board, so not promised there: otto does not test what happens when a Zephyr command runs longer than its timeout, so it promises nothing about it. The Zephyr shell runs one command at a time, on its own thread, so a command made to outlive its budget would block the very shell the test then has to find still answering; and the bed firmware has no command that blocks for a chosen time (issue #1066).

## Declared exceptions and undefined behaviour

These declared exceptions apply to `run`; each is described on
{ref}`Command contracts <command-contracts>`:

- {ref}`How long one line of a command can be, on the pooled-shell and default-session routes <exception-how-long-one-line-of-a-command-can-be-on-the-pooled-shell-and-default-session-routes>`
- {ref}`A 0xFF byte in a command's output, on a console whose host record sets console_options.escapes_iac: false <exception-a-0xff-byte-in-a-command-s-output-on-a-console-whose-host-record-sets-console-options-escapes-iac-false>`

What is never measured, so not promised, for `run` as for every command:
{ref}`Undefined <command-contracts-undefined>`.

(command-contract-run-known-gaps)=
### Known gaps (not device limitations)

- On the local machine, a timed-out `run` of a command inside a subshell, or of a list or loop whose remainder runs long, can end the session: it is marked unusable, and its working directory and variables are lost; a lone command or pipeline is stopped and the session kept. Closing that session sends TERM to its process group: a process that ignores TERM can outlive a shell that exits promptly, and a process that left the group is not signalled. When a given-up session is closed is on {ref}`Closing a session <closing-a-session>` (issue #1101).
