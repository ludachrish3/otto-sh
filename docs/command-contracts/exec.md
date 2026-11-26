<!-- GENERATED FILE -- do not edit by hand.
     scripts/render_support_matrix.py (render_command_contracts) renders this
     from the `entries` marker on each command contract checked
     through `exec`.
     Regenerate with `uv run python -m scripts.render_support_matrix`;
     edits are overwritten. -->

(command-contract-exec)=

# `exec`: a one-off command

`exec` runs a one-off command. On most hosts it runs on a session of its
own and shares nothing with `run`. On a console it starts from what `run`
left and leaves nothing behind. On an embedded host it shares `run`'s
session too; a Zephyr shell, whose frame does not isolate, keeps what
it changes (not measured on a board)
({ref}`what an exec shares with run <exec-session>`,
{ref}`shell state <exec-frame-shell-state>`). From the
CLI it is `otto host <id> exec` ({doc}`/cli/host/exec`); from
Python, {meth}`Host.exec <otto.host.host.Host.exec>`.
The rules every command shares -- what a contract is, what its promises
mean, and how to read what is not promised -- are on
{ref}`Command contracts <command-contracts>`.

## Which session `exec` takes

- The host's one persistent shell session, the one `run` uses: when the host is reached over `console` (a console serves one client), or is an embedded host such as a Zephyr board, whatever the call needs; concurrent calls take turns on it. On a console a call with a `user` switches the session to that user for the call and back; an embedded host refuses `user`.
- A shell session otto keeps for `exec` beside the one `run` uses, with a terminal: when the host is reached over `telnet`; or over `ssh` where the call needs a terminal (it passes `expects` (patterns otto answers while the command runs, such as a password prompt), `sudo=True`, or `terminal=True`), runs as a `user` otto cannot log in as directly, the host's login is **proxied** (otto logs in as one account and then switches to the one it runs as; [Credentials and login proxies](../configuration/host-sources.md#credentials-and-login-proxies)), or the host declares a **session-setup hook** (project code otto runs on every shell session it opens; {ref}`Session setup <per-host-session-setup>`); a call with a `user` runs in a session opened as that user, over their own SSH connection where they have SSH credentials of their own. No host the suite tests has a proxied login ({ref}`What is not covered at all <command-contracts-not-covered>`).
- A shell session otto keeps for `exec` beside the one `run` uses, without a terminal: when the host is reached over `ssh` and none of the cases above applies, including `exec(user=...)` for a user with SSH credentials of their own, over an SSH connection as that user; or the host is the local host, whose sessions read a pipe (each call opens one of its own and closes it after).
- `docker exec` through the container's parent host, as one of the parent's own `exec` calls: when the host is a docker container.

## What `exec` promises

Each promise below is checked through `exec` on every kind of host the
conformance suite reaches ({ref}`the list <command-contracts-checked>`), except
where its **Not checked** column says otherwise; the numbered notes below say why.

