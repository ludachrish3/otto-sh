# Privilege elevation

## One-off: `run(sudo=True)`

    await host.run("apt-get update", sudo=True)

The command is wrapped as `sudo -S -p 'otto-sudo:' <cmd>`. On a `UnixHost` the
login user's password (from `creds`) is auto-answered through the expect
channel; `LocalHost`/Docker assume passwordless sudo by default. Caller-supplied
`expects` are preserved (the password expect is tried first). Embedded/RTOS
hosts raise `NotImplementedError`.

## Scoped: `async with host.as_user(...)`

    async with host.as_user("root"):
        await host.run("systemctl restart foo")   # runs as root
    # session returns to the original user here

`as_user` `su`'s the **persistent session** to the target user on entry and
sends `exit` on the way out. The imperative form is `await host.switch_user(
"root")`. Target-user passwords come from `creds` when present, or pass
`password=` explicitly. Embedded hosts raise `NotImplementedError`.
