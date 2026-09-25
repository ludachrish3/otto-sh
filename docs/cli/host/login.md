# otto host login and logout

Open a fully interactive shell on a remote host with `login`:

```bash
otto --lab my_lab host router1 login
```

Stdin and stdout are bridged to the remote terminal in raw mode, so full-screen
TUIs (`vi`, `top`, `less`) work the same as under a native `ssh` or `telnet`
client.  While the session runs, every remote byte is also appended to the
invocation's `session.log` so the transcript is preserved alongside the normal
`otto host exec` output.

**Ending the session.**  Exit the remote shell normally (`exit`, `logout`, or
`Ctrl+D`) or press `Ctrl+]` — the classic `telnet(1)` escape byte — to disconnect
locally without waiting on the remote.  `Ctrl+C` is forwarded to the remote,
so remote commands can be interrupted the usual way.

**Terminal resize.**  Local `SIGWINCH` is forwarded to the remote PTY on both SSH
(via `window-change` channel request) and telnet (via NAWS subnegotiation), so
remote TUIs reflow on resize.  For telnet, NAWS is enabled automatically for the
`login` command only — non-interactive `run`/`put`/`get` calls keep a fixed
column width.

**Hops.**  `login` honors `--hop` and the `hop` field in `lab.json`, so an
interactive session can tunnel through jump hosts just like the other
subcommands (see {doc}`Connection control <connections>`):

```bash
otto --lab my_lab host --hop jumpbox router1 login
```

**`--user NAME`.**  Open the shell as this user. Containers implement it via
`docker exec -u`; unix hosts replay any login-proxy hops needed to reach that
login (see {doc}`../../cookbook/extending/extending-backends`); host families that can
do neither raise. See {ref}`container-users` for how the container case
combines with a compose fragment's declared default and the image's `USER`.

**Console hosts.**  On a host whose term is `console`
({ref}`console-term`), `login` dials the console server, logs in from the
`login:` prompt, and bridges the line; if the line is not at `login:` it
fails with the same {ref}`errors <console-failures>` as any console
connect. Otto's own session on the host is closed first, since it holds
the one line. `Ctrl+]` on a console *does* end the remote session: closing
the bridge logs the line out (Ctrl-D, then a bounded wait for `login:`)
unless `console_options.logout` is `false`, so the next client finds the
login prompt. That is one Ctrl-D: a nested shell or `su` left running
stays logged in, so use `otto host <id> logout` then.

**`--force`** (console hosts only).  Run the reset below first, report
what it found on an `[otto]` status line, then log in and bridge.  Use it
when the console was left logged in or half-way through a login and you
want the line, whoever left it there.  On an `ssh` or `telnet` host it is
refused before anything is dialled:
`<host>: --force applies to console hosts only (term is 'ssh')`.

```bash
otto --lab my_lab host test2 login --force
```

(host-logout)=

## otto host logout

```bash
otto --lab my_lab host test2 logout
```

Put a serial console back at its `login:` prompt, ending whatever session
was left on it, and print the outcome:

- `<host>: console <server>:<port> already at login prompt` — nothing was
  running; only Enter was sent.
- `<host>: console <server>:<port> reset: login prompt restored` — a session
  or a half-typed login was ended.

**What it does to a shared line.**  It ends *whatever* is on the line — a
colleague's shell as readily as one a crashed run left behind — so it is a
command you choose to run; otto never runs it on its own. It never types a
credential. Otto's own session on the host is closed first, and reconnects
on its next command.

**The reset**, bounded at every step by `console_options.login_timeout`:

1. Press Enter. At a `login:` prompt, press Enter once more: getty prints
   its prompt again, but `login(1)`'s own retry prompt takes the Enter as an
   empty username and asks for a password. A second `login:` means the line
   is clean, and the reset is done.
2. At a password prompt, press Ctrl-C. `login(1)` exits and getty prints a
   fresh `login:`.
3. Otherwise a shell or a program is running. Up to three rounds of:
   Ctrl-C, a wait until the line shows the interrupt landed (its `^C` echo or
   a prompt), then Ctrl-D (end of input — it ends `bash`, `sh`, `python` and
   most REPLs) and Enter. Each round stops as soon as `login:` appears; three
   cover a nested shell and an `su` inside it. The wait matters: a tty
   discards input it has queued when Ctrl-C arrives, so a Ctrl-D sent
   straight after it would be lost.
4. Still no `login:` — `could not reset console after 3 rounds of
   Ctrl-C/Ctrl-D; last seen: '…'`. A pager or an editor answers neither key;
   attach to the console port with a telnet client and quit it by hand.

A silent or busy line fails as it would on connect
({ref}`the console failures <console-failures>`). On a console with no login step
(`console_options.login: false`, and every embedded host) there is nothing
to return to: `logout` reports
`<host>: this console has no login step; nothing to reset` and dials
nothing. On an `ssh` or `telnet` host it is refused:
`<host>: logout applies to console hosts only (term is 'ssh')`.

From Python, `await host.logout()` returns a `Result` whose value is the
outcome line; a failure raises `ConsoleError`.
