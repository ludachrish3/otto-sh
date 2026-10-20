# Overview

![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)

**otto** — Our Trusty Testing Orchestrator — is a framework for deploying
products to remote hosts for testing and validation.  It provides a CLI and
a Python API for running commands on remote systems, transferring files,
executing tests, and monitoring host metrics in real time.

## Who is otto for?

Otto is a general-purpose tool for developers and testers who need to
interact with one or more remote machines as part of their workflow —
deploying builds, validating firmware, running integration tests, or
collecting performance data.

## Where to start

- **I'm setting up a project** — new to otto, or adding products, tests and
  coverage → {doc}`Set up a project <setup/index>`.
- **I joined a project someone else set up** and want to run it →
  {doc}`Use a project <use/index>`.
- **I want to extend otto** — custom products, hosts, metrics, backends, or
  otto as a Python library → {doc}`Extend otto <extend/index>`.
- **I'm looking something up** → the {doc}`CLI reference <cli/index>`,
  {doc}`configuration <configuration/index>` and {doc}`API <api/index>`.
- **I work on otto itself** → {doc}`Developing otto <developing/index>`.

Install otto first: {doc}`installation`.

## Two ways to use otto

Otto serves two audiences:

CLI users
: Interact with otto through the `otto run`, `otto test`, and `otto monitor`
  commands.  See the {doc}`cli/index` for command-level documentation.

API builders
: Import otto's Python packages to build higher-level automation on top of
  hosts, tests, and the monitor.  See the {doc}`api/index` for the full
  class and function reference.

## Key concepts

### Hosts

A **Host** represents a machine otto can talk to.
{class}`~otto.host.unix_host.UnixHost` connects over SSH or Telnet;
{class}`~otto.host.local_host.LocalHost` runs commands on the local machine
without any network connection.
{class}`~otto.host.embedded_host.EmbeddedHost` (and its concrete {class}`~otto.host.embedded_host.ZephyrHost`) drives a firmware/RTOS target over a serial console; a `DockerContainerHost` targets a container.

They all share a common `BaseHost` interface — {meth}`~otto.host.host.Host.run`,
{meth}`~otto.host.host.Host.exec`, {meth}`~otto.host.host.Host.send` /
{meth}`~otto.host.host.Host.expect` — plus file-transfer methods
({meth}`~otto.host.host.Host.put`, {meth}`~otto.host.host.Host.get`) on the
networked hosts.

`run` executes a command on a host's persistent shell session (state
like the working directory and environment variables are preserved between
calls).  `exec` runs each call independently of the persistent shell and
of other concurrent `exec` calls, making it safe to fan out via
`asyncio.gather()`.

Every lab automatically contains a built-in `local` host — a
{class}`~otto.host.local_host.LocalHost` for the machine otto itself runs
on, usable as `otto host local <verb>` with no configuration.  It is
excluded from lab-wide fleet helpers by default so a deploy or monitoring
sweep never silently operates on the runner; see
{ref}`the fleet of interest <fleet-of-interest>` for the opt-in.

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
variable) and makes every host available to instructions, tests, and
the monitor.  Multiple labs can be merged by combining their names with `+`
(`--lab lab_a+lab_b`).

### Projects, project roots and OTTOPATH

otto works on **projects**. A project is a named unit: its identity is the
`name` in its `.otto/settings.toml`, and its optional `[project]` table scopes
it to labs and hosts.

A **project root** is the directory holding one copy of a project: its
`.otto/settings.toml`, its libraries and its tests. Two clones of one project
are two project roots, and otto refuses to load both at once.

**`OTTOPATH`** lists the project roots otto loads, separated by the OS path
separator (`:` on Linux and macOS, `;` on Windows), like `PATH`. To work on a
project, add its project root to `OTTOPATH`. A root listed twice, under any
spelling, is loaded once.

A **workspace** is the set of projects active together: one `OTTOPATH` value.
Each workspace has its own home under `~/.otto` for the state otto derives.

Docker Compose's own *compose project* and the lab-data *repository* keep
their names and always carry their qualifier.

The project's `.otto/settings.toml` tells otto where to find your Python
libraries, tests, run instructions, and lab data:

```toml
name = "my_project"
version = "1.0.0"

libs  = ["pylib"]
tests = ["tests"]
init  = ["my_instructions"]

[[lab.sources]]
backend = "json"
paths = ["../lab_data"]
```

