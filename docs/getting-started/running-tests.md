# Running tests

This page works in the project `otto init --all` scaffolded in
[Project setup](index.md#project-setup), not the bed project: it writes a
test of your own and runs it with `otto test`.

`otto init --tests` (or `--all`) scaffolds `tests/test_example.py` (a
`TestExample` class plus a plain `test_example_function`) and a
`tests/conftest.py` with a repo-wide fixture, so
`otto --lab example_lab test TestExample` and
`otto --lab example_lab test test_example_function` both work immediately.
This page hand-writes a more realistic test.

otto tests are plain pytest: a `Test`-prefixed class or a `test_`-prefixed
function, in a `test_*.py` file under the repo's `tests` directory. There is
no base class and nothing to register.

## Flags for your tests

A test's flags come from an **options class**. The scaffold already has one,
`RepoOptions` in `pylib/acme_options.py`, whose `--message` flag every
`otto test` and `otto run` command takes. Add a second class to the same file
for flags only `otto test` needs:

```python
from pydantic import Field  # with the file's other imports


@options
class DeviceOptions:
    """Flags only `otto test` takes."""

    firmware: Annotated[str, typer.Option(help="Firmware version.")] = "latest"
    retries: Annotated[
        int,
        typer.Option(help="Connection retries (>= 0)."),
    ] = Field(default=3, ge=0)
```

A class becomes flags once it is registered for a verb. Registrations go in an
init module, the one module otto imports at startup for every command; the
scaffold's is `pylib/acme_instructions/__init__.py`, which already registers
`RepoOptions`. Add a line under that one:

```python
register_options("acme_options:DeviceOptions", verbs=["test"])
```

## A test

Replace the scaffolded `tests/test_example.py` with the test below (this
replaces the scaffolded `test_example_function` too; the fixture in
`tests/conftest.py` stays):

```python
import logging

from acme_options import DeviceOptions

logger = logging.getLogger(__name__)


class TestExample:
    """Basic connectivity checks."""

    async def test_reachable(self, ctx) -> None:
        opts = ctx.options(DeviceOptions)
        logger.info(f"firmware={opts.firmware} retries={opts.retries}")
        assert True
```

`ctx` is a fixture otto gives every test; `ctx.options(DeviceOptions)` is this
run's `DeviceOptions`, built from the command line.

Run it:

```bash
otto --lab example_lab test TestExample
otto --lab example_lab test TestExample --firmware 2.1
otto --lab example_lab test test_reachable     # a test by name, whatever its class
otto --lab example_lab test -m "not integration"   # by marker expression
otto test --list-tests                # every test, grouped by repo, module and class
otto test --list-tests TestExample    # the tests in TestExample only
otto test --list-markers              # markers available to --markers
```

A name can be a test (`test_reachable`), a class (`TestExample`) or both
(`TestExample::test_reachable`), and it is looked up in every repo. See
{doc}`../cli/test/index` for every flag and {doc}`../cli/test/selection` for
how names and markers select tests.

`@options` (`from otto import options`) is otto's name for **pydantic's**
dataclass decorator: decorating an options class with it makes the class a
pydantic dataclass, so its fields are validated.
`otto --lab example_lab test TestExample --retries -1` fails with a clean CLI
error (exit code 2) before any test runs, instead of being silently accepted.
The same classes give `otto run` instructions their flags. See
{doc}`../cookbook/authoring/options-classes` for the full picture, and
{doc}`../cookbook/authoring/writing-tests` for everything else a test can
use.

The validation runs at construction time, so an out-of-range value is rejected
before any test runs:

```{doctest}
>>> from typing import Annotated
>>> import typer
>>> from pydantic import Field, ValidationError
>>> from otto import options
>>> @options
... class DeviceOptions:
...     retries: Annotated[int, typer.Option()] = Field(default=3, ge=0)
>>> DeviceOptions().retries
3
>>> try: DeviceOptions(retries=-1)
... except ValidationError: print("rejected")
rejected
```
