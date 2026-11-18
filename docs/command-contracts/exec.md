<!-- GENERATED FILE -- do not edit by hand.
     scripts/render_support_matrix.py (render_command_contracts) renders this
     from the `entries` marker on each command contract checked
     through `exec`.
     Regenerate with `uv run python -m scripts.render_support_matrix`;
     edits are overwritten. -->

(command-contract-exec)=

# `exec`: a one-off command

`exec` runs a one-off command: nothing one call changes is carried to the
next. On a console it starts in a clean login shell, not from what `run`
left in the console's shell
({ref}`shell state, by route <exec-frame-shell-state>`). From the
CLI it is `otto host <id> exec` ({doc}`/cli/host/exec`); from
Python, {meth}`Host.exec <otto.host.host.Host.exec>`.
The rules every command shares -- what a contract is, what its promises
mean, and how to read what is not promised -- are on
{ref}`Command contracts <command-contracts>`.

## Which route `exec` takes

On a remote Unix host (one otto reaches over `ssh`, `telnet` or `console`):

- Reached over `ssh`, `exec` gets a bare SSH channel of its own, unless a case below applies (`ssh-channel`).
- Reached over `telnet`, it goes through a shell session (`pooled-shell`).
- The host's login is **proxied** (otto logs in as one account and then switches to the one it runs as; [Credentials and login proxies](../configuration/host-sources.md#credentials-and-login-proxies)): a shell session (`pooled-shell`). No host the suite tests has a proxied login ({ref}`What is not covered at all <command-contracts-not-covered>`).
- The host declares a **session-setup hook** (project code otto runs on every shell session it opens; {ref}`Session setup <per-host-session-setup>`): a shell session (`pooled-shell`).
- The call needs a shell: it passes `expects` (patterns otto answers while the command runs, such as a password prompt), `sudo=True`, or a `user` otto cannot log in as directly; a call with a `user` runs in a shell session opened as that user (`pooled-shell`).
- `exec(user=...)` over `ssh` for a user with SSH credentials of its own, and no `expects` or `sudo`: an SSH connection as that user (`ssh-channel`).
- The same call where the login is proxied or the host declares a session-setup hook: a shell session opened over that user's own SSH connection and set up as them (`pooled-shell`).
- Reached over `console`, it uses the same session as `run`: a console serves one client (`default-session`).

On other hosts:

- On an embedded host, such as a Zephyr board, its one console session, as `run` (`default-session`).
- On the local host, a fresh local process (`local-subprocess`).
- In a docker container, `docker exec` through its parent host (`container/<the parent's route>`).

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
| cancel an `exec` and have otto stop its command before the cancellation reaches you (on the local machine: its leader ended and its process group hung up, as the known gaps below say), with the host answering the next command | not on BusyBox's applets run on the local machine (note 1); not on a Zephyr board, a BusyBox device over `console` and a GNU/Linux machine over `console` (note 5) | `exec-cancel` |
| read the host log and find the lines of each command's `value`, one record per line, blank lines and a program's own `\r` kept, under `log=NORMAL` and `log=QUIET`, and nothing under `log=NEVER` | not on BusyBox's applets run on the local machine (note 1) | `exec-log` |
| run a command under a dry run and have it reach no device, getting back a `NotRun` result with `retcode == -1`, no route, `command` the line a live call would type, and a `value` that raises `CommandNotRunError` | not on BusyBox's applets run on the local machine (note 1); not on a Zephyr board (note 6) | `exec-dry-run` |
| pass `expects=`, `sudo=` or `user=` to `run` or `exec`, alone or together, on a `ShellCommand` or inside `as_user()`, and get the same effect on every entry; where the device cannot honour an option, the call raises `UnsupportedOptionError` naming it, and nothing is typed | not on BusyBox's applets run on the local machine (note 1); `expects=` on a Zephyr board (note 7); `sudo=` with `user=` on a device (note 8); `user=` and `as_user()` without a second user (note 9) | `exec-options` |
| give a command a time budget and have a breach reported the same way on every route, with the process otto started stopped, the call returned once the budget and otto's bounded cleanup have passed, even when the command closed its output, and the session usable afterwards | not on BusyBox's applets run on the local machine (note 1); not on a Zephyr board (note 10) | `timeout` |