Relative paths resolve against the project root — see
{doc}`configuration/settings`.  The `init` list names Python modules that
otto imports at startup — this is where you register your instructions and
shared options.  `[[lab.sources]]` is an ordered list: declare several and
otto combines them, later sources overriding earlier ones one element (or one
`labs` table entry) at a time (see {doc}`configuration/host-sources`).

### Instructions (`otto run`)

An **instruction** is an async function decorated with
{func}`@instruction() <otto.instructions.instruction>` that becomes a subcommand of
`otto run`.  Instructions have full access to the lab's hosts and can accept
their own CLI options via Typer annotations:

```python
import logging
from typing import Annotated

import typer

from otto.instructions import instruction
from otto.lab import all_hosts

logger = logging.getLogger(__name__)


@instruction()
async def deploy(
    debug: Annotated[
        bool, typer.Option(help="Deploy debug products instead of field products.")
    ] = False,
):
    for host in all_hosts():
        await host.run(["echo deploying", "make install"])
    logger.info("Done")
```

```bash
otto -l my_lab run deploy --debug
```

### Tests (`otto test`)

otto tests are pytest tests: `Test`-prefixed classes and `test_`-prefixed
functions in your project's test directories. Fixtures, `conftest.py`, markers,
parametrize and pytest plugins all work as pytest documents them.
`otto test` runs them by name or marker expression. Its flags include those
of every options class a project registers for the `test` verb, and a test reads
their values with `ctx.options(...)`:

```python
import logging

from my_project_instructions import DeviceOptions  # the init module declares it

logger = logging.getLogger(__name__)


class TestDevice:
    async def test_device_reachable(self, ctx) -> None:
        logger.info(f"firmware={ctx.options(DeviceOptions).firmware}")
        assert True
```

```bash
otto -l my_lab test TestDevice --firmware 2.1
otto test --iterations 10 --threshold 95 TestDevice
```

Tests get pytest markers (`timeout`, `retry`, `parametrize`, `integration`),
non-fatal assertions via the `expect` fixture, per-test artifact directories,
and built-in monitoring; see {doc}`setup/writing-tests`.

An options class is declared in an init module (a module listed in your
`init` setting), with the verbs whose flags it joins:
`@otto.options(verbs=["test"])` for flags only tests take, or
`@otto.options(verbs=["run", "test"])` for project-wide flags such as a device
type or a lab environment, which every instruction and every test run then
take. See {doc}`setup/options-classes`.

### Monitor (`otto monitor`)

The monitor collects live performance metrics (CPU, memory, disk, network)
from one or more hosts and serves an interactive web dashboard:

```bash
otto -l my_lab monitor --live                     # all hosts, default 5 s interval
otto monitor --live --hosts 'host1|host2' -i 2.0   # narrowed hosts, faster polling
otto monitor --live --db metrics.db                # persist as a session for later viewing
otto monitor metrics.db                            # review a captured session
```

A test can also start a monitor of its own through the `monitor` fixture:
`await monitor.start(hosts=...)`, stopped for it when the test ends. It
collects without a dashboard; see
[Monitoring from a test](setup/test-recipes.md#monitoring-from-a-test).

## Quick-start example

1. **Set the environment** — point otto at your project and lab:

   ```bash
   export OTTOPATH=/path/to/my_project
   otto --lab my_lab --list-hosts          # verify hosts are visible
   ```

2. **Run an instruction:**

   ```bash
   otto -l my_lab run deploy --debug
   ```

3. **Run tests:**

   ```bash
   otto -l my_lab test TestDevice --firmware 2.1
   ```

4. **Monitor hosts:**

   ```bash
   otto -l my_lab monitor --live
   ```

## Where to go next

- {doc}`installation` — Air-gapped installs, team setup, offline docs
- {ref}`team-setup-checklist` — One-time team setup (host source, reservations, libs)
- {doc}`setup/options-classes` — Options classes shared by instructions and tests
- {doc}`extend/extending-cli` — Registering your own top-level `otto` commands
- {doc}`extend/python-library` — Using otto as a Python library
- {doc}`architecture/index` — How otto is put together, for contributors and extenders
- {doc}`api/index` — Full API reference for all otto packages
