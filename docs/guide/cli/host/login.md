# otto host login

Open a fully interactive shell on a remote host with `login`:

```bash
otto --lab my_lab host router1 login
```

Stdin and stdout are bridged to the remote terminal in raw mode, so full-screen
TUIs (`vi`, `top`, `less`) work the same as under a native `ssh` or `telnet`
client.  While the session runs, every remote byte is also appended to the
invocation's `session.log` so the transcript is preserved alongside the normal
`otto host run` output.

**Ending the session.**  Exit the remote shell normally (`exit`, `logout`, or
`Ctrl+D`) or press `Ctrl+]` — the classic `telnet(1)` escape byte — to disconnect
locally without waiting on the remote.  The escape hatch exists because `Ctrl+C`
is forwarded to the remote so remote commands can be interrupted the usual way.

**Terminal resize.**  Local `SIGWINCH` is forwarded to the remote PTY on both SSH
(via `window-change` channel request) and telnet (via NAWS subnegotiation), so
remote TUIs reflow on resize.  For telnet, NAWS is enabled automatically for the
`login` command only — non-interactive `run`/`put`/`get` calls keep the historical
fixed column width.

**Hops.**  `login` honors `--hop` and the `hop` field in `lab.json`, so an
interactive session can tunnel through jump hosts just like the other
subcommands (see {doc}`Connection control <connections>`):

```bash
otto --lab my_lab host --hop jumpbox router1 login
```

**`--user NAME`.**  Open the shell as this user. Containers implement it via
`docker exec -u`; unix hosts replay any login-proxy hops needed to reach that
login (see {doc}`../../../library/extending-backends`); host families that can
do neither raise. See {ref}`container-users` for how the container case
combines with a compose fragment's declared default and the image's `USER`.
