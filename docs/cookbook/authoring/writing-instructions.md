# Writing instructions

An **instruction** is an async Python function that otto exposes as an
`otto run` subcommand. This page is how to write one. For invoking the ones
you already have, see {doc}`../../cli/run/index`.

## Defining an instruction

Decorate an async function with `@instruction()` in a module listed in your
settings file's `init` field:

```python
import logging
from typing import Annotated

import typer

from otto.cli.run import instruction
from otto.config import all_hosts

logger = logging.getLogger(__name__)


@instruction()
async def deploy(
    debug: Annotated[
        bool, typer.Option("--field/--debug", help="Use field or debug products.")
    ] = False,
):
    """Deploy the build to all hosts in the lab."""
    for host in all_hosts():
        result = await host.run(
            [
                "echo deploying...",
                "make install",
            ]
        )
        logger.info(f"{host.name}: {result[-1].status}")
```

The function:

- Must be `async` and return a `Result` (or `None`). A returned `Result`'s
  exit code is honored: a failing result exits the process non-zero, under
  the same "Return values" rules as any registered command
  ({doc}`../extending/extending-cli`); `None` renders nothing. `async` is enforced, not
  merely advised — a plain `def` raises `TypeError` at decoration, because
  only a coroutine reaches the lifecycle bridge that sweeps the instruction's
  hosts and turns an interrupt into a clean exit
- Must not *block* that bridge either. `async def` is necessary, not
  sufficient: the interrupt policy is delivered through the event loop, so a
  body that never yields to it — a bare `subprocess.run(...)`, a
  `time.sleep(...)` — is exactly as uninterruptible as a sync one, and Ctrl-C
  will appear to do nothing until it finishes. Lab work belongs in
  `await host.run(...)`; local blocking work belongs in
  {func}`asyncio.to_thread`. A body with nothing to await at all is fine
- Is imported at startup because the module is listed in `init`
- Gets its own `--help` page automatically from the docstring and type
  annotations

`@instruction()` registration is one seam among many; see
{doc}`Extension points <../../architecture/subsystems/extension-points>` for
the registry machinery behind this and every other way otto can be extended.

## Accessing hosts

Inside an instruction body, pull hosts out of the lab with the config
module helpers:

```python
import re
from otto.config import all_hosts, get_host

# Iterate (optionally narrowed by a regex FULLY matched against host ID)
for host in all_hosts():
    await host.run("uname -a")

for host in all_hosts(pattern=re.compile(r"router.*")):
    await host.run("show version")

# Fetch a specific host by ID
router = get_host("router1")
result = await router.run("show version")
```

`pattern` is `re.fullmatch`, never `re.search`: `router` selects the host whose
id is exactly `router`, so write `router.*` to match by prefix.  A pattern that
matches none of the hosts the run may walk raises
{class}`~otto.config.scope.EmptySelectionError` rather than iterating nothing.

`all_hosts()` walks the run's **fleet of interest** — the hosts the active
repos' `[project]` declarations admit — which is the whole loaded lab when no
repo declared one.  `get_host()` is unscoped and reaches any host.
See [The fleet of interest](../../cli/run/defaults.md#the-fleet-of-interest).

For fan-out across the lab — running the same command or async
operation on every host concurrently — use
{func}`~otto.config.fleet.run_on_all_hosts` or
{func}`~otto.config.fleet.do_for_all_hosts`.  These helpers
apply anywhere you have an async context (instructions, suite fixtures,
monitors, ad-hoc scripts) and are documented in full on the
[async patterns page](../async-patterns.md).

Two properties of the fleet helpers to keep in mind:

- **Fleet membership.**  The built-in `local` host (the machine otto
  itself runs on, present in every lab) and Docker container hosts are
  excluded by default.  Opt in with `include_local=True` (on
  `all_hosts()` and `do_for_all_hosts()`) or `include_containers=True`;
  `get_host("local")` always resolves the local host.
- **Failure isolation.**  `run_on_all_hosts()` and `do_for_all_hosts()`
  return a dict mapping each host ID to its result *or* to the exception
  that host raised (`asyncio.gather` with `return_exceptions=True`
  semantics), so one unreachable host never costs you the others'
  results.  Check values with `isinstance(value, BaseException)` before
  using them.

## File transfers

Instructions can transfer files to and from hosts via
{meth}`~otto.host.host.Host.put` and
{meth}`~otto.host.host.Host.get`.  See the
[async patterns page](../async-patterns.md)
for the lab-wide dispatch pattern.

## Sharing repo-wide options across instructions and suites

When several instructions — and often several test suites too — need the
same CLI flags (device type, lab environment, etc.), define a shared base
**options class** (with `@options`) in any importable module — a `libs`
path like `pylib/` is one common choice. See
{doc}`options-classes` for the full treatment. The *same* class can be inherited by

- a suite's inner `Options` class (expanded during auto-registration), and
- an instruction's `options=` class (expanded by
  `@instruction(options=...)`).

