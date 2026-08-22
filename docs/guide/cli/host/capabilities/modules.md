# Kernel modules

Full signatures: {class}`~otto.host.unix_host.UnixHost`.

Unix hosts manage kernel modules with three verbs:

| Method | Behavior |
|--------|----------|
| `await host.lsmod()` | List loaded module names (`Result` whose `value` is `list[str]`). |
| `await host.load(file, name=None)` | Stage the `.ko` on the host, `insmod` it, then remove the staged file. `name` defaults to the file stem. |
| `await host.unload(name)` | `rmmod` the module. Idempotent: unloading a module that is not resident succeeds. |

`load` and `unload` elevate automatically — the `insmod`/`rmmod` runs under
`sudo` unless the session is already root (see `current_user` below).
As `otto host` verbs:

```text
otto host <id> lsmod
otto host <id> load ./build/my_driver.ko
otto host <id> unload my_driver
```

(`EmbeddedHost` has its own `load`/`unload` pair for loading binaries into the
device runtime via the host's binary loader — same verb names, different
signatures; see {doc}`../embedded`.)
