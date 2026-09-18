# Running test suites

This page works in the project `otto init --all` scaffolded in
[Project setup](index.md#project-setup), not the bed project: it writes a test
suite of your own and runs it with `otto test`.

`otto init --tests` (or `--all`) scaffolds `tests/test_example.py` (a
decorator-less `TestExample` suite plus a plain `test_example_function`) and
a `tests/conftest.py` with a repo-wide fixture, so `otto --lab example_lab
test TestExample` and `otto --lab example_lab test --tests
test_example_function` both work immediately. This page hand-writes a
more realistic suite.

A test suite is an {class}`~otto.suite.suite.OttoSuite` subclass with a
`Test`-prefixed name — it registers automatically, no decorator needed.
Replace the scaffolded `tests/test_example.py` with the suite below (this
replaces the scaffolded `test_example_function` too; the fixture in
`tests/conftest.py` stays):

```python
import logging
from typing import Annotated

import typer
from pydantic import Field

from otto import options
from otto.suite import OttoSuite

logger = logging.getLogger(__name__)


@options
class _Options:
    firmware: Annotated[str, typer.Option(help="Firmware version.")] = "latest"
    retries: Annotated[
        int,
        typer.Option(help="Connection retries (>= 0)."),
    ] = Field(default=3, ge=0)


class TestExample(OttoSuite):
    """Basic connectivity checks."""

    Options = _Options

    async def test_reachable(self, suite_options: _Options) -> None:
        logger.info(f"firmware={suite_options.firmware}")
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
{doc}`../cli/test/index` for the full selection-run syntax, including how a
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
