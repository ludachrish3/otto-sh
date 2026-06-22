# Working with hosts

`otto host` provides direct access to host operations from the command line --
running commands, transferring files, opening an interactive shell, and invoking
host capabilities -- without writing a test suite or instruction.

## Syntax

The host ID comes before the subcommand, so all host-level options apply to every
action:

```text
otto host <host_id> <command> [ARGS...] [OPTIONS]
```

## The host verb model

Every `otto host` action is a **verb** on the host. The four core verbs --
`run`, `put`, `get`, and `login` -- are built in (see {doc}`Core commands <commands/index>`).
Every other verb is a **capability verb**: a host method marked `@cli_exposed`
that otto turns into a subcommand automatically, scoped to that host's class.
`otto host <host_id> --help` lists exactly the verbs the chosen host supports.
See {doc}`Capability verbs <capabilities>` for the capability verbs and {doc}`Netcat transfers <commands/netcat>`,
{doc}`Connections <connections>`, and {doc}`Configuration <configuration>` for transport and tuning.

## Listing hosts

Use `--list-hosts` to see which host IDs are available in the loaded lab:

```bash
otto --lab my_lab host --list-hosts
```

This is the same `--list-hosts` option available on the top-level `otto` command.

## Dry run

Like all otto commands, `--dry-run` (or `-n`) previews what would happen without
executing commands or transferring files:

```bash
otto --lab my_lab --dry-run host router1 run "make install"
```

## From Python

The `otto host` subcommands map directly to methods on the
{class}`~otto.host.host.BaseHost` class. Everything `otto host` does from the CLI
can also be done inside instructions and test suites:

```{doctest}
>>> host = LocalHost()
>>> result = run(host.run(["echo hello", "echo world"]))
>>> result.status
<Status.Success: 0>
>>> [cs.output.strip() for cs in result.statuses]
['hello', 'world']
```

File transfers work the same way -- `put` and `get` map to
{meth}`~otto.host.unix_host.UnixHost.put` and
{meth}`~otto.host.unix_host.UnixHost.get`:

```python
from pathlib import Path

# Upload
status, msg = await host.put(
    src_files=[Path("firmware.bin")],
    dest_dir=Path("/tmp"),
)

# Download
status, msg = await host.get(
    src_files=[Path("/var/log/syslog")],
    dest_dir=Path("./logs"),
)
```

```{note}
File transfer methods are only available on
{class}`~otto.host.unix_host.UnixHost` instances, not
{class}`~otto.host.local_host.LocalHost`.  The doctest above uses
`run` which is available on all host types.
`EmbeddedHost` provides its own console/tftp transfer; see {doc}`../embedded`.
```

```{toctree}
:hidden:

commands/index
capabilities
connections
configuration
```
