(userland-capabilities)=

# Userland capabilities

Setting up a lab? {doc}`../../../configuration/lab/probing-hosts` is the step
to follow; this page is the reference behind it.

otto adapts to the device's userland rather than assuming a GNU one: which
elevation mechanism exists, which `timeout` calling convention the applet
speaks, whether `base64` is there at all. Those answers are settled by a probe
round the first time a fresh host object needs one — cheap on a server, slow on
a BusyBox device — and they can be *pinned* in that host's `userland_options`
so the round never happens again.

`probe` is how you get the pin:

```text
otto host <id> probe
```

It resolves the capabilities and prints two things, then the same call's
{doc}`protocol survey <protocols>` right after them. First a reading of
every capability with its value **and its source**:

```text
capability       value      source
applet_base64    present    probed
applet_nc        present    probed
applet_scp       absent     probed
base64_flag      -d         probed
checksum         md5sum     probed
elevation        sudo       probed
shell_dialect    bash       probed
stat_size        stat       probed
sudo_form        shell      probed
timeout_style    coreutils  probed
```

The source is the actionable column. `declared` means the value is already
pinned in this host's `userland_options` and is never re-probed; `probed` means
the device answered, so it is worth pinning; `assumed` means otto could not ask
and the value is only what otto did before it asked anything.

Then the pasteable payload — the settled answers, under the key a `lab.json`
host entry carries:

```json
"userland_options": {
  "elevation": "sudo",
  "sudo_form": "shell",
  "timeout_style": "coreutils",
  "applet_base64": "present"
}
```

Paste that into the host's entry and the next connection issues no probe at
all. See {doc}`../../../configuration/host-options` for where the table lives and how it layers.

**Assumed values are absent from the payload.** The reading above the payload
is where those values are visible, labelled for what they are. A host that
could answer nothing prints an empty pin and says why. Don't pin an assumed
value by hand: a pinned value is never re-probed, so a guess would stick.

`LocalHost`, `DockerContainerHost`, and `EmbeddedHost` build no capability resolver at all, so
`probe` on those reports that plainly instead of printing a pin.

(sudo-form)=
## How `exec(sudo=True)` elevates: `sudo_form`

An `exec(sudo=True)` that otto types into a shell session on a host that
elevates with sudo uses one of two forms. `sudo_form` in the host's
`userland_options` picks one:

`shell` (the default)
: One sudo call runs the whole command string in the login's shell:
  `SUDO_PROMPT='<prompt>' sudo -S "${SHELL:-/bin/sh}" -c '<cmd>'`. Every part
  of a compound command (`a; b`, a pipe, a redirect) runs as root, in the same
  shell language as the session (bash syntax on a bash login). The host's
  sudoers must allow the user to run that shell. The command runs in a new
  shell started by sudo, not in the pooled shell's own subshell: the pooled
  shell's functions, aliases and unexported variables are not there, and sudo
  resets the environment. Under `validate` only the first simple command ran
  under sudo; the rest ran in the pooled shell and saw those.

`validate`
: Two sudo calls: `sudo -S -v` checks the password and runs nothing, then
  `sudo -n <cmd>` runs the command. sudo receives the command itself, so only
  its first simple command runs as root, which is what a sudoers that lists
  single commands matches. The host's sudo must keep its timestamp between the
  two calls: on a sudo with `timestamp_timeout=0` the password is accepted and
  the command is then refused (`sudo: a password is required`, status 1).

When `sudo_form` is not set, the first such `exec` by each login asks the
host's sudo `sudo -l "${SHELL:-/bin/sh}"` once, as that login (after `as_user`
or a proxied login, the switched user), about the shell the `shell` form would
run. sudoers answers per user, so the answer is kept for that login only: one
login's answer is never used for another, and a declared `sudo_form` holds for
every login. `otto host <id> probe` asks the same question as the login it
opens (`--user`), so its table has a `sudo_form` row, it says which login
answered, and its pin carries that login's answer to paste. Unlike the other rows,
this one needs sudo itself: otto types the login's password from the host's
`creds` when it has one (at most once; the `exec` itself may type it once
more on a sudo that keeps no timestamp), and otherwise gives sudo end of file.

Only an answer from sudo about the shell is a measurement: status 0 gives
`shell`. A non-zero status after otto gave the password is ambiguous (a sudo
with `passwd_tries=1` exits 1 on a wrong password as on "not allowed"), so
otto asks `sudo -n -v` once, which reads no password: status 0 means sudo
holds a valid timestamp, the password was accepted and the shell was refused,
which gives `validate` (the form that needs that timestamp). Anything else
fails that `exec` with an error that says sudo refused the login and lists
the three causes sudo does not tell apart (the login may not use sudo, the
configured password is wrong, or sudo refuses the shell and keeps no
timestamp), and names `userland_options.sudo_form`. The password is not sent
again. Both
questions are one typed command, so they are asked on the same terminal (sudo
keeps its timestamp per terminal by default). The same refusal is what a
sudoer sees on a host whose sudo keeps no timestamp (`timestamp_timeout=0`) and
whose sudoers refuses the login's shell: the password was right, but neither
form can run there (`shell` is refused, `validate` needs the timestamp), so
`sudo_form` cannot be measured on such a host and setting it does not help. Everything
else is unread: no password configured and sudo did not say yes (it may want
the password otto does not have), a timeout, a question that could not be
asked. Unread is not pinned: `probe` prints the row as `unmeasured` with the
reason, the `exec` uses the default `shell`, and the next `exec` asks again.
A refused password fails that `exec` without a second try in the same call;
every later `exec` tries once, so count those against the account's lockout. A session that is
already root asks nothing (`unmeasured` in `probe`) and uses `shell`. The row
appears only on a host whose elevation is `sudo`.

otto never switches forms on its own. When the host's sudo refuses the form,
the `exec` returns sudo's refusal (`Sorry, user ... is not allowed to execute
'/bin/bash'` for `shell` on a bash login, `sudo: a password is required` for `validate`) as a
failed result and nothing runs: set `sudo_form` to the other form.

```json
"userland_options": {
  "sudo_form": "validate"
}
```

Both forms keep `exec`'s stdin promise: without `expects=` the command reads
end of file; with `expects=` it reads the terminal, where otto types the
answers. `run(sudo=True)` does not read `sudo_form`: it types
`SUDO_PROMPT='<prompt>' sudo -S <cmd>` on every host.
