# otto host get

Retrieve files from a remote host with `get`:

```bash
otto --lab my_lab host router1 get /var/log/syslog ./logs/
```

Multiple remote paths are supported:

```bash
otto --lab my_lab host router1 get /var/log/syslog /var/log/auth.log ./logs/
```
Arguments mirror {doc}`put` — see its argument table.

## Pseudo-files and file modes

- **`/proc` and `/sys` files arrive empty** over the default `scp` transfer
  and over `nc`, and `get` still reports success
  ([why](../../configuration/host-options.md#what-a-get-can-fetch)); the
  `shell` transfer reads them, and the `local` host copies with Python's
  `shutil.copy2` rather than a transfer. Or read them through the shell:
  `otto --lab my_lab host router1 read-file /proc/version`, or
  `otto --lab my_lab host router1 exec 'cat /proc/version'`.
- **The exec bit is dropped.** Over `scp`, a fetched file takes your local
  default mode, not the remote one, unless the host sets
  `"scp_options": {"preserve": true}`
  ([SFTP, SCP, FTP, Netcat](../../configuration/host-options.md#sftp-scp-ftp-netcat)).
