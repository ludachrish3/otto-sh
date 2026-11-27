# Extending the otto CLI

Beyond instructions (`otto run ...`) and tests (`otto test ...`), otto lets a
project register entirely new **top-level commands** — a single leaf command
or a whole command group — that show up in `otto --help` and tab completion
next to the built-ins (`run`, `test`, `monitor`, `host`, ...). First-party and
third-party commands travel the exact same path:
{func}`~otto.cli.registry.register_cli_command` and the
{func}`~otto.cli.registry.cli_command` decorator built on top of it.

## Registering a top-level command

Decorate an async function with `@cli_command()` in a module listed in your
settings file's `init` field (see {doc}`../configuration/settings`). The
ergonomics match `@instruction()` (see {doc}`../cli/run/index`): an
`OttoContext`-annotated parameter is injected and hidden from the CLI, and an
`options=` dataclass expands into individual flags. Unlike `@instruction()`,
`@cli_command()` takes keyword arguments only (`options=`, `name=`, `help=`,
`lab_free=`, `output_dir=`, `gate=`, `dry_run_preview=`, `accepts_options=`) and does not forward
extra positional or keyword arguments to Typer.

```python
from typing import Annotated

import typer

from otto import options
from otto.cli.registry import cli_command
from otto.context import OttoContext
from otto.result import Result
from otto import Status


@options
class PingOptions:
    count: Annotated[int, typer.Option(help="Number of hosts to sample.")] = 1


@cli_command(options=PingOptions)
async def ping(ctx: OttoContext, opts: PingOptions) -> Result:
    """Ping the first `count` hosts in the lab and report status."""
    hosts = list(ctx.all_hosts())[: opts.count]
    for host in hosts:
        await host.run("true")
    return Result(status=Status.Success, msg=f"pinged {len(hosts)} host(s)")
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
  **flattens** into a bare leaf under the registered name, so its flags
  appear directly under `otto <name> --help` with no nested subcommand;
- a plain or `async` function &mdash; a **leaf** command, wrapped in a
  throwaway `Typer` the same way `@cli_command()`'s target is;
- a `"pkg.mod:attr"` string &mdash; resolved **lazily**, only when the command
  is actually dispatched or explicitly tab-completed. Use it so `otto --help`
  never imports your module.

## The command lifecycle comes free

Registration is the only opt-in. Write commands as `async def`
functions — on your own Typer app or as bare-function loaders — and otto's
dispatch runs each invocation under the full command lifecycle: a cleanup
boundary (hosts opened during the command are closed when it exits), the
two-stage SIGINT/SIGTERM interrupt policy, and the bounded teardown deadline.
There is no decorator to remember and nothing to import from `otto.lifecycle`;
the leaf-invoke wrapper detects the coroutine a plain `async def` leaf
produces and bridges it through the policy. A sync command is invoked as-is,
with none of it. The edges to know about:

- **Group callbacks must be plain `def`.** Typer discards a group callback's
  return value, so an `async def` callback on a `typer.Typer` app (shared
  option plumbing) can never reach the bridge — otto rejects one loudly at
  registration-dispatch time rather than letting it silently do nothing.
- **`@instruction()` rejects every plain `def`.** The rule is re-applied
  when a leaf is invoked, so registering an `InstructionEntry` straight into
  `STANDALONE_INSTRUCTIONS`, hanging a command off `run_app` with `@run_app.command()`,
  or adding a sub-group with `add_typer` is refused the same way.
- **`@cli_command()` rejects a sync handler unless `lab_free=True`.** Note
  what `lab_free` actually means: otto will not load a lab, open a session or
  run the reservation gate for you. It is *not* a promise that the command
  touches no hosts — `otto monitor` is `lab_free=True` and calls
  `all_hosts()`. So read the exemption as "I take responsibility for the
  lifecycle myself", which is what monitor does; if you do not, write the
  command `async def` and let the bridge do it.
- **Other registration routes run a sync leaf as-is.** A sync leaf on a
  `typer.Typer` app loader, or a bare sync function passed to
  `register_cli_command()`, dispatches without the lifecycle.
- **`async def` is necessary, not sufficient.** The interrupt policy is
  delivered through the event loop, so a command body that blocks it never
  sees Ctrl-C — put local blocking work in {func}`asyncio.to_thread`.
- An `async def` command driven *outside* otto's dispatch (for example, a
  `CliRunner` test invoking your Typer app directly) fails visibly — the
  command body never runs and Python warns about the un-awaited coroutine —
  instead of silently running without the policy. Drive tests through the
  registered command, or await the function yourself.

## Where registration happens

Registration must run **before** the root Typer group is consulted, which
means it belongs in a module listed in your `.otto/settings.toml` `init`
field (or a package pulled in transitively from one). {func}`otto.bootstrap.bootstrap`
is otto's composition root: it discovers your projects, then imports each
project's `init` modules, and runs before argv parsing for every real invocation
(see {func}`otto.cli.main.entry`). Test files are not a place to register a
command: they load only inside `otto test`'s pytest session, and a
`register_cli_command()` made from one is refused with
{class}`~otto.registry.RegistrationRefused`. Bootstrap is idempotent —
repeated calls return the same cached result.

Bootstrap **contains** failures per module: if one `init` file raises on
import, that one file's exception is wrapped in a
{class}`~otto.bootstrap.BootstrapError` and collected rather than crashing the
process. `otto --help` still renders — with a framed warning line printed to
stderr for each failed module — so a single broken plugin file degrades help
output instead of bricking the CLI entirely:

```text
warning: project root /path/to/project: failed to load my_broken_module: ImportError(...)
```

Real command dispatch is not so forgiving: {func}`~otto.cli.invoke.command_preamble`
re-checks bootstrap's result and fails loud —

```text
Cannot run commands while a project fails to load (see warnings above).
```

— with exit code 1, before any subcommand body runs (the per-module `warning:`
lines above are printed once, at startup, by {func}`~otto.cli.main.entry`; the
dispatch preamble prints only this framed summary). The rule of thumb: a
broken `init` module never blocks *discovery* (`--help`, tab completion), but
it always blocks *execution*.

## Metadata

`register_cli_command()` takes keyword-only metadata, mirrored on
{class}`~otto.cli.registry.CommandSpec`. `@cli_command()` accepts `lab_free`,
`output_dir`, `gate`, `dry_run_preview` and `accepts_options`, but not `async_leaves` or
`overwrite`, which only `register_cli_command()` takes. The registering module
is not metadata you pass: the engine records it, and
`CLI_COMMANDS.origin(name=name)` reads it back. The first three rows are the ones
most commands set:

| Keyword      | Default | Effect                                                                                                                                   |
|--------------|---------|-------------------------------------------------------------------------------------------------------------------------------------------|
| `lab_free`   | `False` | Skips lab bootstrap *and* CLI session setup (no banner, no logging init) for this command. Use for commands that never touch lab state. |
| `output_dir` | `True`  | Creates a per-invocation artifact directory under `--xdir` before the command body runs.                                                |
| `gate`       | `True`  | Runs the reservation gate before dispatch (ignored entirely when `lab_free=True`).                                                      |
| `dry_run_preview` | `False` | Under `--dry-run`, `False` stops the invocation after validation and prints the would-run block, so a command written without dry runs in mind never reaches a device. `True` runs the body, which must short-circuit at its own `is_dry_run()` branch (the shape `link` and `tunnel` use). One leaf can opt in alone with `cli_exposed(dry_run_preview=True)`. |
| `accepts_options` | `None` | Whether the command offers the flags of options classes a project registers for it. `None` answers by the lab: a command that uses the lab accepts, a `lab_free` one does not. See [A project's options on your command](#a-projects-options-on-your-command). |
| `async_leaves` | `False` | `register_cli_command()` only. Every leaf under the command must be `async def`; otto refuses a sync leaf when it is invoked. `run` sets it. |

The first three take effect once per real invocation, *after* argv parsing and
*never* on a `--help` path.

The built-ins span the whole matrix — read them as worked examples
(`otto/cli/builtin_commands.py`):

- **`schema`** (`otto schema ...`) sets `lab_free=True, output_dir=False,
  gate=False` — JSON-Schema export never touches a lab, so nothing about lab
  or reservation state applies.
- **`reservation`** sets `output_dir=False, gate=False` but stays lab-aware
  (`lab_free` defaults to `False`): it reads lab state, writes no
  per-invocation artifacts, and *is* the gate, so it gates nothing.
- **`cov`** and **`docker`** keep the default `gate=True`: a registered group
  is gated, and each leaf that contacts a lab host runs the reservation gate
  first ({doc}`../cli/reservation/index` lists which leaves). A leaf that loads
  a lab but contacts no host opts out on its own, by setting
  `__cli_gate__ = False` on its callback right after the function
  (`otto cov report`, `otto docker use-cases`); the constant is
  {data}`~otto.cli.invoke.GATE_ATTR`.
- **`monitor`** sets `gate=False` and `lab_free=True` at the spec level, then
  opens its lab *itself*, per-branch, inside the command body: reviewing a saved
  `<source>` reads a local file and never touches live hardware, so it loads no
  lab and runs no gate, while `--live` calls
  {func}`~otto.cli.invoke.open_live_lab` (`gate=True`), the same live-lab
  opening {func}`~otto.cli.invoke.command_preamble` runs for every other
  command (session, lab, the refusals, the reservation gate, in that order).
  This is the precedent to follow whenever a lab-free group has one live
  branch: declare `lab_free=True, gate=False` on the spec and call
  `open_live_lab(ctx, command_spec(ctx), gate=True)`
  ({func}`~otto.cli.invoke.command_spec` reads the registered spec) where the
  branch needs the lab. Do not hand-call the gate's `.evaluate()` instead: that
  skips the refusals the opening runs before it.
- **`run`, `test`, `host`** keep the defaults: the full lab-aware,
  output-dir-creating, gate-checked path.

## A project's options on your command

A project can register an options class for your command, by naming it in
`verbs=` (`"mytool"`, or `"mytool flash"` for one command beneath it;
[Naming commands](../setup/options-advanced.md#naming-commands)), the way it
registers one for `otto run install`.

**Which commands accept them.** A command that uses the lab accepts
registered options without being asked, and a `lab_free=True` command does
not. This is decided for each command, not for its group: a command marked
lab-free beneath a lab-bound group (`__cli_lab_free__ = True` on its
function) does not accept, though its group does. To say otherwise:

- for the whole registration, pass `accepts_options=True` or `False` to
  `register_cli_command()` or `@cli_command()`;
- for one command beneath a group, set `__cli_accepts_options__` to `True`
  or `False` on its function, right after the function, as with
  `__cli_gate__`:

```python
@mytool_app.command()
def version() -> None:
    """Print the tool's version."""
    ...


