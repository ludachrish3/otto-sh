# Getting Started

This page walks you through installing otto, setting up your first project,
and running your first command.

## Installation

Otto requires **Python 3.10** or later.

### From PyPI

For most users this is all you need — install the latest release from PyPI:

```bash
pip install otto-sh
```

The distribution is named `otto-sh`; the command it installs is `otto`. To
install an exact version, pin it: `pip install otto-sh==0.5.4`.

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

### From a GitHub release

Each tagged release on
[GitHub Releases](https://github.com/ludachrish3/otto-sh/releases)
attaches the same `.whl` and `.tar.gz` artifacts that are published to PyPI.
This is useful when you cannot reach PyPI but can reach GitHub, or when you
want to pin to an exact build.

Download the wheel for the version you want and install it directly:

```bash
VERSION=0.3.0
curl -LO "https://github.com/ludachrish3/otto-sh/releases/download/v${VERSION}/otto_sh-${VERSION}-py3-none-any.whl"
pip install "otto_sh-${VERSION}-py3-none-any.whl"
```

Or with the GitHub CLI:

```bash
gh release download v0.3.0 --repo ludachrish3/otto-sh --pattern '*.whl'
pip install otto_sh-*.whl
```

`pip` will still need internet access to fetch otto's runtime dependencies
from PyPI.  For a fully offline install, follow the air-gapped instructions
below using the downloaded wheel as the starting point.

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
| `jinja2` | 3.1.0 | HTML templating for coverage reports |
| `pydantic` | 2.6 | Boundary validation models for lab JSON, host records, and settings |
| `pydantic-settings` | 2.2 | Environment-variable settings (`OTTO_*`) |
| `pysnmp` | 7.1.0 | Async SNMP manager for separate-channel host monitoring |
| `pytest` | 9.1.1 | Test runner; otto imports user test files at runtime |
| `pytest-asyncio` | 1.4.0 | Async test support for pytest |
| `pytest-timeout` | 2.3.1 | Per-test timeouts for `otto test` (`@pytest.mark.timeout`) |
| `rich` | 15.0.0 | Terminal formatting, panels, and tables |
| `sse-starlette` | 3.3.3 | Server-sent events for live dashboard updates |
| `starlette` | 0.52.1 | ASGI request types used directly by the monitor server |
| `telnetlib3` | 4.0.1 | Async Telnet client for telnet-based hosts |
| `tomli` | 2.4.0 | TOML parser for `.otto/settings.toml` |
| `typer` | 0.26 | CLI framework (builds `otto run`, `otto test`, etc.) |
| `typing-extensions` | 4.12.0 | Backport of `typing.override` (PEP 698) for Python < 3.12 |
| `uvicorn` | 0.42.0 | ASGI server for the monitor dashboard |

These pull in additional transitive dependencies (approximately 25 packages
total at runtime).  Notable transitive dependencies with **native (C/Rust)
extensions** that require platform-specific wheels:

| Package | Pulled in by | Notes |
| ------- | ------------ | ----- |
| `cryptography` | asyncssh | SSH encryption; links against OpenSSL |
| `cffi` | cryptography | C FFI bindings |
| `pydantic-core` | pydantic | Rust-based data validation |

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

Otto discovers your project through a `.otto/settings.toml` file. The
fastest way to get a working project is `otto init` — it scaffolds a
minimal, immediately-runnable repo (settings, an example lab host, an
example test suite, and an example instructions module) so you have
something real to run and edit, instead of a blank page:

```bash
mkdir my_project
otto init --all --name my_project --path my_project
```

`otto init` is **lab-free** — it needs no `--lab` flag and no
`OTTO_SUT_DIRS`, since it only writes files under `--path` (an existing
directory; defaults to the current one). Run it bare with no flags for an interactive walkthrough
that asks, per missing area, whether to scaffold it (and prompts for `name`
and `version` only when `.otto/settings.toml` itself is missing):

```bash
otto init
```

`--all` scaffolds every missing area with no prompts. To scaffold only
specific areas, pass one or more of `--lab`, `--tests`, `--instructions`
(`settings` is always included automatically whenever it's missing, since
every other area depends on it):

```bash
otto init --tests --instructions
```

Areas that already exist are never modified — otto validates them instead
(using the same ingestion code it uses everywhere else) and reports each one
✓ or ✗ in a summary table. The command exits with code 1 if any *existing*
area fails validation.

The four areas `otto init` manages:

settings
: `.otto/settings.toml`, pre-wired with `labs`/`tests`/`libs` paths pointing
  at the other three areas.

lab
: `lab_data/lab.json` with one example host (`example-device`, in lab
  `example_lab`) plus `lab_data/README.md` explaining the schema.

tests
: `tests/test_example.py` (a decorator-less `TestExample` suite plus a plain
  `test_example_function`) and `tests/conftest.py` demonstrating repo-wide
  fixtures.

instructions
: `pylib/<name>_instructions/`, registering one `smoke` instruction.

When it finishes, `otto init` prints a "Next steps" list — the exact
commands to run next, in order (the `export OTTO_SUT_DIRS=...` line is
skipped if your repo is already listed there):

```text
Next steps
  1. export OTTO_SUT_DIRS=/path/to/my_project
  2. otto --install-completion
  3. otto --lab example_lab --list-hosts
  4. otto test --list-suites
  5. otto test TestExample
  6. otto test --tests test_example_function
```

See {doc}`guide/cli-reference` for the full `otto init` flag reference, and
the {ref}`team-setup-checklist` in {doc}`guide/repo-setup` for the one-time
decisions (host source, reservations, shared libs) that come after the
initial scaffold.

### What `otto init` creates

The rest of this section walks through what a scaffolded project looks
like and why — useful whether you ran `otto init` and want to understand the
result, or you're editing an existing project by hand.

A freshly scaffolded project has this shape:

```text
my_project/
├── .otto/
│   └── settings.toml
├── lab_data/
│   ├── lab.json
│   └── README.md
├── pylib/
│   └── my_project_instructions/
│       └── __init__.py
└── tests/
    ├── conftest.py
    └── test_example.py
```

### settings.toml

The settings file tells otto where to find your code:

```toml
name = "my_project"
version = "0.1.0"

labs  = ["${sut_dir}/lab_data"]
tests = ["${sut_dir}/tests"]
libs  = ["${sut_dir}/pylib"]
init  = ["my_project_instructions"]
```

`${sut_dir}` is automatically replaced with the repository root directory at
load time.

| Field | Purpose |
| ----- | ------- |
| `name` | Product or repo name (shown in CLI output) |
| `version` | Semantic version string |
| `labs` | Paths to directories containing lab JSON files |
| `libs` | Python package directories added to `PYTHONPATH` at startup |
| `tests` | Defines where test discovery happens: directories scanned for `test_*.py` files (triggers suite registration) |
| `init` | Python modules imported at startup (registers instructions and shared options) |

```{tip}
Setting otto up for a *team* is a one-time exercise — host source, reservation
gating, shared libs, tab completion. The {ref}`team-setup-checklist` in
{doc}`guide/repo-setup` walks through it.
```

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

`otto init --lab` (or `--all`) scaffolds `lab_data/lab.json` with one
example host for you, plus a `lab_data/README.md` walking through its
fields — see {doc}`guide/host-database` for the full per-field schema. This
section explains the format so you can add real hosts by hand.

A lab file is a JSON object with a `hosts` array (and an optional `links`
array declaring data-plane routes between hosts — see {doc}`guide/lab-config`).
Place lab files in one of the directories listed in your `labs` setting; each
host joins one or more labs through its `labs` field, and `--lab <name>`
selects the matching hosts:

```json
{
    "hosts": [
        {
            "ip": "192.168.1.1",
            "element": "router1",
            "os_type": "unix",
            "valid_terms": ["ssh"],
            "creds": [{ "login": "admin", "password": "secret" }],
            "labs": ["my_lab"]
        },
        {
            "ip": "192.168.1.2",
            "element": "switch1",
            "os_type": "unix",
            "valid_terms": ["telnet"],
            "creds": [{ "login": "admin", "password": "secret" }],
            "labs": ["my_lab"]
        }
    ],
    "links": []
}
```

otto loads each entry into a host object — the same dicts build the
`router1` and `switch1` hosts:

```{doctest}
>>> from otto.storage.factory import create_host_from_dict
>>> hosts = [create_host_from_dict(h) for h in [
...     {"ip": "192.168.1.1", "element": "router1", "os_type": "unix",
...      "valid_terms": ["ssh"], "creds": [{"login": "admin", "password": "secret"}],
...      "labs": ["my_lab"]},
...     {"ip": "192.168.1.2", "element": "switch1", "os_type": "unix",
...      "valid_terms": ["telnet"], "creds": [{"login": "admin", "password": "secret"}],
...      "labs": ["my_lab"]}]]
>>> [h.element for h in hosts]
['router1', 'switch1']
```

Verify otto can see your hosts:

```bash
otto --lab my_lab --list-hosts
```

Every lab also automatically contains a built-in `local` host — a
{class}`~otto.host.local_host.LocalHost` that runs commands on the machine
otto itself runs on, with no JSON entry needed — so
`otto --lab my_lab host local run "uname -a"` always works.  Fleet helpers
like `all_hosts()` exclude it by default; see {doc}`guide/run`.

## Your first instruction

`otto init --instructions` (or `--all`) scaffolds `pylib/<name>_instructions/`
with one `smoke` instruction, so `otto --lab example_lab run smoke` works as
soon as `OTTO_SUT_DIRS` points at the repo (`otto run`, unlike `init`, needs
a lab). This section shows how to hand-write a more realistic one.

An instruction is an async function that becomes a subcommand of `otto run`.
Create `pylib/my_instructions.py` and add `"my_instructions"` to the `init`
list in `settings.toml`:

```python
from typing import Annotated

import typer

from otto.cli.run import instruction
from otto.configmodule import all_hosts
from otto.logger import get_logger

logger = get_logger()


@instruction()
async def hello(
    message: Annotated[str, typer.Option(help="Message to echo.")] = "hello from otto",
):
    """Run a simple echo command on every host in the lab."""
    for host in all_hosts():
        result = (await host.run(f"echo {message}")).only
        logger.info(f"{host.name}: {result.value.strip()}")
```

Run it:

```bash
otto --lab my_lab run hello
otto --lab my_lab run hello --message "hi there"
otto run --list-instructions          # see all available instructions
```

## Your first test suite

`otto init --tests` (or `--all`) scaffolds `tests/test_example.py` (a
decorator-less `TestExample` suite plus a plain `test_example_function`) and
a `tests/conftest.py` with a repo-wide fixture, so `otto --lab example_lab
test TestExample` and `otto --lab example_lab test --tests
test_example_function` both work immediately. This section shows how to
hand-write a more realistic suite.

A test suite is an {class}`~otto.suite.suite.OttoSuite` subclass with a
`Test`-prefixed name — it registers automatically, no decorator needed.
Create `tests/test_example.py`:

```python
from typing import Annotated

import typer
from pydantic import Field

from otto import options
from otto.suite import OttoSuite


@options
class _Options:
    firmware: Annotated[str, typer.Option(help="Firmware version.")] = "latest"
    retries: Annotated[int, typer.Option(help="Connection retries (>= 0).")] = Field(default=3, ge=0)


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
otto test --list-markers              # see markers available to --markers
otto test --list-tests                # list every test in every registered suite
otto test --list-tests --markers slow # list tests matching the marker expression
otto test --list-tests TestExample    # list tests in TestExample only
otto --lab my_lab test --tests test_reachable  # run by name, no suite name needed
otto --lab my_lab test -m "not integration"    # run by marker, no suite name needed
```

The last two forms skip the suite name entirely — `--tests` and/or `-m`
alone select matching tests across every suite (and every repo). See
{doc}`guide/test` for the full selection-run syntax, including how a
suite's `Options` defaults apply when it's reached this way.

`@options` (`from otto import options`) is otto's name for **pydantic's**
dataclass decorator: decorating an Options class with it makes the class a
pydantic dataclass, so its fields are validated. `otto test TestExample
--retries -1` fails with a clean CLI error (exit code 2) instead of being
silently accepted. The same `@options` classes power `@instruction(options=...)`
for `otto run` subcommands. See {doc}`guide/options` for the full picture.

The validation runs at construction time, so an out-of-range value is rejected
before the suite ever runs:

```{doctest}
>>> from typing import Annotated
>>> import typer
>>> from pydantic import Field, ValidationError
>>> from otto import options
>>> @options
... class _Options:
...     retries: Annotated[int, typer.Option()] = Field(default=3, ge=0)
>>> _Options().retries
3
>>> try: _Options(retries=-1)
... except ValidationError: print("rejected")
rejected
```

## Monitoring hosts

Launch the live performance dashboard:

```bash
otto --lab my_lab monitor
otto --lab my_lab monitor router1,switch1 --interval 2.0
```

This opens a web dashboard showing CPU, memory, disk, and network metrics.

## Where to go next

- {ref}`team-setup-checklist` -- One-time setup when adopting otto for a team
- {doc}`guide/index` -- Detailed guides for each CLI command and project configuration
- {doc}`cookbook/index` -- Recipes for common asyncio patterns
- {doc}`api/index` -- Full API reference

## Next steps

- {doc}`guide/lab-config` — configuring hosts and labs
- {doc}`guide/embedded` — firmware/RTOS targets
- {doc}`guide/index` — all command guides
