# Getting Started

This page walks you through installing otto, setting up your first project,
and running your first command.

## Installation

Otto requires **Python 3.10** or later.

### From source (development)

If you have a clone of the otto repository and an internet connection, use
uv to install otto along with its dev dependencies (pytest, sphinx, ruff,
etc.):

```bash
uv sync           # installs otto + runtime deps + dev deps
```

Dev dependencies are only needed for running tests, building docs, and
linting.  They are **not** included in the otto wheel and are not needed
on machines that only run otto.

### From a wheel (internet-connected)

Build a wheel and install it.  The wheel only declares runtime
dependencies — dev tools like pytest and sphinx are excluded:

```bash
uv build --wheel                   # produces dist/otto_sh-<version>-py3-none-any.whl
pip install dist/otto_sh-*.whl     # installs otto + downloads runtime deps from PyPI
```

### Air-gapped installation

Otto is designed to work on air-gapped networks.  Since `pip install` and
`uv sync` cannot reach PyPI on an isolated host, you must pre-download all
wheel files on an internet-connected machine and transfer them to the
target.

#### Step 1: Build the otto wheel (internet-connected machine)

```bash
uv build --wheel          # produces dist/otto_sh-<version>-py3-none-any.whl
```

#### Step 2: Download all runtime dependency wheels

Use `pip download` to fetch every dependency as a wheel file.  You must
target the same Python version and platform as the air-gapped host:

```bash
pip download \
    dist/otto_sh-*.whl \
    --dest ./wheels \
    --python-version 3.10 \
    --platform manylinux2014_x86_64 \
    --only-binary :all:
```

This places the otto wheel **and** all of its transitive runtime
dependencies into `./wheels/`.  Dev dependencies (pytest, sphinx, etc.)
are **not** included because they are not declared in the wheel's metadata.

```{note}
Three of otto's transitive dependencies ship **platform-specific binary
wheels**: `cryptography`, `cffi`, and `pydantic-core`.  The `--platform`
and `--python-version` flags must match the target host.  Common platform
tags:

- `manylinux2014_x86_64` — most Linux x86-64 systems
- `manylinux2014_aarch64` — Linux ARM64
- `macosx_11_0_arm64` — macOS Apple Silicon
- `win_amd64` — Windows 64-bit

If your air-gapped host runs a different architecture, adjust accordingly.
```

Alternatively, you can export a pinned requirements file first:

```bash
uv export --no-dev --no-hashes > requirements.txt
pip download -r requirements.txt --dest ./wheels --only-binary :all:
cp dist/otto_sh-*.whl ./wheels/
```

#### Step 3: Transfer the wheels directory

Copy the entire `wheels/` directory to the air-gapped host using whatever
transfer method is available (USB drive, SCP to a bastion, shared
filesystem, etc.).

#### Step 4: Install from the local wheels directory

On the air-gapped host:

```bash
pip install --no-index --find-links ./wheels/ otto-sh
```

Or with uv:

```bash
uv pip install --no-index --find-links ./wheels/ otto-sh
```

The `--no-index` flag tells the installer to look *only* in `./wheels/`
and never contact PyPI.

### Verifying the installation

```bash
otto --version
```

### Enabling tab completion

Otto ships with a Typer-generated shell completion script.  Install it once
with `--install-completion` and then source the generated script in your
shell:

```bash
otto --install-completion
source ~/.bash_completions/otto.sh
```

To make tab completion available in every new shell, add those two lines to
your `~/.bashrc` (or `~/.profile`) so they run automatically at login.

### Dependencies

Otto's direct runtime dependencies (declared in `pyproject.toml` under
`[project] dependencies`):

| Package | Min version | Purpose |
| ------- | ----------- | ------- |
| `aioftp` | 0.27.2 | Async FTP client for file transfers |
| `aiosqlite` | 0.21.0 | Async SQLite for persisting monitor metrics |
| `asyncssh` | 2.22.0 | SSH connections to remote hosts |
| `fastapi` | 0.135.1 | Monitor dashboard web server |
| `rich` | 14.3.3 | Terminal formatting, panels, and tables |
| `sse-starlette` | 3.3.3 | Server-sent events for live dashboard updates |
| `telnetlib3` | 4.0.1 | Async Telnet client for telnet-based hosts |
| `tomli` | 2.4.0 | TOML parser for `.otto/settings.toml` |
| `typer` | 0.24.0 | CLI framework (builds `otto run`, `otto test`, etc.) |
| `uvicorn` | 0.42.0 | ASGI server for the monitor dashboard |

These pull in additional transitive dependencies (approximately 25 packages
total at runtime).  Notable transitive dependencies with **native (C/Rust)
extensions** that require platform-specific wheels:

| Package | Pulled in by | Notes |
| ------- | ------------ | ----- |
| `cryptography` | asyncssh | SSH encryption; links against OpenSSL |
| `cffi` | cryptography | C FFI bindings |
| `pydantic-core` | fastapi → pydantic | Rust-based data validation |

Dev dependencies (pytest, sphinx, ruff, pyinstrument, etc.) are declared
in the `[dependency-groups] dev` section of `pyproject.toml` and are **not**
included in the otto wheel.  They are only installed by `uv sync` for
development purposes.

### Air-gapped considerations

