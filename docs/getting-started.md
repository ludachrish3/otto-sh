# Getting Started

This page walks you through installing otto, setting up your first project,
and running your first command.

## Installation

Otto requires **Python 3.10** or later. Install the latest release from PyPI into a
virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install otto-sh
```

The distribution is named `otto-sh`; the command it installs is `otto`.

Setting up a team, working on an air-gapped network, or managing otto alongside your
project's other Python dependencies? {doc}`installation` covers the recommended
`pyproject.toml`/uv setup, downloading wheels, internal package indexes, and reading
these docs offline.

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
specific areas, pass one or more of `--schemas`, `--lab`, `--tests`,
`--instructions` (`settings` is always included automatically whenever it's
missing, since every other area depends on it):

```bash
otto init --tests --instructions
```

Areas that already exist are never modified — except the otto-owned schemas
area, which `otto init --schemas` refreshes (e.g. after upgrading otto) —
otto validates them instead (using the same ingestion code it uses
everywhere else) and reports each one ✓ or ✗ in a summary table. The command
exits with code 1 if any *existing* area fails validation.

The five areas `otto init` manages:

settings
: `.otto/settings.toml`, pre-wired with `labs`/`tests`/`libs` paths and a
  `#:schema` editor directive, pointing at the other four areas.

schemas
: `.otto/schemas/*.schema.json` — generated editor schemas for `lab.json`,
  `settings.toml`, and each host type — plus `.vscode` wiring. Otto-owned
  and kept fresh by a staleness doctor; `otto init --schemas` regenerates
  it even when already present.

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
  7. otto run smoke
```

See {doc}`guide/cli-reference` for the full `otto init` flag reference, and
the {ref}`team-setup-checklist` in {doc}`guide/setup/repo-setup` for the one-time
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

labs  = ["lab_data"]
tests = ["tests"]
libs  = ["pylib"]
init  = ["my_project_instructions"]
```

Relative paths resolve against the repository root; `~` expands to your home
directory.  See {doc}`guide/setup/repo-setup` for the full rule.

| Field | Purpose |
| ----- | ------- |
| `name` | Product or repo name (shown in CLI output) |
| `version` | Semantic version string |
| `labs` | Paths to directories containing lab JSON files |
| `libs` | Python package directories added to `PYTHONPATH` at startup |
| `tests` | Where test discovery happens. Each directory's **top-level** `test_*.py` files are imported at startup, registering their `Test*` `OttoSuite` subclasses; pytest itself recurses normally when tests are run ([details](guide/test.md#suite-registration)) |
| `init` | Python modules imported at startup (registers instructions and shared options) |

```{tip}
Setting otto up for a *team* is a one-time exercise — host source, reservation
gating, shared libs, tab completion. The {ref}`team-setup-checklist` in
{doc}`guide/setup/repo-setup` walks through it.
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
| `OTTO_LAB` | Lab name(s) to use; combine several with `+` | *(or use `--lab`)* |
| `OTTO_XDIR` | Output directory for logs and artifacts | current directory |
| `OTTO_LOG_LEVEL` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) | `INFO` |
| `OTTO_LOG_DAYS` | Number of days to retain logs | `30` |
| `OTTO_TEARDOWN_DEADLINE` | Seconds an interrupted command's cleanup may run before being abandoned | `10` |

## Lab files

`otto init --lab` (or `--all`) scaffolds `lab_data/lab.json` with one
example host for you, plus a `lab_data/README.md` walking through its
fields — see {doc}`guide/setup/lab-config` for the full per-field schema. This
section explains the format so you can add real hosts by hand.

A lab file is a JSON object with a `hosts` array (and an optional `links`
array declaring data-plane routes between hosts — see {doc}`guide/setup/lab-config`).
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
>>> from otto.host.factory import create_host_from_dict
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
like `all_hosts()` exclude it by default; see {doc}`guide/run/index`.

## Your first instruction

`otto init --instructions` (or `--all`) scaffolds `pylib/<name>_instructions/`
with one `smoke` instruction, so `otto --lab example_lab run smoke` works as
soon as `OTTO_SUT_DIRS` points at the repo (`otto run`, unlike `init`, needs
a lab). This section shows how to hand-write a more realistic one.

An instruction is an async function that becomes a subcommand of `otto run`.
Create `pylib/my_instructions.py` and add `"my_instructions"` to the `init`
list in `settings.toml`:

```python
import logging
from typing import Annotated

import typer

from otto.cli.run import instruction
from otto.config import all_hosts

logger = logging.getLogger(__name__)


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
    retries: Annotated[
        int,
        typer.Option(help="Connection retries (>= 0)."),
    ] = Field(default=3, ge=0)


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
for `otto run` subcommands. See {doc}`guide/run/options` for the full picture.

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
- {doc}`library/index` -- Using otto as a Python library, plus recipes
- {doc}`api/index` -- Full API reference

## Next steps

- {doc}`guide/setup/lab-config` — configuring hosts and labs
- {doc}`guide/hosts/embedded` — firmware/RTOS targets
- {doc}`guide/index` — all command guides
