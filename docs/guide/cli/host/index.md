# otto host
`otto host` provides direct access to host operations from the command line --
running commands, transferring files, opening an interactive shell, and invoking
host capabilities -- without writing a test suite or instruction.

Hosts are *defined* in `lab.json` — see {doc}`../../configuration/lab-config`.
This section is about *using* them.
```{raw} html
:file: ../../../_static/generated/termynal/help-host.html
```
## Syntax

The host ID comes before the subcommand, so all host-level options apply to every
action:

```text
otto host <host_id> <command> [ARGS...] [OPTIONS]
```
Run commands, transfer files, log in, and invoke capability verbs on lab hosts.

```text
otto host <HOST_ID> run [--sudo] [--timeout SECS] <COMMANDS...>
otto host <HOST_ID> put <SRC...> <DEST>
otto host <HOST_ID> get <SRC...> <DEST>
otto host <HOST_ID> login
otto host <HOST_ID> reboot [--hard] [--wait] [--timeout SECS]
otto host <HOST_ID> install [--stage-only]
otto host <HOST_ID> power [STATE]
otto host <HOST_ID> ls [PATH] [--all]
otto host --list-hosts
```

`otto host` is **not** scoped by any repo's `[project]` declaration: naming a
host explicitly reaches it wherever it is in the loaded lab, and the "Available
hosts" listing printed for an unknown id enumerates the whole lab for the same
reason.  Explicit targeting beats scoping — see
{ref}`project-scope`.

## The host verb model

Every `otto host` action is a **verb** on the host, and every verb — the four
core ones included — is synthesized from an `@cli_exposed` host method by the
same signature-driven mechanism. `run`, `put`, `get` and `login` are the ones
every host class carries; anything else is a **capability verb**, scoped to the
host's class, so `otto host <host_id> --help` lists exactly what the chosen host
supports and nothing more.

See {doc}`capabilities/index` for the capability families and which host types
expose them, {doc}`netcat` and {doc}`connections` for transport, and
{doc}`../../configuration/host-options` for per-host tuning. Authoring a verb of
your own is {doc}`../../../library/cli-exposed-verbs`.

## Subcommands

| Subcommand | Description |
| ---------- | ----------- |
| `run` | Execute one or more commands on the host |
| `put` | Upload local files to the host |
| `get` | Download files from the host |
| `login` | Open an interactive shell session on the host |
| `reboot` | Reboot the host (soft or hard power-cycle) |
| `shutdown` | Power off the host from its own shell |
| `power` | Turn the host on/off or toggle (requires a power controller) |
| `stage` | Stage products onto the host without installing |
| `install` | Stage then install products |
| `uninstall` | Uninstall products |
| `is-installed` | Exit 0 if all products are installed |
| `is-uninstalled` | Exit 0 if no products are installed |
| `cleanup` | Uninstall products, then remove dev tools and toolchain tools |
| `is-clean` | Exit 0 if no product, dev tool, or toolchain tool is present |
| `get-logs` | Retrieve product and debug logs into `logs/<host-id>/` |
| `get-product-logs` | Retrieve each product's logs into `logs/<host-id>/product/` |
| `get-debug-logs` | Fetch the host's `debug_log_globs` into `logs/<host-id>/debug/` |
| `install-tools` | Install the host's dev tools, and its toolchain tools with `--toolchain` |
| `install-dev-tools` | Stage then install every dev tool attached to the host |
| `uninstall-dev-tools` | Remove every dev tool attached to the host (best-effort) |
| `install-toolchain-tools` | Put each declared toolchain tool, renamed and chowned |
| `remove-toolchain-tools` | Remove every declared toolchain tool from the host |
| `exists` | Exit 0 if a path exists on the host |
| `ls` | List directory contents on the host |
| `glob` | Expand a pattern on the host and print the matching paths |
| `mkdir` | Create a directory on the host |
| `rm` | Remove a path on the host |
| `cp` | Copy a path on the host |
| `mv` | Move/rename a path on the host |
| `read-file` | Print a file's text contents |
| `write-file` | Write text to a file on the host |
| `probe` | Report the host's userland capabilities and print the `userland_options` pin |

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
:file: ../../../_static/generated/termynal/complete-host-ids.html
```

Once a host id is typed, the verb candidates narrow to that host's class —
only the verbs the chosen host actually supports:

```{raw} html
:file: ../../../_static/generated/termynal/complete-host-verbs.html
```

See {doc}`../../../architecture/subsystems/hosts` for how completion is
synthesized from the same class-scoped mechanism as the verbs themselves.
## Host-level options

| Option | Description |
| ------ | ----------- |
| `HOST_ID` (argument) | Host ID to operate on (see `--list-hosts`) |
| `--hop HOST_ID` | Route through an intermediate SSH hop host |
| `--term TYPE` | Override the terminal protocol for this session |
| `--transfer TYPE` | Override the file transfer protocol for this session |
| `--list-hosts` | List all available host IDs and exit |
## Dry run

Like all otto commands, `--dry-run` (or `-n`) previews what would happen without
executing commands or transferring files:

```bash
otto --lab my_lab --dry-run host router1 run "make install"
```

Add `--probe` to also learn whether the host is up before committing to a real
run.  That opens a connection and nothing else — no command is issued over it,
and an unreachable host is reported rather than treated as a failure.  See
{doc}`../index` for the full rules.
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

## Beyond the CLI

Every verb here is a method on {class}`~otto.host.host.BaseHost` first — calling
them from an instruction or a suite is {doc}`../../../library/writing-instructions`.
Hosts are also otto's most extensible area: register new connection or transfer
backends ({doc}`../../../library/extending-backends`) and bring up embedded
targets otto doesn't ship support for
({doc}`../../../library/extending-embedded`). The registry machinery behind every
seam is described in
{doc}`../../../architecture/subsystems/extension-points`.

```{toctree}
:caption: Subcommands
:hidden:

run
put
get
login
```

```{toctree}
:caption: Topics
:hidden:

connections
netcat
embedded
capabilities/index
```
