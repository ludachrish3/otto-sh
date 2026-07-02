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
>>> [cr.value.strip() for cr in result]
['hello', 'world']
```

File transfers work the same way -- `put` and `get` map to
{meth}`~otto.host.unix_host.UnixHost.put` and
{meth}`~otto.host.unix_host.UnixHost.get`:

```python
from pathlib import Path

# Upload
res = await host.put(
    src_files=[Path("firmware.bin")],
    dest_dir=Path("/tmp"),
)
if not res:
    logger.error(f"upload failed: {res.msg}")

# Download
res = await host.get(
    src_files=[Path("/var/log/syslog")],
    dest_dir=Path("./logs"),
)
if not res:
    logger.error(f"download failed: {res.msg}")
```

```{note}
`put` and `get` are available on all host types, with per-class semantics:
{class}`~otto.host.local_host.LocalHost` copies files within the local
filesystem, {class}`~otto.host.unix_host.UnixHost` transfers between the
local machine and the remote host, and `EmbeddedHost` provides its own
console/tftp transfer path; see {doc}`../embedded`.
```

## Exit codes

Every `otto host <name> <verb>` invocation derives its exit code from the
verb's returned {class}`~otto.result.Result` family, via `Result.exit_code`.
Command results are ssh-like: the shell's retcode when the command ran,
255 when it never ran.  (`oneshot` is Python-only — it is not a CLI verb,
so these rows apply to `run`.)

| Situation | Exit code |
| --- | --- |
| Verb succeeded (incl. `Status.Skipped`) | 0 |
| `run`: a command failed | that command's shell retcode (ssh-like: `run 'exit 42'` exits 42) |
| `run`: the command never ran (connection failure) | 255 (matches ssh's convention) |
| Any other verb: `Status.Failed` | 1 |
| Any other verb: `Status.Error` | 2 (note: Click also uses 2 for CLI usage errors) |
| Any other verb: `Status.Unstable` | 3 |

Custom verbs on third-party host classes may return plain values instead of a
`Result`; the CLI prints them as-is and exits 0.

```{toctree}
:hidden:

commands/index
capabilities
connections
configuration
```
