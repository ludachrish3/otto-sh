# otto host put

Transfer local files to a remote host with `put`:

```bash
otto --lab my_lab host router1 put firmware.bin /tmp/
```

Multiple source files are supported:

```bash
otto --lab my_lab host router1 put config.yaml license.key /opt/app/
```

File transfers default to SCP. To use a different backend (SFTP, FTP, or the
custom netcat backend), see {doc}`Connection control <connections>` for the per-invocation
`--transfer` override and {doc}`netcat` for the netcat backend.
## Arguments

```text
otto host <HOST_ID> put SRC... DEST
otto host <HOST_ID> get SRC... DEST
```

`SRC...` is one or more source paths (space-separated); `DEST` is the
destination directory.  For `put`, sources are local paths; for `get`, sources
are remote paths.  The remote side of each — `get`'s sources and `put`'s
destination — completes against the host itself
([Remote path completion](../index.md#remote-path-completion)).

| Option | Default | Description |
| ------ | ------- | ----------- |
| `--mode TEXT` | backend default | **`put` only.** Octal permission bits for the uploaded file(s) — `755`, `0644`, `0o4755`. Always read as octal, never decimal |
| `--user NAME` | none | Chown the landed file(s) to this owner. Containers only — every other host family refuses it on both `put` and `get`. On containers, `get` accepts it and ignores it: reads are ownership-indifferent. See {ref}`container-users` |

The mode is applied after the bytes land, in one batched `chmod` covering the
whole transfer.  Hosts whose transfer backend has no permission model
(embedded `console`/`tftp`) reject `--mode` before transferring anything,
rather than accepting it and silently doing nothing.

```console
$ otto host web1 put ./app.bin /opt/bin --mode 755
```
The same argument shapes apply to {doc}`get`.