| With `exec` you can | Not checked | {ref}`Promise id <command-contracts-promise-id>` |
| --- | --- | --- |
| run a command and trust the exit code otto hands back | not on BusyBox's applets run on the local machine (note 1) | `exec-exit-code` |
| read a command's output without otto's own prompts, echo and markers mixed into it | not on BusyBox's applets run on the local machine (note 1) | `exec-framing` |
| read a command's output exactly as the command wrote it -- blank lines, spaces, tabs and carriage returns intact -- with only the one final newline removed | not on BusyBox's applets run on the local machine (note 1) | `exec-output-exact` |
| read `retcode` 128+N for a command killed by signal N, as a shell reports it, and keep using the host afterwards | not on BusyBox's applets run on the local machine (note 1); not on a Zephyr board (note 2) | `exec-signal-exit` |
| run an `exec` that starts from the state its session was set up with (a console's narrower promise is linked below the table), and trust that its `cd`, variables, options, `exit` and, where it runs on a terminal, its terminal settings reach no later `exec` or `run` | not on BusyBox's applets run on the local machine (note 1); not on a Zephyr board (note 3) | `exec-isolated` |
| run an `exec` without `expects=` and have its command read end of file on stdin, rather than wait on a terminal nobody types into | not on BusyBox's applets run on the local machine (note 1); not on a Zephyr board (note 4) | `exec-stdin` |
| gather several `exec` calls on one host and have each return its own command's output, never another call's | not on BusyBox's applets run on the local machine (note 1) | `exec-concurrent` |
| cancel an `exec` and have otto stop its command before the cancellation reaches you (on the local machine: its process group terminated, and what is left in it after the grace killed, as {ref}`What the stop reaches <exec-stop-reach>` says), with the host answering the next command | not on BusyBox's applets run on the local machine (note 1); not on a Zephyr board, a BusyBox device over `console` and a GNU/Linux machine over `console` (note 5) | `exec-cancel` |
| read the host log and find the lines of each command's `value`, one record per line, blank lines and a program's own `\r` kept, under `log=NORMAL` and `log=QUIET`, and nothing under `log=NEVER` | not on BusyBox's applets run on the local machine (note 1) | `exec-log` |
| run a command under a dry run and have it reach no device, getting back a `NotRun` result with `retcode == -1`, `command` the line a live call would type, and a `value` that raises `CommandNotRunError` | not on BusyBox's applets run on the local machine (note 1); not on a Zephyr board (note 6) | `exec-dry-run` |
| pass `expects=`, `sudo=` or `user=` to `run` or `exec`, alone or together, on a `ShellCommand` or inside `as_user()`, and get the same effect on every entry; where the device cannot honour an option, the call raises `UnsupportedOptionError` naming it, and nothing is typed | not on BusyBox's applets run on the local machine (note 1); `expects=` on a Zephyr board (note 7); `sudo=` with `user=` on a device (note 8); `user=` and `as_user()` without a second user (note 9) | `exec-options` |
| type a command holding punctuation, a TAB, UTF-8 text or an unquoted `!word`, or one ending in a `#` comment, and have the shell receive it byte for byte, the comment read as a comment; one holding a control character a terminal would act on reaches a session without a terminal intact, and on a session with one is refused with `UntypableCommandError` before anything is typed | not on BusyBox's applets run on the local machine (note 1); not on a Zephyr board (note 10) | `typed-input` |
| give a command a time budget and have a breach reported the same way on every host, with the process otto started stopped, the call returned once the budget and otto's bounded cleanup have passed, even when the command closed its output, and the session usable afterwards | not on BusyBox's applets run on the local machine (note 1); not on a Zephyr board (note 11) | `timeout` |

On some devices a promise above is narrower: see
{ref}`Where a device gets a narrower promise <command-contracts-narrower>`.

On some devices a promise above has a known gap: see
{ref}`Known gaps <command-contract-exec-known-gaps>`.

## Where a promise is not checked

1. Not checked on BusyBox's applets run on the local machine: there the applets are put on the `PATH` of the `run` session only, which a one-off `exec` session never sees.
2. Not checked on a Zephyr board, so not promised there: the Zephyr shell runs each command on its own thread rather than as a process, so there is no command a signal can kill there.
3. Not checked on a Zephyr board, so not promised there: otto has not measured whether one `exec` on a Zephyr board can change the shell state the next one sees, so this is not checked there.
4. Not checked on a Zephyr board, so not promised there: the Zephyr shell runs each command on its own thread rather than as a process with a stdin, so there is nothing to read there.
5. Not checked on a Zephyr board, a BusyBox device over `console` and a GNU/Linux machine over `console`, so not promised there: otto checks a cancelled `exec` on a Zephyr board and on a console with unit tests rather than on a device. The bed firmware has no command that blocks (issue #1066), and a Zephyr guest is never driven to a cancellation (issue #260); and a console takes one client, so otto cannot watch the command from a second connection while it cancels it.
6. Not checked on a Zephyr board, so not promised there: the Zephyr shell has no stock command that leaves an effect a later command can read back, so nothing on the device could show whether a command ran; a unit test checks that a dry run writes nothing to an open Zephyr session.
7. On a Zephyr board `expects=` is honoured, but its shell has no command that prompts and reads a line, so unit tests check it, not a device.
8. `sudo=` together with `user=` runs on no device: no login on the test bed may do it, so only a stand-in `sudo` on the local machine checks it.
9. `user=` and `as_user()` are checked only on hosts whose login has a second user to switch to.
10. Not checked on a Zephyr board, so not promised there: no command of the Zephyr shell reads its own argument back as data, and its line editor takes a TAB as completion.
11. Not checked on a Zephyr board, so not promised there: otto does not test what happens when a Zephyr command runs longer than its timeout, so it promises nothing about it. The Zephyr shell runs one command at a time, on its own thread, so a command made to outlive its budget would block the very shell the test then has to find still answering; and the bed firmware has no command that blocks for a chosen time (issue #1066).

## Declared exceptions and undefined behaviour

These declared exceptions apply to `exec`; each is described on
{ref}`Command contracts <command-contracts>`:

- {ref}`How long one line of a command can be, on a session with a terminal <exception-how-long-one-line-of-a-command-can-be-on-a-session-with-a-terminal>`
- {ref}`A 0xFF byte in a command's output, on a console whose host record sets console_options.escapes_iac: false <exception-a-0xff-byte-in-a-command-s-output-on-a-console-whose-host-record-sets-console-options-escapes-iac-false>`
- {ref}`Elevating a command with sudo= on run and exec, on the embedded and zephyr host families <exception-elevating-a-command-with-sudo-on-run-and-exec-on-the-embedded-and-zephyr-host-families>`
- {ref}`Running a command as another user= on run and exec, on the embedded and zephyr host families <exception-running-a-command-as-another-user-on-run-and-exec-on-the-embedded-and-zephyr-host-families>`

What is never measured, so not promised, for `exec` as for every command:
{ref}`Undefined <command-contracts-undefined>`.

(command-contract-exec-known-gaps)=
### Known gaps (not device limitations)

- `typed-input`: On a BusyBox serial console, a literal TAB in a command sent with `run()` or `exec()` reaches the shell with an extra 0x16 byte (Ctrl-V) before it ([`typed-tab-literal-next`](../architecture/subsystems/busybox-support.md#typed-tab-literal-next)).

(command-contract-exec-host-forms)=
### Per-host forms

- An `exec(sudo=True)` sent through a shell session on a host that elevates with sudo types one of two forms, set per host by `userland_options.sudo_form` ({ref}`sudo-form`). Unset, otto asks the host's sudo `sudo -l "$SHELL"` once per login, on that login's first such `exec`: `shell` on a yes, `validate` when sudo says not allowed and then holds a timestamp (`sudo -n -v`); a refusal after the password with no timestamp fails that `exec` without a second password; any other outcome is unread, uses `shell` and is asked again. One login's answer is never used for another. `run(sudo=True)` does not read it and types `sudo -S <cmd>` (issue #1086).
- `shell` (the default): one sudo call, `sudo -S "$SHELL" -c '<cmd>'`, so the whole command string runs as root, in a new shell of the login's own kind (not the pooled shell: its functions, aliases and unexported variables are not seen). It needs the host's sudoers to allow the login's shell; where it does not, the `exec` returns sudo's refusal (status 1) and nothing runs: set `sudo_form` to `validate` (`tests/unit/host/test_exec_stdin.py`).
- `validate`: `sudo -S -v`, then `sudo -n <cmd>`, so only the command's first simple command runs as root. It needs the host's sudo to keep its timestamp between the two calls; on a sudo with `timestamp_timeout=0` the password is accepted and the command is then refused (`sudo: a password is required`, status 1) and does not run: set `sudo_form` to `shell` (`tests/unit/host/test_exec_stdin.py`).
