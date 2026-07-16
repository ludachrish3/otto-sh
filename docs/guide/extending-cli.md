# Extending the otto CLI

Beyond instructions (`otto run ...`) and suites (`otto test ...`), otto lets a
project register entirely new **top-level commands** — a single leaf command
or a whole command group — that show up in `otto --help` and tab completion
next to the built-ins (`run`, `test`, `monitor`, `host`, ...). First-party and
third-party commands travel the exact same path:
{func}`~otto.cli.registry.register_cli_command` and the
{func}`~otto.cli.registry.cli_command` decorator built on top of it. otto's own
eight subcommand groups register through this same function — see
`otto/cli/builtin_commands.py` for the reference call sites this page mirrors.

## Registering a top-level command

Decorate an async function with `@cli_command()` in a module listed in your
settings file's `init` field (see {doc}`setup/repo-setup`). The ergonomics
deliberately match `@instruction()` (see {doc}`run/index`): an `OttoContext`-annotated
parameter is injected and hidden from the CLI, and an `options=` dataclass
expands into individual flags. Unlike `@instruction()`, `@cli_command()` takes
keyword arguments only (`options=`, `name=`, `help=`, `lab_free=`, `output_dir=`,
`gate=`) and does not forward extra positional or keyword arguments to Typer.

```python
from typing import Annotated

import typer

from otto import options
from otto.cli.registry import cli_command
from otto.context import OttoContext
from otto.result import Result
from otto.utils import Status


@options
class PingOptions:
    count: Annotated[int, typer.Option(help="Number of hosts to sample.")] = 1


@cli_command(options=PingOptions)
async def ping(ctx: OttoContext, opts: PingOptions) -> Result:
    """Ping the first `count` hosts in the lab and report status."""
    hosts = list(ctx.all_hosts())[: opts.count]
    for host in hosts:
        await host.run("true")
    return Result(Status.Success, msg=f"pinged {len(hosts)} host(s)")
```

```bash
otto --lab my_lab ping --count 3
otto ping --help
```