Suite and instruction option classes are **independent but
compatible** — they can be completely different, inherit from a common
base (the recommended posture for repo-wide flags), or be literally the
same class. Nothing in the machinery forces any of these.

See also
[Inheriting shared options](../suite-recipes.md#inheriting-shared-options)
in the suite recipes.

### 1. Define repo-wide options

```python
# pylib/my_instructions/options.py
from typing import Annotated

import typer

from otto import options


@options
class RepoOptions:
    device_type: Annotated[
        str,
        typer.Option(
            help="Type of device under test (e.g. 'router', 'switch').",
        ),
    ] = "router"

    lab_env: Annotated[
        str,
        typer.Option(
            help="Lab environment to target (e.g. 'staging', 'production').",
        ),
    ] = "staging"
```

### 2. Inherit and extend in each instruction

```python
# pylib/my_instructions/deploy.py
import logging
from typing import Annotated

import typer

from otto import options
from otto.cli.run import instruction

from .options import RepoOptions

logger = logging.getLogger(__name__)


@options
class _DeployOpts(RepoOptions):  # inherits --device-type, --lab-env
    debug: Annotated[
        bool,
        typer.Option(
            "--field/--debug",
            help="Use field or debug products.",
        ),
    ] = False


@instruction(options=_DeployOpts)
async def deploy(opts: _DeployOpts):
    """Deploy the build to all hosts in the lab."""
    logger.info(
        f"device_type={opts.device_type!r}  lab_env={opts.lab_env!r}  debug={opts.debug}",
    )
```

The ``opts`` parameter (you can name it anything) receives a fully
populated ``_DeployOpts`` instance.  All fields — inherited and local —
appear as flat CLI flags:

```bash
otto run deploy --help
# Shows: --device-type, --lab-env, --field/--debug
```

### 2b. Inherit the same base in a suite

A suite's inner ``Options`` class can inherit from the very same
``RepoOptions`` class, so ``otto test`` subcommands expose the same
repo-wide flags as ``otto run``:

```python
# tests/test_device.py
import logging
from typing import Annotated

import typer

from otto import options
from my_instructions.options import RepoOptions
from otto.suite import OttoSuite

logger = logging.getLogger(__name__)


@options
class _Options(RepoOptions):  # inherits --device-type, --lab-env
    firmware: Annotated[str, typer.Option()] = "latest"


class TestDevice(OttoSuite):
    Options = _Options

    async def test_version(self, suite_options: _Options) -> None:
        logger.info(
            f"device_type={suite_options.device_type!r} "
            f"lab_env={suite_options.lab_env!r} "
            f"firmware={suite_options.firmware!r}"
        )
```

Both `otto run deploy --help` and `otto test TestDevice --help` now
surface the same `--device-type` and `--lab-env` flags, sourced from a
single definition.

### 3. Mix with inline parameters

You can combine an ``options`` dataclass with regular inline parameters.
The dataclass fields and inline parameters all become CLI options:

```python
@instruction(options=_DeployOpts)
async def deploy(
    opts: _DeployOpts,
    verbose: Annotated[bool, typer.Option("--verbose/--quiet")] = False,
):
    if verbose:
        logger.info("Verbose mode enabled")
    ...
```

The ``options=`` parameter is optional; an instruction may use inline
parameters alone.

## The override ladder

Three rungs, and the question they each answer:

| Rung | Answers | Subclass |
| ---- | ------- | -------- |
| `Product` / `DevTool` | How does *this artifact* install on a host? | `Product`, `ShellProduct`, `DevTool` |
| Host class | How does *this family of machines* do it? | `UnixHost`, `EmbeddedHost`, … |
| `ProjectActions` | What does *this repo* do to the whole lab? | `ProjectActions` |

Climb only as far as the question goes. A product that installs with a
different command is a `Product` override; a host family whose debug logs come
out of journald overrides `get_debug_logs`; a repo that must push a license
before anything installs overrides `ProjectActions.install`.

Register the subclass from a module listed in your settings file's `init`
field — that import is what attributes the class to its repo:

```python
# pylib/widget_instructions/__init__.py  (listed in .otto/settings.toml [init])
from pathlib import Path

from otto.cli.run import instruction
from otto.project import InstallOptions, ProjectActions, register_project_actions
from otto.result import Result
from otto import Status


@register_project_actions
class WidgetActions(ProjectActions):
    """Widget's lab lifecycle: the defaults, plus a license every host needs."""

    @instruction(options=InstallOptions)
    async def install(self, opts: InstallOptions) -> Result:
        pushed = await self._push_license()
        if not pushed.is_ok:
            return pushed
        return await super().install(opts)

    async def _push_license(self) -> Result:
        for host in self.ctx.all_hosts():
            result = await host.put(Path("licenses/widget.lic"), Path("/etc/widget"))
            if not result.is_ok:
                return result
        return Result(Status.Success)
```

`install` is a **project instruction**, so the override is a decorated method
and it carries an options class — otto's own here, since this repo adds no flag
of its own. [Project instructions](#project-instructions) below is the full
declaration; `options=` and what a repo may and may not restate are its
subject.

Two things that example relies on:

- **`super()` keeps every default.** Override the one method that needs
  changing and call up for the rest; a subclass may also mix custom sequencing
  with the per-host verbs (`await host.install()` for chosen hosts) — nothing in
  the defaults is privileged.
- **`self.repo` and `self.ctx` are provided.** `self.repo.name` is the owner
  scope, and `self.ctx` is this repo's *view* of the live context —
  `self.ctx.all_hosts()` and `self.ctx.do_for_all_hosts(...)` are the fleet and
  its dispatch, already bounded to this repo's fleet of interest and already
  supplying `owner=` to the host verb. Subclass code spells neither. To
  dispatch a host verb that takes no owner, pass
  `self.ctx.do_for_all_hosts(verb, with_owner=False)`.

```{important}
**Let `actions_for` build it.** `otto.project.actions_for(repo, ctx)` is what
hands the instance `ctx.for_repo(repo.name)` — the view that both bounds the
fleet and supplies the `owner=`. Constructing `ProjectActions(repo, ctx)` by
hand with a plain `OttoContext` raises `TypeError`.
```

A repo registers **at most one** `ProjectActions`; a second registration from
the same repo fails loud. Different repos each registering their own is the
intended composition, not a collision. A repo that registers nothing gets
`ProjectActions` itself.

Some things are *not* per-repo, and the defaults refuse them: **debug logs**
and **toolchain tools** belong to a host, not to a repo, and **impairments and
tunnels** belong to the lab. All of them are performed once, by the layer
above the repo walk.

## Declaring lab state in suites

A suite that needs the lab in a known state marks it instead of scripting
the walk:

```python
import pytest

from otto.suite import OttoSuite


@pytest.mark.ensure("installed")
class TestWidget(OttoSuite):
    async def test_service_answers(self) -> None:
        """Runs against a fully-installed lab, whatever state the last test left."""
```

The steps are `installed`, `uninstalled` and `clean` (and `none`); a marker
on a test overrides the class's, and the path runs in the written order
before the body. Each step runs the same converge as `otto run install
--ensure`. The marker's full semantics are in
{doc}`writing-suites`; the bullets below are what each step does.

**Where a body's options come from under `otto test`.** There are no
project-instruction flags on `otto test`, so the fixture builds each repo's
options class itself: a field takes the suite's value when the suite's
`Options` class and the repo's options class inherit it from the **same
declaring class**, every other field takes its default, and pydantic validates
the instance — so a bad default fails the test naming the field rather than
installing something odd. A suite field that merely has the same name as a
repo field, such as `variant`, is not passed. A repo that wants a test to steer
its install promotes the field into the base its suites already inherit.

- **Function-scoped**: the guarantee is per test *case*. When the state already
  holds, the cost is one probe of it — but not the same probe for all three.
  `installed` and `uninstalled` ask `status()`, which counts the
  *counted* repos' products. `clean` asks `is_clean()` instead, and that
  is much the heavier sweep: **every** repo is asked (not only the counted
  ones), dev tools are probed alongside products, each host is asked once more
  for `toolchain_tools_absent()`, every impairable link's netem state is read,
  and the lab is scanned for tunnel processes.
- **`installed` recovers a PARTIAL lab** by tearing it down and
  installing fresh.
- **`clean` is stronger than `uninstalled`**: dev tools,
  toolchain tools, impairments and tunnels are not products, so an
  uninstalled-but-tooled — or merely impaired — lab still gets cleaned.
- **`is_clean()` answers for exactly what `cleanup` removes**: a lab dirty
  only in tunnels is not clean, and `otto run cleanup` is what the step runs
  to fix it. A *foreign* qdisc leaves the lab "clean", because `cleanup` will
  not remove one.
- **A state that could not be read is an error, never an answer.** A host that
  did not respond to the toolchain probe, a link whose impairment could not be
  read, a tunnel scan that reached nobody: each raises out of `is_clean()`
  rather than being counted clean or dirty. On every one of those axes, if the
  sweep *did* read one link carrying netem, or find one tunnel, before it ran
  out of hosts, the lab is dirty and says so — an unreachable host does not
  undo an answer otto already has. That holds within an axis. Once one of them
  cannot answer, the axes after it are not read at all.
- **`otto run status --full` asks the same probes and never raises.** It
  prints an `unknown` cell where `is_clean()` refuses to answer; see
  [Reading `status`](../../cli/run/defaults.md#reading-status).
- **`status()` never moves for either of them.** An impaired link and a live
  tunnel are lab infrastructure; the tri-state install answer stays a count of
  products, so a lab under test with 200 ms of injected delay still reads
  INSTALLED.
- **Failure errors the test, naming the host — never a skip.** A host that
  cannot be brought to the state a test requires fails that test
  ({class}`~otto.errors.EnsureStateError`), rather than quietly removing it from
  the run.

## Project instructions

Everything above this section is a **standalone instruction**: `@instruction`
on a free function, one body, belonging to one repo, its own `otto run`
subcommand. A **project instruction** is the other kind — `@instruction` on a
`ProjectActions` method. One name, one walk across the lab's repos, and one
body *per repo*; `otto run <name>` runs every applicable repo's body in
dependency order.

otto's six defaults — `install`, `uninstall`, `cleanup`, `get-logs`,
`install-tools` and `status` — are project instructions otto declares on
`ProjectActions` itself, before any repo's init module is imported. A repo
overrides one, or adds a project instruction of its own, by the same decorator:

```python
from typing import Annotated

import typer

from otto import Status, options
from otto.cli.run import instruction
from otto.project import InstallOptions, ProjectActions, register_project_actions
from otto.result import Result


@options
class WidgetInstallOpts(InstallOptions):  # MUST inherit the first-party class
    variant: Annotated[str, typer.Option(help="Firmware variant.")] = "field"


@options
class DeployOpts:  # a new name has no first-party base to inherit
    build: Annotated[str, typer.Option(help="Build to deploy.")] = "latest"


@register_project_actions
class WidgetActions(ProjectActions):
    @instruction(options=WidgetInstallOpts)  # overrides a first-party body
    async def install(self, opts: WidgetInstallOpts) -> Result:
        """Install widget's products, pinning the firmware variant."""
        return await super().install(opts)

    @instruction(
        options=DeployOpts,
        walk="forward",
        continue_on_failure=False,
        require_dependencies=True,
        help="Deploy every repo's build, dependencies first.",
    )
    async def deploy(self, opts: DeployOpts) -> Result:
        """Deploy widget's build."""
        ...  # your work, opts.build in hand
        return Result(Status.Success)
```

What the decorator on a method does differently:

- It registers into the **project-instruction table** under the name, instead
  of building a standalone Typer command. On a free function it builds that
  standalone command.
- The body is repo-scoped through `self.ctx` and `self.repo`, so nothing is
  handed to it beyond its options.
- The walk-shape keywords — `walk`, `continue_on_failure`,
  `require_dependencies`, `combine_results` and `render` — are fixed by the
  **first** declaration of the name. otto's six are fixed before a repo can
  speak; a repo restating one of them fails at init, naming the keyword. A repo
  declaring a new name sets them, and the next repo to declare that name
  inherits them. Each keyword's meaning is tabulated under [Your repo's flags on
  a default](../../cli/run/defaults.md#your-repos-flags-on-a-default).
- An override of a first-party name **must** pass an `options=` class that
  inherits the first-party class for that name
  (`InstallOptions` and its five siblings, all
  exported from `otto.project`); registration refuses anything else. A repo's
  own new name has no base and declares freely.

`otto run <name>` exposes the union of every registered body's fields, and each
body receives its own class. Which fields merge into one flag, what a
cross-repo collision looks like, and where a shared base class belongs are in
[One command, every repo's flags](../../cli/run/defaults.md#one-command-every-repos-flags).

## The collision error

A **standalone** instruction may not take a project instruction's name — any of
them, otto's six and a repo's own alike. Otto refuses it while that repo's init
modules are being imported:

```text
repo 'widget' defines instruction 'install', which is a project instruction.
Override lab behavior by declaring the method on a ProjectActions subclass
instead (see docs/cli/run/defaults.md), or rename the instruction.
```

A **method** declaration of the same name is the sanctioned override and passes;
it is the shape the section above shows.

If you are upgrading a repo that already has an `install` instruction, the
migration is one of two moves:

1. **It really is your repo's install.** Declare it as a method with
   `@instruction(options=...)` on your `ProjectActions` subclass, inheriting
   `InstallOptions`, and delete the standalone
   instruction. Every surface — the command, scripts, suites, the `installed`
   ensure step — picks the change up at once.
2. **It is unrelated** (`install` meaning something else entirely). Rename it;
   `otto run install-firmware` collides with nothing.

## From Python

The `otto host` subcommands map directly to methods on the
{class}`~otto.host.host.BaseHost` class. Everything `otto host` does from the CLI
can also be done inside instructions and test suites:

```{doctest}
>>> from asyncio import run
>>> from otto.host.local_host import LocalHost
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
console/tftp transfer path; see {doc}`../../cli/host/embedded`.

`mode` follows the same split: it is honoured by
{class}`~otto.host.local_host.LocalHost`, every
{class}`~otto.host.unix_host.UnixHost` backend (`scp`, `sftp`, `ftp`, `nc`),
and `DockerContainerHost`.  `EmbeddedHost` has no permission model -- a FAT or
LittleFS device has no permission bits to set -- so passing `mode` to one
fails before any bytes move rather than being silently ignored.
```

## Log modes

`host.run(...)` — and each per-command `ShellCommand` inside it — accepts a
`log` mode: `normal`, `quiet`, or `never`. It controls how that command's I/O
reaches the console and the log files.

- `normal` — the default: the command and its output appear on the console and
  in every log file.
- `quiet` — keeps the command's I/O off the console; it is still recorded in
  `verbose.log`.
- `never` — redacts the I/O from every sink.

Warnings and errors are never suppressed by the log mode, whichever one you
pass. See {doc}`../../cli/host/run` for the CLI-side view of the same
output.
