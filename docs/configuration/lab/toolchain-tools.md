# Declaring toolchain tools in lab data

Toolchain tools are host-wide artifacts (a cross-built `gdbserver`, a runtime
`.so`), declared alongside the coverage toolchain in the host's `lab.json`
entry:

```json
"toolchain": {
    "sysroot": "/opt/arm-toolchain",
    "tools": [
        {
            "name": "gdb",
            "source": "build/arm-linux-gnueabihf-gdb",
            "dest": "/usr/local/bin",
            "user": "root",
            "mode": "755"
        }
    ]
}
```

`name` is a **rename target**, not a label: `put` lands every file under its
source basename, so a tool whose `name` differs is `mv`'d there before it is
chowned — which is how `arm-linux-gnueabihf-gdb` installs as plain `gdb`. Those
destinations are usually root-owned, hence the per-tool `user` and `mode`.

One toolchain serves every owner on a host, so placing and removing it is a
host-wide step: no project's actions touch it (see
{doc}`../../cli/run/defaults`), and the default `install_toolchain_tools` is the method
most likely to need project surgery — override it on the host class.
