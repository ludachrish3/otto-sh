<!-- GENERATED FILE -- do not edit by hand.
     scripts/render_support_matrix.py (render_command_contracts) renders this
     from the `entries` marker on each command contract checked
     through `exec`.
     Regenerate with `uv run python -m scripts.render_support_matrix`;
     edits are overwritten. -->

(command-contract-exec)=

# `exec`: a one-off command

`exec` runs a one-off command: nothing one call changes is carried to the
next. From the CLI it is `otto host <id> exec` ({doc}`/cli/host/exec`); from
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
- The call needs a shell: it passes `expects` (patterns otto answers while the command runs, such as a password prompt), `sudo=True`, or a `user` otto cannot log in as directly (`pooled-shell`).
- `exec(user=...)` over `ssh` for a user with SSH credentials of its own, and no `expects` or `sudo`: an SSH connection as that user, even when the login is proxied or the host declares a session-setup hook (`ssh-channel`).
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
| read `retcode` 128+N for a command killed by signal N, as a shell reports it, and keep using the host afterwards | only on some hosts (note 2) | `exec-signal-exit` |
| give a command a time budget and have a breach reported the documented way, with the session still usable afterwards | not on a Zephyr board (note 3); not on BusyBox's applets run on the local machine (note 1) | `timeout` |

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
| run a command and trust the exit code otto hands back | only on some hosts (note 4) | `exec-exit-code` |
| read a command's output without otto's own prompts, echo and markers mixed into it | only on some hosts (note 4) | `exec-framing` |
| read a command's output exactly as the command wrote it -- blank lines, spaces, tabs and carriage returns intact -- with only the one final newline removed | only on some hosts (note 4) | `exec-output-exact` |
| give a command a time budget and have a breach reported the documented way, with the session still usable afterwards | only on some hosts (note 4) | `timeout` |

On some devices a promise above is narrower: see
{ref}`Where a device gets a narrower promise <command-contracts-narrower>`.

## Where a promise is not checked

1. Not checked on BusyBox's applets run on the local machine: there the applets are put on the `PATH` of the `run` session only, which a one-off `exec` process never sees.
2. Checked only on a GNU/Linux machine over `ssh`, a BusyBox device over `ssh`, the local machine and the local machine logged in to over SSH: on every other host `exec` types the command into a shell session (the `pooled-shell` or `default-session` route), and a killed command would take that session down with it, so otto does not check it there yet.
3. Not checked on a Zephyr board, so not promised there: otto does not test what happens when a Zephyr command runs longer than its timeout, so it promises nothing about it. The Zephyr shell runs one command at a time, on its own thread, so a command made to outlive its budget would block the very shell the test then has to find still answering.
4. Checked only on a GNU/Linux machine over `ssh`, a BusyBox device over `ssh` and the local machine logged in to over SSH: only there does `exec` otherwise get an SSH channel of its own, which `exec-shell` sends through a shell session instead.

## Declared exceptions and undefined behaviour

These declared exceptions apply to `exec`; each is described on
{ref}`Command contracts <command-contracts>`:

- {ref}`How long one command can be, on the ssh-channel route <exception-how-long-one-command-can-be-on-the-ssh-channel-route>`
- {ref}`How long one line of a command can be, on the pooled-shell and default-session routes <exception-how-long-one-line-of-a-command-can-be-on-the-pooled-shell-and-default-session-routes>`
- {ref}`A 0xFF byte in a command's output, on a console whose host record sets console_options.escapes_iac: false <exception-a-0xff-byte-in-a-command-s-output-on-a-console-whose-host-record-sets-console-options-escapes-iac-false>`

What is never measured, so not promised, for `exec` as for every command:
{ref}`Undefined <command-contracts-undefined>`.