On some devices a promise above is narrower: see
{ref}`Where a device gets a narrower promise <command-contracts-narrower>`.

## Shell-routed `exec`

This section is about one case only: an `exec` on a host reached over
`ssh` that the cases above send through a shell session (`pooled-shell`)
instead of its own channel. The conformance suite checks that path as an
entry of its own, `exec-shell`. A host reached over `telnet` or `console`
always sends `exec` through a shell session: `pooled-shell` over `telnet`,
`default-session` (the session `run` uses) over `console`. Their `exec`
promises are listed and checked in the main `exec` table above, and the
notes on the table below do not apply to them.

| With `exec` sent through a shell session you can | Not checked | {ref}`Promise id <command-contracts-promise-id>` |
| --- | --- | --- |
| run a command and trust the exit code otto hands back | only on some hosts (note 11) | `exec-exit-code` |
| read a command's output without otto's own prompts, echo and markers mixed into it | only on some hosts (note 11) | `exec-framing` |
| read a command's output exactly as the command wrote it -- blank lines, spaces, tabs and carriage returns intact -- with only the one final newline removed | only on some hosts (note 11) | `exec-output-exact` |
| read `retcode` 128+N for a command killed by signal N, as a shell reports it, and keep using the host afterwards | only on some hosts (note 11) | `exec-signal-exit` |
| run an `exec` that starts from the state its session was set up with (a console's narrower promise is linked below the table), and trust that its `cd`, variables, options, `exit` and, where it runs on a terminal, its terminal settings reach no later `exec` or `run` | only on some hosts (note 11) | `exec-isolated` |
| run an `exec` without `expects=` and have its command read end of file on stdin, rather than wait on a terminal nobody types into | only on some hosts (note 11) | `exec-stdin` |
| gather several `exec` calls on one host and have each return its own command's output, never another call's | only on some hosts (note 11) | `exec-concurrent` |
| cancel an `exec` and have otto stop its command before the cancellation reaches you (on the local machine: its leader ended and its process group hung up, as the known gaps below say), with the host answering the next command | only on some hosts (note 11) | `exec-cancel` |
| read the host log and find the lines of each command's `value`, one record per line, blank lines and a program's own `\r` kept, under `log=NORMAL` and `log=QUIET`, and nothing under `log=NEVER` | only on some hosts (note 11) | `exec-log` |
| run a command under a dry run and have it reach no device, getting back a `NotRun` result with `retcode == -1`, no route, `command` the line a live call would type, and a `value` that raises `CommandNotRunError` | only on some hosts (note 11) | `exec-dry-run` |
| pass `expects=`, `sudo=` or `user=` to `run` or `exec`, alone or together, on a `ShellCommand` or inside `as_user()`, and get the same effect on every entry; where the device cannot honour an option, the call raises `UnsupportedOptionError` naming it, and nothing is typed | `sudo=` with `user=` on a device (note 8); `user=` and `as_user()` without a second user (note 9); only on some hosts (note 11) | `exec-options` |
| give a command a time budget and have a breach reported the same way on every route, with the process otto started stopped, the call returned once the budget and otto's bounded cleanup have passed, even when the command closed its output, and the session usable afterwards | only on some hosts (note 11) | `timeout` |

On some devices a promise above is narrower: see
{ref}`Where a device gets a narrower promise <command-contracts-narrower>`.

## Where a promise is not checked

1. Not checked on BusyBox's applets run on the local machine: there the applets are put on the `PATH` of the `run` session only, which a one-off `exec` process never sees.
2. Not checked on a Zephyr board, so not promised there: the Zephyr shell runs each command on its own thread rather than as a process, so there is no command a signal can kill there.
3. Not checked on a Zephyr board, so not promised there: otto has not measured whether one `exec` on a Zephyr board can change the shell state the next one sees, so this is not checked there.
4. Not checked on a Zephyr board, so not promised there: the Zephyr shell runs each command on its own thread rather than as a process with a stdin, so there is nothing to read there.
5. Not checked on a Zephyr board, a BusyBox device over `console` and a GNU/Linux machine over `console`, so not promised there: otto checks a cancelled `exec` on a Zephyr board and on a console with unit tests rather than on a device. The bed firmware has no command that blocks (issue #1066), and a Zephyr guest is never driven to a cancellation (issue #260); and a console takes one client, so otto cannot watch the command from a second connection while it cancels it.
6. Not checked on a Zephyr board, so not promised there: the Zephyr shell has no stock command that leaves an effect a later command can read back, so nothing on the device could show whether a command ran; a unit test checks that a dry run writes nothing to an open Zephyr session.
7. On a Zephyr board `expects=` is honoured, but its shell has no command that prompts and reads a line, so unit tests check it, not a device.
8. `sudo=` together with `user=` runs on no device: no login on the test bed may do it, so only a stand-in `sudo` on the local machine checks it.
9. `user=` and `as_user()` are checked only on hosts whose login has a second user to switch to.
10. Not checked on a Zephyr board, so not promised there: otto does not test what happens when a Zephyr command runs longer than its timeout, so it promises nothing about it. The Zephyr shell runs one command at a time, on its own thread, so a command made to outlive its budget would block the very shell the test then has to find still answering; and the bed firmware has no command that blocks for a chosen time (issue #1066).
11. Checked only on a GNU/Linux machine over `ssh`, a BusyBox device over `ssh` and the local machine logged in to over SSH: only there does `exec` otherwise get an SSH channel of its own, which `exec-shell` sends through a shell session instead.

## Declared exceptions and undefined behaviour

These declared exceptions apply to `exec`; each is described on
{ref}`Command contracts <command-contracts>`:

- {ref}`How long one command can be, on the ssh-channel route <exception-how-long-one-command-can-be-on-the-ssh-channel-route>`
- {ref}`How long one line of a command can be, on the pooled-shell and default-session routes <exception-how-long-one-line-of-a-command-can-be-on-the-pooled-shell-and-default-session-routes>`
- {ref}`A 0xFF byte in a command's output, on a console whose host record sets console_options.escapes_iac: false <exception-a-0xff-byte-in-a-command-s-output-on-a-console-whose-host-record-sets-console-options-escapes-iac-false>`
- {ref}`Elevating a command with sudo= on run and exec, on the embedded and zephyr host families <exception-elevating-a-command-with-sudo-on-run-and-exec-on-the-embedded-and-zephyr-host-families>`
- {ref}`Running a command as another user= on run and exec, on the embedded and zephyr host families <exception-running-a-command-as-another-user-on-run-and-exec-on-the-embedded-and-zephyr-host-families>`

What is never measured, so not promised, for `exec` as for every command:
{ref}`Undefined <command-contracts-undefined>`.

(command-contract-exec-known-gaps)=
### Known gaps (not device limitations)

- On the local machine, a timed-out `exec` sends TERM, and a cancelled one HUP, to the other processes of the command's process group and ends its leader, but they may still be exiting when the call returns, and one that ignores the signal survives (`tests/unit/host/test_process_stop.py`).

(command-contract-exec-host-forms)=
### Per-host forms

- An `exec(sudo=True)` sent through a shell session on a host that elevates with sudo types one of two forms, set per host by `userland_options.sudo_form` ({ref}`sudo-form`). Unset, otto asks the host's sudo `sudo -l "$SHELL"` once per login, on that login's first such `exec`: `shell` on a yes, `validate` when sudo says not allowed and then holds a timestamp (`sudo -n -v`); a refusal after the password with no timestamp fails that `exec` without a second password; any other outcome is unread, uses `shell` and is asked again. One login's answer is never used for another. `run(sudo=True)` does not read it and types `sudo -S <cmd>` (issue #1086).
- `shell` (the default): one sudo call, `sudo -S "$SHELL" -c '<cmd>'`, so the whole command string runs as root, in a new shell of the login's own kind (not the pooled shell: its functions, aliases and unexported variables are not seen). It needs the host's sudoers to allow the login's shell; where it does not, the `exec` returns sudo's refusal (status 1) and nothing runs: set `sudo_form` to `validate` (`tests/unit/host/test_exec_stdin.py`).
- `validate`: `sudo -S -v`, then `sudo -n <cmd>`, so only the command's first simple command runs as root. It needs the host's sudo to keep its timestamp between the two calls; on a sudo with `timestamp_timeout=0` the password is accepted and the command is then refused (`sudo: a password is required`, status 1) and does not run: set `sudo_form` to `shell` (`tests/unit/host/test_exec_stdin.py`).
