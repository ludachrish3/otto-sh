# Working with hosts

`otto host` provides direct access to host operations from the command line --
running commands, transferring files, opening an interactive shell, and invoking
host capabilities -- without writing a test suite or instruction.

Hosts are *defined* in `lab.json` — see {doc}`../setup/lab-config`.
This section is about *using* them.

## `otto host --help`

```{raw} html
:file: ../../_static/generated/termynal/help-host.html
```

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

## Tab completion

Host ids tab-complete from the loaded lab, including the built-in `local`
host:

```{raw} html
:file: ../../_static/generated/termynal/complete-host-ids.html
```

Once a host id is typed, the verb candidates narrow to that host's class —
only the verbs the chosen host actually supports:

```{raw} html
:file: ../../_static/generated/termynal/complete-host-verbs.html
```

See {doc}`../../architecture/subsystems/hosts` for how completion is
synthesized from the same class-scoped mechanism as the verbs themselves.

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

`put` takes an optional `mode` -- the permission bits the uploaded files
should end up with:

```python
res = await host.put(
    src_files=[Path("app.bin")],
    dest_dir=Path("/opt/bin"),
    mode=0o755,
)
```

From the CLI the same value is written as an octal string, which is **always**
read base-8 -- `--mode 755` means `0o755`, never decimal 755:

```console
$ otto host web1 put ./app.bin /opt/bin --mode 755
```

The mode is applied after the bytes land, in a single batched `chmod` covering
the whole transfer.  If the transfer succeeds but the `chmod` fails, those
files are reported as errors that still carry their destination path -- so a
caller can tell "never arrived" apart from "arrived with the wrong
permissions".

```{note}
`put` and `get` are available on all host types, with per-class semantics:
{class}`~otto.host.local_host.LocalHost` copies files within the local
filesystem, {class}`~otto.host.unix_host.UnixHost` transfers between the
local machine and the remote host, and `EmbeddedHost` provides its own
console/tftp transfer path; see {doc}`embedded`.

`mode` follows the same split: it is honoured by
{class}`~otto.host.local_host.LocalHost`, every
{class}`~otto.host.unix_host.UnixHost` backend (`scp`, `sftp`, `ftp`, `nc`),
and `DockerContainerHost`.  `EmbeddedHost` has no permission model -- a FAT or
LittleFS device has no permission bits to set -- so passing `mode` to one
fails before any bytes move rather than being silently ignored.
```

## Exit codes

Every `otto host <name> <verb>` invocation derives its exit code from the
verb's returned {class}`~otto.result.Result` family, via `Result.exit_code`.
Command results are ssh-like: the shell's retcode when the command ran,
255 when it never ran.  (`exec` is Python-only — it is not a CLI verb,
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

## Extending

Hosts are otto's most extensible area: register new connection or
transfer backends ({doc}`extending-backends`) and bring up embedded
targets otto doesn't ship support for ({doc}`extending-embedded`).
The registry machinery behind every seam is described in
{doc}`../../architecture/subsystems/extension-points`.

```{toctree}
:hidden:

commands/index
capabilities
connections
configuration
embedded
os-profiles
extending-backends
extending-embedded
```
