"""Reference repo-wide ``@options`` classes (sample).

Options classes are the contract that threads otto's lifecycle: a repo-wide
base is defined once (in any module named in your ``init`` setting), then
inherited by every test suite's inner ``Options`` class and every
``@instruction(options=...)`` so the same flags appear on ``otto test`` and
``otto run``.

``@options`` (``from otto import options``) is otto's name for pydantic's
dataclass decorator: decorating a class with it makes the class a pydantic
dataclass, so its fields are validated at construction. It is not the standard
library's ``@dataclass``.

Copy this module as a starting point, or import these classes directly:

>>> from otto.examples.options import RepoOptions, DeviceSuiteOptions
>>> RepoOptions().device_type
'router'
>>> DeviceSuiteOptions(device_type="switch", firmware="2.1").firmware
'2.1'
>>> from pydantic import ValidationError
>>> try:
...     RepoOptions(retries=-1)
... except ValidationError:
...     print("rejected")
rejected
"""

from typing import Annotated

import typer
from pydantic import Field

from otto import options


@options
class RepoOptions:
    """Repo-wide options shared by every suite and instruction.

    Inherit this from a suite's inner ``Options`` class or from the class you
    pass to ``@instruction(options=...)`` and every field becomes a CLI flag on
    both ``otto test`` and ``otto run`` subcommands.
    """

    device_type: Annotated[
        str,
        typer.Option(help="Type of device under test (e.g. 'router', 'switch')."),
    ] = "router"
    lab_env: Annotated[
        str,
        typer.Option(help="Lab environment to target (e.g. 'staging', 'production')."),
    ] = "staging"
    retries: Annotated[
        int,
        typer.Option(help="Connection retries (must be >= 0)."),
    ] = Field(default=3, ge=0)


@options
class DeviceSuiteOptions(RepoOptions):
    """Suite options: inherits the repo-wide flags and adds ``--firmware``."""

    firmware: Annotated[
        str,
        typer.Option(help="Firmware version to validate against."),
    ] = "latest"


@options
class DeployInstructionOptions(RepoOptions):
    """Instruction options: inherits the repo-wide flags and adds ``--field/--debug``."""

    debug: Annotated[
        bool,
        typer.Option("--field/--debug", help="Use field or debug products."),
    ] = False
