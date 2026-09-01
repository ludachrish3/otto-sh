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
| `--user NAME` | none | Transfer as this owner. On containers it chowns the landed file(s), and `get` accepts it and ignores it — reads are ownership-indifferent (see {ref}`container-users`). On unix hosts it *authenticates* as that user, so `put` lands the bytes already owned by them and `get` reads with their permissions — direct-cred users only, never over the `ftp` backend. Every other host family refuses it on both verbs |

On a unix host the transfer rides that user's own connection, so a `DEST` that
is still relative once
[`default_dest_dir`](../../configuration/lab-config.md#common-optional) has been
applied lands in *their* home directory, not the login user's.

The mode is applied after the bytes land, in one batched `chmod` covering the
whole transfer.  Hosts whose transfer backend has no permission model
(embedded `console`/`tftp`) reject `--mode` before transferring anything,
rather than accepting it and silently doing nothing.

```console
$ otto host web1 put ./app.bin /opt/bin --mode 755
```
The same argument shapes apply to {doc}`get`.