Beyond installation, keep the following in mind when running otto without
internet access:

Monitor dashboard assets
: The monitor's web dashboard bundles all static assets (HTML, CSS,
  JavaScript, and Plotly.js) inside the otto package itself.  **No CDN or
  external network access is needed** to serve the dashboard.

SSH host key verification
: `asyncssh` will attempt to verify SSH host keys.  On first connection
  to a new host, you may need to pre-populate `~/.ssh/known_hosts` or
  configure your hosts to skip strict host key checking, depending on
  your security requirements.

Log retention
: Otto stores logs and artifacts under the `--xdir` directory.  On
  isolated systems with limited disk, use the `--log-days` setting
  (default: 30 days) to control automatic cleanup.

Python availability
: Ensure the air-gapped host has Python 3.10+ installed.  If the system
  Python is older, you will need to transfer a compatible Python build as
  well.

## Project setup

Otto discovers your project through a `.otto/settings.toml` file.  Create a
minimal project structure:

```text
my_project/
├── .otto/
│   └── settings.toml
├── pylib/
│   └── my_instructions.py
└── tests/
    └── test_example.py
```

### settings.toml

The settings file tells otto where to find your code:

```toml
name = "my_project"
version = "1.0.0"

labs  = ["${sutDir}/../lab_data"]
libs  = ["${sutDir}/pylib"]
tests = ["${sutDir}/tests"]
init  = ["my_instructions"]
```

`${sutDir}` is automatically replaced with the repository root directory at
load time.

| Field | Purpose |
| ----- | ------- |
| `name` | Product or repo name (shown in CLI output) |
| `version` | Semantic version string |
| `labs` | Paths to directories containing lab JSON files |
| `libs` | Python package directories added to `PYTHONPATH` at startup |
| `tests` | Directories scanned for `test_*.py` files (triggers suite registration) |
| `init` | Python modules imported at startup (registers instructions and shared options) |

### Environment variables

Set `OTTO_SUT_DIRS` to point otto at your project:

```bash
export OTTO_SUT_DIRS=/path/to/my_project
```

Other useful environment variables:

| Variable | Purpose | Default |
| -------- | ------- | ------- |
| `OTTO_SUT_DIRS` | Comma-separated paths to repos under test | *(required)* |
| `OTTO_LAB` | Lab name(s) to use | *(or use `--lab`)* |
| `OTTO_XDIR` | Output directory for logs and artifacts | current directory |
| `OTTO_LOG_LEVEL` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) | `INFO` |
| `OTTO_LOG_DAYS` | Number of days to retain logs | `30` |

## Lab files

A lab is a JSON file that describes the hosts otto can connect to.  Place lab
files in one of the directories listed in your `labs` setting:

```json
{
    "name": "my_lab",
    "hosts": {
        "router1": {
            "ip": "192.168.1.1",
            "user": "admin",
            "password": "secret",
            "term": "ssh"
        },
        "switch1": {
            "ip": "192.168.1.2",
            "user": "admin",
            "password": "secret",
            "term": "telnet"
        }
    }
}
```

Verify otto can see your hosts:

```bash
otto --lab my_lab --list-hosts
```

## Your first instruction

An instruction is an async function that becomes a subcommand of `otto run`.
Create `pylib/my_instructions.py`:

```python
from typing import Annotated

import typer

from otto.cli.run import instruction
from otto.configmodule.configmodule import all_hosts
from otto.logger import getOttoLogger

logger = getOttoLogger()


@instruction()
async def hello(
    message: Annotated[str, typer.Option(help="Message to echo.")] = "hello from otto",
):
    """Run a simple echo command on every host in the lab."""
    for host in all_hosts():
        result = (await host.run(f"echo {message}")).only
        logger.info(f"{host.name}: {result.output.strip()}")
```

Run it:

```bash
otto --lab my_lab run hello
otto --lab my_lab run hello --message "hi there"
otto run --list-instructions          # see all available instructions
```

## Your first test suite

A test suite is an {class}`~otto.suite.suite.OttoSuite` subclass registered
with {func}`@register_suite() <otto.suite.register.register_suite>`.
Create `tests/test_example.py`:

```python
from dataclasses import dataclass
from typing import Annotated

import typer

from otto.suite import OttoSuite, register_suite


@dataclass
class _Options:
    firmware: Annotated[str, typer.Option(help="Firmware version.")] = "latest"


@register_suite()
class TestExample(OttoSuite[_Options]):
    """Basic connectivity checks."""

    Options = _Options

    async def test_reachable(self, suite_options: _Options) -> None:
        self.logger.info(f"firmware={suite_options.firmware}")
        assert True
```

Run it:

```bash
otto --lab my_lab test TestExample
otto --lab my_lab test TestExample --firmware 2.1
otto test --list-suites               # see all registered suites
```

## Monitoring hosts

Launch the live performance dashboard:

```bash
otto --lab my_lab monitor
otto --lab my_lab monitor router1,switch1 --interval 2.0
```

This opens a web dashboard showing CPU, memory, disk, and network metrics.

## Where to go next

- {doc}`guide/index` -- Detailed guides for each CLI command and project configuration
- {doc}`cookbook/index` -- Recipes for common asyncio patterns
- {doc}`api/index` -- Full API reference