version.__cli_accepts_options__ = False  # no project flags on `otto mytool version`
```

The marker counts beneath a group only. For a top-level command (a
function, or an app that flattens into its one command), the registration
decides, and a marker that disagrees with it is refused when the command is
resolved, with a message that says to set `accepts_options=` on the
registration.

**What your command's users then get**, with no code of yours:

- the flags of every class that covers the command, in its `--help` and in
  TAB;
- the values validated before your body runs: a bad value is a usage error,
  exit 2;
- under `otto -n`, the values in the dry-run block, a secret field masked
  ({doc}`../cli/dry-run`);
- the values in your body and in anything it calls, through
  `otto.get_options(Cls)`; your function's own parameters are unchanged.

TAB completes such a flag in every shell. In bash it also completes the
flag's value when the field has fixed choices (an `Enum` type) or uses one of
otto's own completion sources.

**Your parameters share a namespace with those flags.** A project's field
named or spelled like one of your command's parameters is an error on that
command ([One flag per name on a command](../setup/options-advanced.md#one-flag-per-name-on-a-command)
has the details). Adding a parameter to a command that projects already use
can therefore break a project that spells the same name.

**A group's own callback cannot read them.** It runs before its command's
flags are parsed. Your registration decides what a read there does, not the
command that follows: when the registration accepts registered options,
`otto.get_options` in any of its group callbacks raises
{class}`~otto.params.OptionsNotAvailableError`, even when that command's
`__cli_accepts_options__` is `False`; when the registration does not accept,
the read returns defaults, even when that command's marker is `True`. A
group invoked with no command (`typer.Typer(invoke_without_command=True)`)
offers no registered flags, and its callback reads defaults.

## Collisions

Two commands registering the same name is a **hard failure at registration
time**, naming both modules:

```text
DuplicateRegistration: CLI command 'mytool' is already registered by
'acme.cli'; second registration from 'acme.other'. Pass overwrite=True to
replace it deliberately.
```

A deliberate replacement passes `overwrite=True`, as for every registry in
{doc}`extending-backends`. Two projects that both do so for one name get an
order-dependent winner — whichever init module runs last — and that is the
explicit choice each of them made. Root `otto --help`, tab completion and
dispatch serve the replacement from then on.

## Completion

Every registered command name appears automatically in `otto --help` and in
shell tab completion — there is nothing extra to wire up. Two paths feed this:

- **Full path** (any invocation that bootstraps): the live
  {data}`~otto.cli.registry.CLI_COMMANDS` registry has every command from
  every loaded `init` module, first- and third-party alike.
- **Fast path** (shell completion, `otto <TAB>`): bootstrap is *skipped*
  entirely — completion never executes arbitrary user code. A cache file
  records each third-party command's name, help text, and `lab_free` flag
  from the most recent cache rebuild — plus, for a group, its subcommand tree
  (names, helps, option schemas), so `otto <your-group> <TAB>` completes
  children without importing your code.
  Built-in commands aren't cached — they re-register on every real
  invocation. On the fast path, otto serves stubs assembled purely from that
  cached data; a name only the live registry knows about is invisible until
  the cache is rebuilt. That normally happens on its own: adding the command
  edits an init module the cache is keyed on, so the next TAB or root
  `otto --help` rebuilds it. Ordinary commands never do
  ({doc}`../architecture/subsystems/completion-cache`).

  One cost note for lazy `"pkg.mod:attr"` group loaders: serializing the
  subcommand tree imports that module during a cache rebuild (never when
  completion is answered from the cache). If the import fails, the cache degrades
  to the group's name and help — and the real dispatch error stays loud.

(cli-return-values)=
## Return values

Inside the command body, return whatever your logic produces. If it's a
`Result` (or `CommandResult`/`Results`), otto derives the process exit code
from it using the same polymorphic, ssh-like rules `otto host <name> <verb>`
uses — see [Exit codes](../cli/host/index.md#exit-codes) in the host guide for the
full table; a failing result exits the process non-zero. A `bool`
return is printed and sets the exit code (`0` for `True`, `1` for `False`), so
a predicate command works in `if otto ...`; any other plain (non-`Result`)
return value is printed as-is and the process exits `0`;
a `list` that carries a Rich `Table` or `Text` is printed one item per
line, so a renderable interleaved with plain strings renders in order
rather than as one pretty-printed Python object. A value whose text is its
whole report opts in with {class}`~otto.result.CliReport`: give its class
an `__otto_cli_report__()` method returning `str(self)`, and the CLI prints
exactly that text, as plain text, never Rich's dump of its fields or its
markup. Defining `__str__` alone does not opt in.
Returning `None` renders nothing — a side-effect-only command stays silent
unless it prints its own output. The same rules apply to registered
commands and instructions.

## See also

- {doc}`../cli/run/index` — instructions (`otto run ...`), the closest sibling to a
  `@cli_command()` leaf
- {doc}`extending-backends` — the term/transfer backend registries, which
  share {class}`~otto.registry.Registry`'s engine
- {doc}`../configuration/settings` — the `init` field that makes registration modules load
- {doc}`python-library` — using otto without the CLI at all
- {doc}`Extension points <../architecture/subsystems/extension-points>` — the
  registry machinery behind this and every other seam otto can be extended at