The command name defaults to the function name with underscores turned into
dashes (`send_report` &rarr; `send-report`); pass `name=` to `@cli_command()` to
override it. The one-line help shown in `otto --help` comes from `help=` if
given, otherwise the docstring's first line — read without importing the
module (see [Where registration happens](#where-registration-happens) below).

## Registering a command group

For more than one related subcommand, build a `typer.Typer` app and register
it directly with {func}`~otto.cli.registry.register_cli_command` — no
decorator needed, since a live `typer.Typer` *is* the loader:

```python
import typer

from otto.cli.registry import register_cli_command

mytool_app = typer.Typer(help="Project-specific device utilities.")


@mytool_app.command()
def flash(image: str) -> None:
    """Flash IMAGE onto the currently selected device."""
    ...


@mytool_app.command()
def erase() -> None:
    """Erase the currently selected device."""
    ...


register_cli_command("mytool", mytool_app, help="Project-specific device utilities.")
```

```bash
otto mytool flash --image build/out.bin
otto mytool erase
otto mytool --help
```

A `register_cli_command()` loader can be one of three things:

- a live `typer.Typer` app &mdash; treated as a **group** when it has more than
  one command, a callback, or sub-groups, so `otto mytool --help` lists
  subcommands. A single-command, callback-free, subgroup-free app instead
  **flattens** into a bare leaf under the registered name — exactly the rule
  Typer itself applies to a name-less `add_typer`, so `otto monitor --help`
  shows monitor's own `--live` / `--hosts` flags directly rather than hiding
  them behind a spurious nested `monitor` subcommand;
- a plain or `async` function &mdash; a **leaf** command, wrapped in a
  throwaway `Typer` the same way `@cli_command()`'s target is;
- a `"pkg.mod:attr"` string &mdash; resolved **lazily**, only when the command
  is actually dispatched or explicitly tab-completed. This is what every
  built-in uses (e.g. `register_cli_command("run", "otto.cli.run:run_app",
  ...)`) so that `otto --help` never imports `otto.cli.run`, `otto.cli.test`,
  or any other subcommand module it isn't showing the details of.

## Where registration happens

Registration must run **before** the root Typer group is consulted, which
means it belongs in a module listed in your `.otto/settings.toml` `init`
field (or a package pulled in transitively from one). {func}`otto.bootstrap.bootstrap`
is otto's composition root: it discovers your repos, then imports each
repo's `init` modules and test files, and runs before argv parsing for every
real invocation (see {func}`otto.cli.main.entry`). Bootstrap is idempotent —
repeated calls return the same cached result.

Bootstrap **contains** failures per module: if one `init` file raises on
import, that one file's exception is wrapped in a
{class}`~otto.bootstrap.BootstrapError` and collected rather than crashing the
process. `otto --help` still renders — with a framed warning line printed to
stderr for each failed module — so a single broken plugin file degrades help
output instead of bricking the CLI entirely:

```text
warning: repo /path/to/repo: failed to load my_broken_module: ImportError(...)
```

Real command dispatch is not so forgiving: {func}`~otto.cli.invoke.command_preamble`
re-checks bootstrap's result and fails loud —

```text
Cannot run commands while a repo fails to load (see warnings above).
```

— with exit code 1, before any subcommand body runs (the per-module `warning:`
lines above are printed once, at startup, by {func}`~otto.cli.main.entry`; the
dispatch preamble prints only this framed summary). The rule of thumb: a
broken `init` module never blocks *discovery* (`--help`, tab completion), but
it always blocks *execution*.

## Metadata

`register_cli_command()` and `@cli_command()` share the same keyword-only
metadata, mirrored on {class}`~otto.cli.registry.CommandSpec`:

| Keyword      | Default | Effect                                                                                                                                   |
|--------------|---------|-------------------------------------------------------------------------------------------------------------------------------------------|
| `lab_free`   | `False` | Skips lab bootstrap *and* CLI session setup (no banner, no logging init) for this command. Use for commands that never touch lab state. |
| `output_dir` | `True`  | Creates a per-invocation artifact directory under `--xdir` before the command body runs.                                                |
| `gate`       | `True`  | Runs the reservation gate before dispatch (ignored entirely when `lab_free=True`).                                                      |

All three are read lazily by the shared leaf-invoke preamble
({func}`~otto.cli.invoke.command_preamble`), which runs once per real
invocation, *after* argv parsing and *never* on a `--help` path — a
subcommand's `--help` exits during Click's parse step, before
`Command.invoke` is reached, so it can never create a spurious output
directory or trip the reservation gate.

The built-ins span the whole matrix — read them as worked examples
(`otto/cli/builtin_commands.py`):

- **`schema`** (`otto schema ...`) sets `lab_free=True, output_dir=False,
  gate=False` — JSON-Schema export never touches a lab, so nothing about lab
  or reservation state applies.
- **`cov`** and **`reservation`** set `output_dir=False, gate=False` but stay
  lab-aware (`lab_free` defaults to `False`) — they read lab state but write
  no per-invocation artifacts and gate nothing.
- **`monitor`** sets `gate=False` at the spec level, then gates *itself*,
  per-branch, inside the command body: reviewing a saved `<source>` reads a
  local file and never touches live hardware, so it's gate-exempt by design,
  while `--live` collection still evaluates the gate explicitly. This is the
  precedent to follow whenever a uniform `gate=True`/`gate=False` would be
  either too strict or too permissive for some of a command's branches —
  declare `gate=False` on the spec and, wherever the branch actually needs
  it, read `ctx.meta["otto_reservation"]` (a
  {class}`~otto.reservations.check.ReservationGate`, or `None` if none was
  built) and call its `.evaluate()` yourself — the same inline pattern
  {func}`~otto.cli.invoke.command_preamble` uses for `gate=True` commands.
- **`run`, `test`, `host`, `docker`** all keep the defaults (or override just
  `gate` for `docker`, which is `gate=False` because docker was never
  reservation-gated — the flag preserves that pre-existing behavior) —
  everything else takes the full lab-aware, output-dir-creating, gate-checked
  path.

## Collisions

Two commands registering the same name is a **hard failure at registration
time**, naming both modules:

```text
ValueError: CLI command 'mytool' is already registered by 'acme.cli'; second
registration from 'acme.other'. CLI command names cannot be overwritten; pick a
unique name.
```

Unlike the backend registries covered in {doc}`hosts/extending-backends` (term,
transfer, host classes, ...), which accept `overwrite=True` for a deliberate
replacement, **`register_cli_command()` has no `overwrite` parameter at
all** — there is deliberately no escape hatch for CLI commands. A user-facing
top-level command name is part of your CLI's surface area; silently letting a
second registration replace it would make `otto --help` and tab completion
depend on unpredictable init-module import order. If you need to intentionally
replace a built-in's behavior, give your command a different top-level name.

## Completion

Every registered command name appears automatically in `otto --help` and in
shell tab completion — there is nothing extra to wire up. Two paths feed this:

- **Slow path** (a real invocation): bootstrap runs, so the live
  {data}`~otto.cli.registry.CLI_COMMANDS` registry has every command from
  every loaded `init` module, first- and third-party alike.
- **Fast path** (shell completion, `otto <TAB>`): bootstrap is *skipped*
  entirely for latency — completion never executes arbitrary user code. A
  cache file records each third-party command's name, help text, and
  `lab_free` flag from the most recent slow-path run (built by
  `collect_cli_commands()` in `otto/config/completion_cache.py`) —
  plus, for a group, its subcommand tree (names, helps, option schemas), so
  `otto <your-group> <TAB>` completes children without importing your code.
  Built-in commands aren't cached — they re-register on every real
  invocation, so caching them would be redundant. On the fast path, otto
  serves stubs assembled purely from that cached data; a name only
  the live registry knows about (never seen by a completing shell before) is
  simply invisible until the next slow-path run refreshes the cache.

  One cost note for lazy `"pkg.mod:attr"` group loaders: serializing the
  subcommand tree imports that module during the *slow-path* cache refresh
  (never during completion itself). If the import fails, the cache degrades
  to the group's name and help — and the real dispatch error stays loud.

## Return values

Inside the command body, return whatever your logic produces. If it's a
`Result` (or `CommandResult`/`Results`), otto derives the process exit code
from it using the same polymorphic, ssh-like rules `otto host <name> <verb>`
uses — see [Exit codes](hosts/index.md#exit-codes) in the host guide for the
full table. A plain (non-`Result`) return value is printed as-is and the
process exits `0`.

## See also

- {doc}`run/index` — instructions (`otto run ...`), the closest sibling to a
  `@cli_command()` leaf
- {doc}`hosts/extending-backends` — the term/transfer backend registries, which
  share {class}`~otto.registry.Registry`'s engine but allow `overwrite=True`
  where CLI commands deliberately don't
- {doc}`setup/repo-setup` — the `init` field that makes registration modules load
- {doc}`../library/index` — using otto without the CLI at all
- {doc}`Extension points <../architecture/subsystems/extension-points>` — the
  registry machinery behind this and every other seam otto can be extended at
