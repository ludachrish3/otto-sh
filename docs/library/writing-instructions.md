# Writing instructions

An **instruction** is an async Python function that otto exposes as an
`otto run` subcommand. This page is how to write one. For invoking the ones
you already have, see {doc}`../guide/cli/run/index`.

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
  ({doc}`extending-cli`); `None` renders nothing. `async` is enforced, not
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
{doc}`Extension points <../architecture/subsystems/extension-points>` for
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
repo declared one.  `get_host()` is deliberately unscoped and reaches any host.
See [The fleet of interest](../guide/cli/run/defaults.md#the-fleet-of-interest).

For fan-out across the lab — running the same command or async
operation on every host concurrently — use
{func}`~otto.config.fleet.run_on_all_hosts` or
{func}`~otto.config.fleet.do_for_all_hosts`.  These helpers
apply anywhere you have an async context (instructions, suite fixtures,
monitors, ad-hoc scripts) and are documented in full on the
[async patterns page](async-patterns.md).

Two properties of the fleet helpers to keep in mind:

- **Fleet membership.**  The built-in `local` host (the machine otto
  itself runs on, present in every lab) and Docker container hosts are
  excluded by default — a lab-wide sweep should never silently operate
  on the runner or on containers.  Opt in with `include_local=True` (on
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
[async patterns page](async-patterns.md)
for the lab-wide dispatch pattern.

## Sharing repo-wide options across instructions and suites

When several instructions — and often several test suites too — need the
same CLI flags (device type, lab environment, etc.), define a shared base
**options class** (with `@options`) in any module listed in your `init`
setting — a `libs` path like `pylib/` is one common choice. See
{doc}`options-classes` for the full treatment. The *same* class can be inherited by

- a suite's inner `Options` class (expanded during auto-registration), and
- an instruction's `options=` class (expanded by
  `@instruction(options=...)`).

Suite and instruction option classes are **independent but
compatible** — they can be completely different, inherit from a common
base (the recommended posture for repo-wide flags), or be literally the
same class. Nothing in the machinery forces any of these.

See also
[Inheriting shared options](suite-recipes.md#inheriting-shared-options)
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
from typing import Annotated

import typer

from otto import options
from my_instructions.options import RepoOptions
from otto.suite import OttoSuite


@options
class _Options(RepoOptions):  # inherits --device-type, --lab-env
    firmware: Annotated[str, typer.Option()] = "latest"


class TestDevice(OttoSuite[_Options]):
    Options = _Options

    async def test_version(self, suite_options: _Options) -> None:
        self.logger.info(
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

Existing instructions that use only inline parameters continue to work
unchanged — the ``options=`` parameter is entirely opt-in.

## The override ladder

Three rungs, and the question they each answer:

| Rung | Answers | Subclass |
| ---- | ------- | -------- |
| `Product` / `DevTool` | How does *this artifact* install on a host? | `Product`, `FileProduct`, `DevTool` |
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

from otto.project import ProjectActions, register_project_actions
from otto.result import Result
from otto.utils import Status


@register_project_actions
class WidgetActions(ProjectActions):
    """Widget's lab lifecycle: the defaults, plus a license every host needs."""

    async def install(self) -> Result:
        pushed = await self._push_license()
        if not pushed.is_ok:
            return pushed
        return await super().install()

    async def _push_license(self) -> Result:
        for host in self.ctx.all_hosts():
            result = await host.put(Path("licenses/widget.lic"), Path("/etc/widget"))
            if not result.is_ok:
                return result
        return Result(Status.Success)
```

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
hand with a plain `OttoContext` raises `TypeError`, deliberately: no default
body spells `owner=` any more, so that object would walk the whole union *and*
call every host verb with `owner=None`, which the host layer reads as **every**
owner's products. Its `cleanup()` would uninstall the neighbours' products and
report success.
```

A repo registers **at most one** `ProjectActions`; a second registration from
the same repo fails loud. Different repos each registering their own is the
intended composition, not a collision. A repo that registers nothing gets
`ProjectActions` itself.

Some things are deliberately *not* per-repo, and the defaults refuse them:
**debug logs** and **toolchain tools** belong to a host, not to a repo — N
repos each sweeping the same host's debug logs means N transfers each
overwriting the last, and one toolchain serves every owner on a host, so a repo
removing it would take its neighbours' tooling with it. **Impairments and
tunnels** are one step further out again: they belong to the lab rather than to
any single host, and nothing in a repo's products or dev tools put them there.
All of them are performed once, by the layer above.

## Converging fixtures in suites

A suite that needs the lab in a known state requests a fixture instead of
scripting the walk:

```python
from otto.suite import OttoSuite


class TestWidget(OttoSuite):
    async def test_service_answers(self, ensure_installed) -> None:
        """Runs against a fully-installed lab, whatever state the last test left."""
        self.logger.info("lab is installed")
```

The three fixtures are `ensure_installed`, `ensure_uninstalled` and
`ensure_clean`. Each is a one-line wrapper over the same converge functions the
CLI calls, so a fixture and `otto run install --ensure` cannot diverge. See
{doc}`../guide/cli/test/index` for the full fixture list.

- **Function-scoped**: the guarantee is per test *case*. When the state already
  holds, the cost is one probe of it — but not the same probe for all three.
  `ensure_installed` and `ensure_uninstalled` ask `status()`, which counts the
  *counted* repos' products. `ensure_clean` asks `is_clean()` instead, and that
  is much the heavier sweep: **every** repo is asked (not only the counted
  ones), dev tools are probed alongside products, each host is asked once more
  for `toolchain_tools_absent()`, every impairable link's netem state is read,
  and the lab is scanned for tunnel processes.
- **`ensure_installed` recovers a PARTIAL lab** by tearing it down and
  installing fresh — installing over remnants is how a lab got into that state
  in the first place.
- **`ensure_clean` is stronger than `ensure_uninstalled`**: dev tools,
  toolchain tools, impairments and tunnels are not products, so an
  uninstalled-but-tooled — or merely impaired — lab still gets cleaned.
- **`is_clean()` answers for exactly what `cleanup` removes**, which is the
  rule that keeps the two from drifting: a lab dirty only in tunnels is not
  clean, and `otto run cleanup` is what the fixture runs to fix it. It cuts the
  other way too — a *foreign* qdisc leaves the lab "clean", because `cleanup`
  provably will not remove one, and reporting otherwise would send every
  `ensure_clean` into a cleanup that cannot change the answer.
- **A state that could not be read is an error, never an answer.** A host that
  did not respond to the toolchain probe, a link whose impairment could not be
  read, a tunnel scan that reached nobody: each raises out of `is_clean()`
  rather than being counted clean (a fact nobody measured) or dirty (a converge
  into a cleanup on the same non-fact). The exception proves the rule, on every
  one of those axes: if the sweep *did* read one link carrying netem, or find
  one tunnel, before it ran out of hosts, the lab is dirty and says so — an
  unreachable host cannot unmake an answer otto already has. That holds within
  an axis. Once one of them cannot answer, the axes after it are not read at
  all, because the only thing they could do is strengthen a verdict that is
  already unavailable.
- **`otto run status --full` asks the same probes and never raises.** A
  display's duty on a state nobody could read is the opposite of a converge's,
  so it prints an `unknown` cell where `is_clean()` refuses to answer. Both
  come off the same probe, which is what keeps them from disagreeing; see
  [Reading `status`](../guide/cli/run/defaults.md#reading-status).
- **`status()` never moves for either of them.** An impaired link and a live
  tunnel are lab infrastructure; the tri-state install answer stays a count of
  products, so a lab under test with 200 ms of injected delay still reads
  INSTALLED.
- **Failure errors the test, naming the host — never a skip.** A host that
  cannot be brought to the state a test requires fails that test
  ({class}`~otto.errors.EnsureStateError`), rather than quietly removing it from
  the run.

## The collision error

A repo instruction may not take one of the six first-party names. Otto refuses
it while that repo's init modules are being imported:

```text
repo 'widget' defines instruction 'install', which is a first-party default.
Override lab behavior by registering a ProjectActions subclass instead (see
docs/guide/cli/run/defaults.md), or rename the instruction.
```

If you are upgrading a repo that already has an `install` instruction, the
migration is one of two moves:

1. **It really is your repo's install.** Move its body into a
   `ProjectActions.install` override, as above, and delete the instruction.
   Every surface — the command, scripts, suites, the `ensure_installed` fixture
   — picks the change up at once.
2. **It is unrelated** (`install` meaning something else entirely). Rename it;
   `otto run install-firmware` collides with nothing.

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
console/tftp transfer path; see {doc}`../guide/cli/host/embedded`.

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
pass. See {doc}`../guide/cli/host/run` for the CLI-side view of the same
output.
