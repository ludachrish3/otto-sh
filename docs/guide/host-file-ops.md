# Remote file operations

Posix-shell hosts (`UnixHost`, `LocalHost`, `DockerContainerHost`) expose
unix-CLI-style helpers for managing files **already on** the host — complementary
to `put`/`get`, which move files between local and remote.

| Method | Behavior |
|--------|----------|
| `await host.exists(path)` | `True` if `path` exists. |
| `await host.ls(path=".", all=False)` | List entry names (`all` includes dotfiles). |
| `await host.mkdir(path, parents=True)` | Create a directory. |
| `await host.rm(path, recursive=False, force=False)` | Remove a path. |
| `await host.cp(src, dst, recursive=False)` | Copy on the host. |
| `await host.mv(src, dst)` | Move/rename on the host. |
| `await host.read_file(path)` | Return text contents (raises `FileNotFoundError`). |
| `await host.write_file(path, data, append=False)` | Write text (base64 on the wire, injection-safe). |

`write_file`/`read_file` transfer text; for exact-byte/binary fidelity use
`put`/`get`.

## Embedded hosts

`EmbeddedHost` supports the subset its filesystem provides — `exists`, `ls`,
`rm` (via the device `fs` commands). `mkdir`/`cp`/`mv`/`read_file`/`write_file`
raise `NotImplementedError`; use `get`/`put` for device reads/writes.
