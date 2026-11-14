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
| give a command a time budget and have a breach reported the documented way, with the session still usable afterwards | not on a Zephyr board (note 2) | `timeout` |

On some devices a promise above is narrower: see
{ref}`Where a device gets a narrower promise <command-contracts-narrower>`.

## Where a promise is not checked

1. Not checked on a Zephyr board, so not promised there: the Zephyr shell runs each command on its own thread rather than as a process, so there is no command a signal can kill there.
2. Not checked on a Zephyr board, so not promised there: otto does not test what happens when a Zephyr command runs longer than its timeout, so it promises nothing about it. The Zephyr shell runs one command at a time, on its own thread, so a command made to outlive its budget would block the very shell the test then has to find still answering.

## Declared exceptions and undefined behaviour

These declared exceptions apply to `run`; each is described on
{ref}`Command contracts <command-contracts>`:

- {ref}`How long one line of a command can be, on the pooled-shell and default-session routes <exception-how-long-one-line-of-a-command-can-be-on-the-pooled-shell-and-default-session-routes>`
- {ref}`A 0xFF byte in a command's output, on a console whose host record sets console_options.escapes_iac: false <exception-a-0xff-byte-in-a-command-s-output-on-a-console-whose-host-record-sets-console-options-escapes-iac-false>`

What is never measured, so not promised, for `run` as for every command:
{ref}`Undefined <command-contracts-undefined>`.
