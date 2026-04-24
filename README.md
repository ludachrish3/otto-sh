# otto

**otto** — Our Trusty Testing Orchestrator — is a framework for deploying
products to remote hosts for testing and validation. It provides a CLI and a
Python API for running commands on remote systems, transferring files,
executing test suites, and monitoring host metrics in real time.

## Who is otto for?

Otto is a general-purpose tool for developers and testers who need to interact
with one or more remote machines as part of their workflow — deploying builds,
validating firmware, running integration tests, or collecting performance
data.

## Two ways to use otto

- **CLI users** — interact with otto through the `otto run`, `otto test`, and
  `otto monitor` commands.
- **API builders** — import otto's Python packages to build higher-level
  automation on top of hosts, suites, and the monitor.

## Key concepts

### Hosts

A **Host** represents a machine otto can talk to. `RemoteHost` connects over
SSH or Telnet; `LocalHost` runs commands on the local machine without any
network connection.

Both expose the same core interface — `run`, `oneshot`, `send` / `expect`, and
file-transfer methods (`put`, `get`).

`run` executes a command on a host's persistent shell session (state like the
working directory and environment variables are preserved between calls).
`oneshot` runs each call independently of the persistent shell and of other
concurrent `oneshot` calls, making it safe to fan out via `asyncio.gather()`.

### Labs

Hosts can be reached through intermediate **hops** — SSH jump hosts that otto
tunnels through automatically. Hops can be chained for multi-hop paths
(`otto -> hop1 -> hop2 -> target`). All file transfer protocols (SCP, SFTP,
FTP, netcat) work through hops. Set the `hop` field in a host's JSON
definition or use `--hop` on the CLI.

A **Lab** is a JSON file that describes a set of hosts and their topology.
Otto loads labs at startup (via `--lab` or the `OTTO_LAB` environment
variable) and makes every host available to instructions, test suites, and
the monitor. Multiple labs can be merged by passing several names.

### Repos and settings

Otto discovers your project through a `.otto/settings.toml` file at the
repository root. This file tells otto where to find your Python libraries,
test suites, run instructions, and lab data:

```toml
name = "my_project"
version = "1.0.0"

labs  = ["${sutDir}/../lab_data"]
libs  = ["${sutDir}/pylib"]
tests = ["${sutDir}/tests"]
init  = ["my_instructions"]
```

`${sutDir}` is replaced with the repository root at load time. The `init` list
names Python modules that otto imports at startup — this is where you
register your instructions and shared options.

### Instructions (`otto run`)

An **instruction** is an async function decorated with `@instruction()` that
becomes a subcommand of `otto run`. Instructions have full access to the
lab's hosts and can accept their own CLI options via Typer annotations:

```python
from otto.cli.run import instruction
from otto.configmodule.configmodule import all_hosts
from otto.logger import getOttoLogger

logger = getOttoLogger()

@instruction()
async def deploy(
    debug: Annotated[bool, typer.Option("--field/--debug")] = False,
):
    for host in all_hosts():
        await host.run(["echo deploying", "make install"])
    logger.info("Done")
```

```bash
otto -l my_lab run deploy --debug
```

### Test suites (`otto test`)

A **suite** is a class that extends `OttoSuite` and is registered with the
`@register_suite()` decorator. Each suite becomes a subcommand of `otto test`.
Suites can define their own `Options` dataclass whose fields appear as CLI
flags:

```python
from dataclasses import dataclass
from typing import Annotated

import typer
from otto.suite import OttoSuite, register_suite

@dataclass
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

Both suites and instructions accept an options dataclass. For flags that are
repo-wide (device type, lab environment, etc.), define a single `RepoOptions`
dataclass in your pylib and inherit it from both sides.

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
`await self.startMonitor(hosts=...)` and `await self.stopMonitor()`.

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

## Documentation

Full documentation lives under `docs/` and can be built with `make docs` —
the generated HTML is written to `docs/_build/html/`. Key entry points:

- `docs/getting-started.md` — installation and first steps
- `docs/guide/` — detailed guides for each CLI command
- `docs/cookbook/` — recipes for common asyncio patterns
- `docs/api/` — full API reference for all otto packages
