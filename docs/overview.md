# Overview

![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)

**otto** — Our Trusty Testing Orchestrator — is a framework for deploying
products to remote hosts for testing and validation.  It provides a CLI and
a Python API for running commands on remote systems, transferring files,
executing test suites, and monitoring host metrics in real time.

## Who is otto for?

Otto is a general-purpose tool for developers and testers who need to
interact with one or more remote machines as part of their workflow —
deploying builds, validating firmware, running integration tests, or
collecting performance data.

## Two ways to use otto

Otto serves two audiences, and the documentation is organized around them:

CLI users
: Interact with otto through the `otto run`, `otto test`, and `otto monitor`
  commands.  See the {doc}`guide/index` for command-level documentation.

API builders
: Import otto's Python packages to build higher-level automation on top of
  hosts, suites, and the monitor.  See the {doc}`api/index` for the full
  class and function reference.

## Key concepts

### Hosts

A **Host** represents a machine otto can talk to.
{class}`~otto.host.unix_host.UnixHost` connects over SSH or Telnet;
{class}`~otto.host.local_host.LocalHost` runs commands on the local machine
without any network connection.
{class}`~otto.host.embedded_host.EmbeddedHost` (and its concrete {class}`~otto.host.embedded_host.ZephyrHost`) drives a firmware/RTOS target over a serial console; a `DockerContainerHost` targets a container.

They all share a common `BaseHost` interface — {meth}`~otto.host.host.Host.run`,
{meth}`~otto.host.host.Host.oneshot`, {meth}`~otto.host.host.Host.send` /
{meth}`~otto.host.host.Host.expect` — plus file-transfer methods
({meth}`~otto.host.host.Host.put`, {meth}`~otto.host.host.Host.get`) on the
networked hosts.

`run` executes a command on a host's persistent shell session (state
like the working directory and environment variables are preserved between
calls).  `oneshot` runs each call independently of the persistent shell and
of other concurrent `oneshot` calls, making it safe to fan out via
`asyncio.gather()`.

Every lab automatically contains a built-in `local` host — a
{class}`~otto.host.local_host.LocalHost` for the machine otto itself runs
on, usable as `otto host local <verb>` with no configuration.  It is
excluded from lab-wide fleet helpers by default so a deploy or monitoring
sweep never silently operates on the runner; see {doc}`guide/run` for the
opt-in.

### Results

Host verbs report their outcome through the result family in
{mod}`otto.result`: {meth}`~otto.host.host.Host.run` returns
{class}`~otto.result.Results`, a sequence of one
{class}`~otto.result.CommandResult` per command executed.  Truthiness
follows success, never the payload — an empty-but-successful result is
truthy, a failed result carrying output is falsy — so `if not result:` is
the idiomatic failure check.  The CLI derives its exit codes from the same
objects, ssh-style: a failing command's own return code, or 255 when the
command never ran.

### Labs

Hosts can be reached through intermediate **hops** — SSH jump hosts that
otto tunnels through automatically.  Hops can be chained for multi-hop
paths (`otto -> hop1 -> hop2 -> target`).  All file transfer protocols
(SCP, SFTP, FTP, netcat) work through hops.
Set the `hop` field in a host's JSON definition or use `--hop` on the CLI.

A **Lab** is a JSON file that describes a set of hosts and their topology.
Otto loads labs at startup (via `--lab` or the `OTTO_LAB` environment
variable) and makes every host available to instructions, test suites, and
the monitor.  Multiple labs can be merged by passing several names.

### Repos and settings

Otto discovers your project through a `.otto/settings.toml` file at the
repository root.  This file tells otto where to find your Python libraries,
test suites, run instructions, and lab data:

```toml
name = "my_project"
version = "1.0.0"

labs  = ["${sut_dir}/../lab_data"]
libs  = ["${sut_dir}/pylib"]
tests = ["${sut_dir}/tests"]
init  = ["my_instructions"]
```

