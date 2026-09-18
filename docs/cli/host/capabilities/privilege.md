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

The command is wrapped as `sudo -S -p 'otto-sudo:' <cmd>`. On a
{class}`~otto.host.unix_host.UnixHost` the login user's password (from `creds`)
is auto-answered through the expect channel; `LocalHost`/Docker assume
passwordless sudo by default. Caller-supplied `expects` are preserved (the
password expect is tried first). Embedded/RTOS hosts raise `NotImplementedError`.

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
