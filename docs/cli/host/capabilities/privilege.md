# Privilege elevation

Privilege elevation is Python-only — there are no CLI verbs for `as_user` or
`switch_user`. Full signatures: {class}`~otto.host.host.BaseHost`.

otto keeps **four** privilege layers, and they answer different questions.
`run(sudo=True)` is *per-command* elevation: one command runs elevated and
the session is untouched. {meth}`~otto.host.host.BaseHost.as_user` and
{meth}`~otto.host.host.BaseHost.switch_user` set the *session identity*:
every later call on that session runs as the new user until the block ends
or the caller switches back. The elevation *mechanism* is chosen from the
device's userland ({attr}`~otto.host.userland.Userland.elevation`, applied
by `PosixPrivilege`) — which binary a `sudo=True` command is actually built
around on a host that has no `sudo`. And the *login target*
({attr}`~otto.host.connections.ConnectionManager.login_target`) is the user
the connection lands as in the first place, before any elevation happens.
Reach for the narrowest one that answers your question: a single command
wants `sudo=True`, a block of them wants `as_user`, and the other two are
configuration — probed from the device or declared in lab data — rather than
something you call.

## One-off: `run(sudo=True)`

    await host.run("apt-get update", sudo=True)

The command is wrapped as `SUDO_PROMPT='otto-sudo-<random>:' sudo -S <cmd>`,
or as `su -c '<cmd>'` on a host whose userland has `su` and no `sudo`. On a
{class}`~otto.host.unix_host.UnixHost` the password (from `creds`: the login
user's for sudo, root's for su) is auto-answered through the expect channel;
`LocalHost`/Docker assume passwordless sudo by default. Caller-supplied
`expects` are preserved (the password answer is tried first). The password
travels as the cred's own `SecretStr` and is unwrapped only inside the
session's transport write (what a traceback then shows is
{ref}`hidden-library-frames`); a caller's own expect may answer with a
`SecretStr` the same way. Embedded/RTOS
hosts raise `NotImplementedError`.

### Where the password goes

otto types the password only at the elevation prompt, and only once per
command:

- **The sudo prompt is random.** Each elevated command gets a fresh
  `otto-sudo-<16 letters and digits>:` prompt, set through the `SUDO_PROMPT`
  environment variable on the `sudo` invocation rather than as `sudo -p`, so it
  appears in no process's argument list. Output that merely looks like a prompt
  — the old fixed `otto-sudo:`, a `Password:` line, a process named like a
  prompt — gets no password, because it cannot match a prompt nobody else
  knows. The `sudo` argument list itself is unchanged: the command is still
  sudo's argv tail, exactly what sudoers authorizes.
- **Only after the command starts.** The terminal echoes the command line otto
  typed, prompt included, before the command runs, so the answer is armed only
  after otto's BEGIN marker.
- **Once.** A second sudo prompt means the password was refused. otto interrupts
  the command, checks the shell is back (discarding the session if it is not),
  and raises {exc}`~otto.host.errors.PasswordRefusedError` — "password refused
  for `<user>` on `<host>`" — instead of typing it again or waiting out the
  command's timeout. When you see it, check the password of that login's cred
  in the host's `creds`, and check that the host prompts with otto's
  `SUDO_PROMPT` ({ref}`elevation-policy`). `su` does not prompt twice: a wrong `su` password is the
  command's non-zero result, and a `Password:` the command prints after su
  accepted the password is output, answered by nothing.
- **Not to root's own `su`.** `su` never asks root for a password, so a session
  that is already root arms no answer for `su -c`, and a `Password:` in that
  command's output is just output.
- **Never logged.** The DEBUG "expect matched" line shows the pattern and
  `response=<N chars>`, for every expect pair, otto's or yours; under
  `LogMode.NEVER` there is no such line at all.

Your own `expects` pairs keep their behaviour: they answer every time they
match, from the first byte read.

(elevation-policy)=
### The supported elevation policy

The guarantees above hold for hosts set up this way:

- sudoers keeps its default `env_reset` and does not list `SUDO_PROMPT` in
  `env_keep`, so the elevated command never sees the prompt;
- the prompt sudo prints is otto's: `SUDO_PROMPT` overrides sudoers'
  `passprompt`, but a custom PAM prompt wins unless `passprompt_override` is
  set. A prompt otto cannot match is never answered, so the command waits out
  its timeout, as it did under `sudo -p`;
- `su` prompts otto's login user for a password: no passwordless PAM rule such
  as `pam_wheel trust` for that user.

Outside this policy the once-only rule still limits the password to one send
per command.

One exposure remains, and it needs the command itself to cause it: an elevated
command that prints other processes' environments while sudo is still running
(`ps e` under sudo, with sudo's credentials cached) can print the live prompt,
and otto answers it once. The prompt also appears in the logged command line;
it is worthless once the command ends.

## Scoped: `async with host.as_user(...)`

    async with host.as_user("root"):
        await host.run("systemctl restart foo")   # runs as root
    # session returns to the original user here

{meth}`~otto.host.host.BaseHost.as_user` `su`'s the **persistent session**
to the target user on entry and sends `exit` on the way out. The imperative form
is {meth}`~otto.host.host.BaseHost.switch_user`. Target-user passwords come
from `creds` when present, or pass `password=` explicitly. Embedded hosts raise
`NotImplementedError`.

## Inspecting the effective user: `current_user`

Each shell session tracks the OS user it is currently running as. The
read-only {attr}`~otto.host.host.BaseHost.current_user` property reports it
for the host's default session — seeded from the login user and changed only
by `switch_user` / `as_user`:

    async with host.as_user("root"):
        assert host.current_user == "root"
    assert host.current_user != "root"   # back to the login user

Named sessions elevate independently, so each carries its own
`current_user` (see `HostSession.current_user`).
