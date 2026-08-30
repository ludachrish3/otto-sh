# Getting Started

This page installs otto and maps into a multi-page worked example that
defines otto's own test bed, host by host, from scratch.

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
project's other Python dependencies? {doc}`../installation` covers the recommended
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

Otto discovers your project through a `.otto/settings.toml` file. `otto init
--all --name acme --path /tmp/otto-gs/acme` scaffolds a runnable one (settings,
an example lab host, an example suite, an example instruction) and prints the
next steps, completion included. The directory has to exist first
(`mkdir -p /tmp/otto-gs/acme`) — `otto init` never creates one — and
substitute your own name and a path of your own: `/tmp/otto-gs` is the scratch
directory otto's own documentation build creates and wipes.

```{literalinclude} ../examples/getting-started/captures/init-all.txt
:language: text
```

The last three steps name the lab explicitly (`--lab example_lab`), as every
example on this page does; {doc}`../guide/cli/index` covers `--lab`, the
`OTTO_LAB` environment variable that replaces it, and the rest of the global
options.

{doc}`../guide/cli/init` is the flag reference; {doc}`../guide/configuration/settings`
explains every key `settings.toml` accepts and the one-time
{ref}`team-setup-checklist`. Point otto at the project with the
`export OTTO_SUT_DIRS=…` line it printed — nothing is discovered from the
working directory.

The scaffolded `example-device` is a placeholder: replace its `ip` and `creds`
in `lab_data/lab.json` (the scaffolded `lab_data/README.md` explains every
field) before anything connects to it. Every lab also carries a built-in
`local` host — the machine otto runs on — which needs no lab edit at all:
`otto --lab example_lab host local run "uname -a"`.

## The worked example

The rest of this section defines otto's own test bed from scratch — four
Ubuntu VMs, five BusyBox guests, seven Zephyr targets — as a real, checked-in
project (`docs/examples/getting-started/`). Every fragment shown is included
from that project and every command's output is captured from a real run —
the bed's, where a command needs one — so what you read is what runs.

```{toctree}
:maxdepth: 1

defining-hosts/index
customizations
boards-of-interest
reservations
```

## Running things

### Your first instruction

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
otto --lab example_lab run hello --help   # the generated flags, no host contacted
otto --lab example_lab run hello
otto --lab example_lab run hello --message "hi there"
otto run --list-instructions              # see all available instructions
```

### Your first test suite

`otto init --tests` (or `--all`) scaffolds `tests/test_example.py` (a
decorator-less `TestExample` suite plus a plain `test_example_function`) and
a `tests/conftest.py` with a repo-wide fixture, so `otto --lab example_lab
test TestExample` and `otto --lab example_lab test --tests
test_example_function` both work immediately. This section shows how to
hand-write a more realistic suite.

A test suite is an {class}`~otto.suite.suite.OttoSuite` subclass with a
`Test`-prefixed name — it registers automatically, no decorator needed.
Replace the scaffolded `tests/test_example.py` with:

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
otto --lab example_lab test TestExample
otto --lab example_lab test TestExample --firmware 2.1
otto test --list-suites               # see all registered suites
otto test --list-markers              # see markers available to --markers
otto test --list-tests                # list every test in every registered suite
otto test --list-tests --markers slow # list tests matching the marker expression
otto test --list-tests TestExample    # list tests in TestExample only
otto --lab example_lab test --tests test_reachable  # run by name, no suite name needed
otto --lab example_lab test -m "not integration"    # run by marker, no suite name needed
```

The last two forms skip the suite name entirely — `--tests` and/or `-m`
alone select matching tests across every suite (and every repo). See
{doc}`../guide/cli/test/index` for the full selection-run syntax, including how a
suite's `Options` defaults apply when it's reached this way.

`@options` (`from otto import options`) is otto's name for **pydantic's**
dataclass decorator: decorating an Options class with it makes the class a
pydantic dataclass, so its fields are validated. `otto --lab example_lab test
TestExample --retries -1` fails with a clean CLI error (exit code 2) instead
of being silently accepted. The same `@options` classes power `@instruction(options=...)`
for `otto run` subcommands. See {doc}`../library/options-classes` for the full picture.

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

### Monitoring hosts

Launch the live performance dashboard:

```bash
otto --lab example_lab monitor --live
otto --lab example_lab monitor --live --hosts 'example-device' --interval 2.0
```

This opens a web dashboard showing CPU, memory, disk, and network metrics.
`--live` is what collects from the lab; the positional argument reviews a
saved export instead, and `--hosts` narrows by a regex full-matched against
host ids — see {doc}`../guide/cli/monitor/index`.

## Where to go next

- {ref}`team-setup-checklist` -- One-time setup when adopting otto for a team
- {doc}`../guide/cli/index` -- Every `otto` command, one page per verb
- {doc}`../guide/configuration/index` -- The project and lab files every command reads
- {doc}`../library/index` -- Using otto as a Python library, plus recipes
- {doc}`../api/index` -- Full API reference
