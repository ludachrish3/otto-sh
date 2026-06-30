# Options classes

Options classes are how you add command-line flags to your `otto run`
instructions and `otto test` suites. They are otto's through-line: define a set
of options once, and the same definition surfaces on every command that
inherits it.

## Options across otto's lifecycle

Options appear at three points in a project's lifecycle:

- **Project definition** — you define repo-wide options once, in a module named
  in your `init` setting. These are the common flags every instruction and suite
  shares (device type, lab environment, …).
- **Instruction execution** — {func}`@instruction() <otto.cli.run.instruction>`
  expands an options class into `otto run` flags and hands your function a
  populated instance.
- **Test suite runs** — {class}`~otto.suite.suite.OttoSuite` with
  {func}`@register_suite() <otto.suite.register.register_suite>` expands an
  options class into `otto test` flags and passes them to each test method as
  `suite_options`.

Defining the options once and inheriting them keeps `otto run` and `otto test`
in lock-step.

## Anatomy of an options class

An options class has fields annotated with `Annotated[T, typer.Option(...)]`.
Each field becomes a CLI flag; the `typer.Option(...)` carries the help text and
any flag spelling.

```python
from typing import Annotated

import typer

from otto import options


@options
class RepoOptions:
    device_type: Annotated[
        str, typer.Option(help="Type of device under test (e.g. 'router', 'switch').")
    ] = "router"
    lab_env: Annotated[
        str, typer.Option(help="Lab environment to target.")
    ] = "staging"
```

`--device-type` and `--lab-env` now appear in `--help` wherever this class is
used.

## `@options` is a pydantic dataclass

`@options` (`from otto import options`) is otto's ergonomic name for
**pydantic's** dataclass decorator — `pydantic.dataclasses.dataclass`,
re-exported under otto's namespace. Decorating a class with `@options` makes it
a *pydantic dataclass*: its fields are validated when the class is constructed.

```{important}
`@options` is **pydantic's** dataclass, not the standard library's
`@dataclass`. Use `@options` for every options class so your flags are
validated and consistent.
```

Importing `from otto import options` — rather than reaching for pydantic
directly — keeps every options class on one standard import and gives otto a
single seam to evolve options behaviour in the future.

## Validating fields

Add pydantic constraints with `Field(...)`. An out-of-range value is rejected at
construction — before the suite or instruction runs — and otto turns the error
into a clean CLI failure (exit code 2, naming the offending flag) instead of
silently accepting it.

```python
from typing import Annotated

import typer
from pydantic import Field

from otto import options


@options
class RepoOptions:
    retries: Annotated[
        int, typer.Option(help="Connection retries (must be >= 0).")
    ] = Field(default=3, ge=0)
```

```bash
otto test TestDevice --retries -1
# error: Invalid value: retries: Input should be greater than or equal to 0
```

Validation runs at construction time, so the bad value never reaches your test.
A copyable example ships in otto as `otto.examples.options`
(`src/otto/examples/options.py`):

```{doctest}
>>> from otto.examples.options import RepoOptions
>>> RepoOptions().retries
3
>>> from pydantic import ValidationError
>>> try:
...     RepoOptions(retries=-1)
... except ValidationError:
...     print("rejected")
rejected
```

## Sharing repo-wide options

Define a base options class once and inherit it everywhere you want the same
flags. Put the base in **any module named in your repo's `init` setting** — the
location is yours. A `libs` directory such as `pylib/` is a common place to keep
it, but the only rule is that the module is importable and listed in `init` (see
{doc}`repo-setup`).

`otto.examples.options` bundles a complete example: a `RepoOptions` base plus a
suite options class and an instruction options class that both inherit it.

### In a test suite

```python
from typing import Annotated

import typer
from otto import options
from otto.suite import OttoSuite, register_suite

from my_shared.options import RepoOptions  # your base, listed in `init`


@options
class _Options(RepoOptions):              # inherits --device-type, --lab-env, --retries
    firmware: Annotated[str, typer.Option(help="Firmware version.")] = "latest"


@register_suite()
class TestDevice(OttoSuite[_Options]):
    Options = _Options

    async def test_version(self, suite_options: _Options) -> None:
        self.logger.info(f"device={suite_options.device_type} fw={suite_options.firmware}")
```

`otto test TestDevice --help` shows `--device-type`, `--lab-env`, `--retries`,
and `--firmware`.

### In an instruction

```python
from typing import Annotated

import typer
from otto import options
from otto.cli.run import instruction

from my_shared.options import RepoOptions  # your base, listed in `init`


@options
class _DeployOpts(RepoOptions):           # inherits --device-type, --lab-env, --retries
    debug: Annotated[bool, typer.Option("--field/--debug")] = False


@instruction(options=_DeployOpts)
async def deploy(opts: _DeployOpts):
    ...
```

`otto run deploy --help` shows the same repo-wide flags plus `--field/--debug`.

See {doc}`run` and {doc}`test` for the full instruction and suite guides, and
[Inheriting shared options](../cookbook/suite-recipes.md#inheriting-shared-options)
in the cookbook.