`${sut_dir}` is replaced with the repository root at load time.  The `init`
list names Python modules that otto imports at startup — this is where you
register your instructions and shared options.

### Instructions (`otto run`)

An **instruction** is an async function decorated with
{func}`@instruction() <otto.cli.run.instruction>` that becomes a subcommand of
`otto run`.  Instructions have full access to the lab's hosts and can accept
their own CLI options via Typer annotations:

```python
from otto.cli.run import instruction
from otto.configmodule import all_hosts
from otto.logger import get_logger

logger = get_logger()

@instruction()
async def deploy(
    debug: Annotated[bool, typer.Option("--field/--debug",
        help="Use field or debug products.")] = False,
):
    for host in all_hosts():
        await host.run(["echo deploying", "make install"])
    logger.info("Done")
```

```bash
otto -l my_lab run deploy --debug
```

### Test suites (`otto test`)

A **suite** is a class that extends {class}`~otto.suite.suite.OttoSuite`
and is registered with the
{func}`@register_suite() <otto.suite.register.register_suite>` decorator.
Each suite becomes a subcommand of `otto test`.  Suites can define an `Options` class whose fields appear as CLI flags:

```python
from typing import Annotated

import typer

from otto import options
from otto.suite import OttoSuite, register_suite

@options
class _Options:
    firmware: Annotated[str, typer.Option(help="Firmware version.")] = "latest"

@register_suite()
class TestDevice(OttoSuite[_Options]):
    Options = _Options

    async def test_device_reachable(self, suite_options: _Options) -> None:
        self.logger.info(f"firmware={suite_options.firmware}")
        assert True
```

```bash
otto -l my_lab test TestDevice --firmware 2.1
otto test --iterations 10 --threshold 95 TestDevice
```

Suites support pytest markers (`timeout`, `retry`, `parametrize`,
`integration`), non-fatal assertions via `self.expect()`, per-test artifact
directories, and built-in monitoring.

Both suites and instructions accept an options class. For flags that
are repo-wide (device type, lab environment, etc.), define a single
`RepoOptions` class in a module listed in your `init` setting — a `libs` path
like `pylib/` is one common choice — and inherit it from both sides —
see [Sharing repo-wide options](guide/run.md#sharing-repo-wide-options-across-instructions-and-suites).

### Monitor (`otto monitor`)

The monitor collects live performance metrics (CPU, memory, disk, network)
from one or more hosts and serves an interactive web dashboard:

```bash
otto -l my_lab monitor                     # all hosts, default 5 s interval
otto monitor host1,host2 --interval 2.0    # specific hosts, faster polling
otto monitor --db metrics.db               # persist data for later viewing
otto monitor --file metrics.db             # replay saved data
```

Monitoring can also be started from within a test suite using
`await self.start_monitor(hosts=...)` and
`await self.stop_monitor()`.

## Quick-start example

1. **Set the environment** — point otto at your repo and lab:

   ```bash
   export OTTO_SUT_DIRS=/path/to/my_project
   otto --lab my_lab --list-hosts          # verify hosts are visible
   ```

2. **Run an instruction:**

   ```bash
   otto -l my_lab run deploy --debug
   ```

3. **Run a test suite:**

   ```bash
   otto -l my_lab test TestDevice --firmware 2.1
   ```

4. **Monitor hosts:**

   ```bash
   otto -l my_lab monitor
   ```

## Where to go next

- {doc}`getting-started` — Installation and first steps
- {ref}`team-setup-checklist` — One-time team setup (host source, reservations, libs)
- {doc}`guide/index` — Detailed guides for each CLI command
- {doc}`guide/options` — Shared options classes for instructions and suites
- {doc}`guide/extending-cli` — Registering your own top-level `otto` commands
- {doc}`cookbook/index` — Recipes for common asyncio patterns
- {doc}`architecture/index` — How otto is put together, for contributors and extenders
- {doc}`api/index` — Full API reference for all otto packages
