# Connection control

How otto reaches a host for a single invocation: route through SSH jump hosts with
`--hop`, and override the terminal or file-transfer protocol with `--term` /
`--transfer`. (For *persistent* connection tuning in `hosts.json`, see
{doc}`Host configuration <configuration>`.)

## Reaching hosts through hops

To change the final hop before a target host, use `--hop` to choose an
intermediate SSH jump host:

```bash
otto --lab my_lab host --hop jumpbox target_seed run "uname -a"
```

The hop host must support SSH. The target host can use any terminal
protocol (SSH or telnet) -- otto tunnels the connection through the
hop automatically.

Hops can be chained: if the hop host itself has a `hop` configured,
otto builds a recursive tunnel chain
(`otto -> hop1 -> hop2 -> ... -> target`).  Circular references are
detected and rejected at connection time.

The `--hop` option works with all subcommands:

```bash
otto --lab my_lab host --hop jumpbox target_seed put firmware.bin /tmp/
otto --lab my_lab host --hop jumpbox target_seed get /var/log/syslog ./logs/
```

For persistent hop configuration, set the `hop` field in `hosts.json`
(see {doc}`../lab-config` for the full host schema):

```json
{
    "ip": "10.10.200.12",
    "element": "target",
    "board": "seed",
    "hop": "jumpbox_seed",
    "creds": [{ "login": "admin", "password": "secret" }]
}
```

### File transfer protocols through hops

All file transfer protocols work through SSH hops:

- **SCP** (PUT and GET) — native SSH tunnel, no port forwarding needed.
- **SFTP** (PUT and GET) — piggybacks on the tunneled SSH connection.
- **FTP** (PUT and GET) — control and PASV data ports are forwarded
  automatically through the tunnel.
- **Netcat** (PUT and GET) — see {doc}`commands/netcat`.

Embedded hosts using the **console** transfer backend are also hop-capable:
the telnet console session is tunnelled through the SSH hop in the same way
as any other telnet connection. See {doc}`../embedded` for a worked example.

## Overriding protocol for a single session

Use `--term` to override the terminal protocol and `--transfer` to override
the file transfer protocol for a single invocation without editing `hosts.json`:

```bash
otto --lab my_lab host --term telnet router1 run "show version"
otto --lab my_lab host --transfer sftp router1 put firmware.bin /tmp/
```

Both options can be combined:

```bash
otto --lab my_lab host --term telnet --transfer ftp router1 put config.txt /etc/
```

Valid values for the built-in backends (UnixHost):

- `--term`: `ssh`, `telnet`
- `--transfer`: `scp`, `sftp`, `ftp`, `nc`

The accepted values are validated against the host's configured menu
(`valid_terms` / `valid_transfers` fields in `hosts.json`); out-of-menu
selections are rejected at invocation time. See {doc}`../lab-config` for
those fields. Projects can also register additional backends via
`register_term_backend` / `register_transfer_backend`; see
{doc}`../extending-backends`.

Embedded hosts use the `console` / `tftp` transfer backends instead — see
{doc}`../embedded`.

The override applies only to the current invocation. To persist the change,
update the `term` or `transfer` field in `hosts.json`.
