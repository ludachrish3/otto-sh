# otto run

`otto run` executes **instructions** -- async functions that have full access
to the lab's hosts.  Each instruction becomes its own subcommand with typed
CLI options.

## Defining an instruction

Decorate an async function with `@instruction()` in a module listed in your
settings file's `init` field:

```python
from typing import Annotated

import typer

from otto.cli.run import instruction
from otto.configmodule.configmodule import all_hosts
from otto.logger import getOttoLogger

logger = getOttoLogger()


@instruction()
async def deploy(
    debug: Annotated[bool, typer.Option("--field/--debug",
        help="Use field or debug products.")] = False,
):
    """Deploy the build to all hosts in the lab."""
    for host in all_hosts():
        result = await host.run([
            "echo deploying...",
            "make install",
        ])
        logger.info(f"{host.name}: {result.statuses[-1].status}")
```

The function:

- Must be `async` and return a `CommandStatus` (or `None`)
- Is imported at startup because the module is listed in `init`
- Gets its own `--help` page automatically from the docstring and type
  annotations

## Running instructions

```bash
otto --lab my_lab run deploy                # run with defaults
otto --lab my_lab run deploy --debug        # pass a flag
otto run --list-instructions                # see all available instructions
```

## Accessing hosts

Inside an instruction body, pull hosts out of the lab with the config
module helpers:

```python
import re
from otto.configmodule.configmodule import all_hosts, get_host

# Iterate (optionally filtered by a regex on host ID)
for host in all_hosts():
    await host.run("uname -a")

for host in all_hosts(pattern=re.compile(r"router")):
    await host.run("show version")

# Fetch a specific host by ID
router = get_host("router1")
result = await router.run("show version")
```

For fan-out across the lab — running the same command or async
operation on every host concurrently — use
{func}`~otto.configmodule.configmodule.run_on_all_hosts` or
{func}`~otto.configmodule.configmodule.do_for_all_hosts`.  These helpers
apply anywhere you have an async context (instructions, suite fixtures,
monitors, ad-hoc scripts) and are documented in full on the
[async patterns cookbook page](../cookbook/async-patterns.md).

## Logging and artifacts

Every `otto run` invocation creates an output directory under `--xdir`:

```text
<xdir>/run/<instruction_name>/<timestamp>/
```

Use `logger.output_dir` to write artifacts there:

```python
output_file = logger.output_dir / "results.json"
```

## File transfers

Instructions can transfer files to and from hosts via
{meth}`~otto.host.host.Host.put` and
{meth}`~otto.host.host.Host.get`.  See the
[async patterns cookbook page](../cookbook/async-patterns.md)
for the lab-wide dispatch pattern.

## Sharing repo-wide options across instructions and suites

When several instructions — and often several test suites too — need the
same CLI flags (device type, lab environment, etc.), define a shared
base dataclass in your pylib. The *same* dataclass can be inherited by

- a suite's inner `Options` class (expanded by `@register_suite()`), and
- an instruction's `options=` dataclass (expanded by
  `@instruction(options=...)`).

Suite and instruction option dataclasses are **independent but
compatible** — they can be completely different, inherit from a common
base (the recommended posture for repo-wide flags), or be literally the
same class. Nothing in the machinery forces any of these.

See also
[Inheriting shared options](../cookbook/suite-recipes.md#inheriting-shared-options)
in the suite cookbook.

### 1. Define repo-wide options

```python
# pylib/my_instructions/options.py
from dataclasses import dataclass
from typing import Annotated

import typer


@dataclass
class RepoOptions:
    device_type: Annotated[str, typer.Option(
        help="Type of device under test (e.g. 'router', 'switch').",
    )] = "router"

    lab_env: Annotated[str, typer.Option(
        help="Lab environment to target (e.g. 'staging', 'production').",
    )] = "staging"
```

### 2. Inherit and extend in each instruction

```python
# pylib/my_instructions/deploy.py
from dataclasses import dataclass
from typing import Annotated

import typer

from otto.cli.run import instruction
from otto.logger import getOttoLogger

from .options import RepoOptions

logger = getOttoLogger()


@dataclass
class _DeployOpts(RepoOptions):                     # inherits --device-type, --lab-env
    debug: Annotated[bool, typer.Option(
        "--field/--debug",
        help="Use field or debug products.",
    )] = False


@instruction(options=_DeployOpts)
async def deploy(opts: _DeployOpts):
    """Deploy the build to all hosts in the lab."""
    logger.info(
        f"device_type={opts.device_type!r}  "
        f"lab_env={opts.lab_env!r}  "
        f"debug={opts.debug}",
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
``RepoOptions`` dataclass, so ``otto test`` subcommands expose the same
repo-wide flags as ``otto run``:

```python
# tests/test_device.py
from dataclasses import dataclass
from typing import Annotated

import typer

from my_instructions.options import RepoOptions
from otto.suite import OttoSuite, register_suite


@dataclass
class _Options(RepoOptions):                       # inherits --device-type, --lab-env
    firmware: Annotated[str, typer.Option()] = "latest"


@register_suite()
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

## Dry run

Use `--dry-run` (or `-n`) to preview what would happen without running any
commands on hosts:

```bash
otto --lab my_lab --dry-run run deploy
```

Commands and file transfers are skipped, but connections are still verified.
