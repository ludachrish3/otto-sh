"""Reference ``@options`` classes (sample).

An options class holds the flags a verb takes. Each class is registered, from
an init module (a module named in your ``init`` setting), for the verbs whose
flags it joins: here ``RepoOptions`` for both ``otto run`` and ``otto test``,
and ``DeviceTestOptions`` for ``otto test`` only. A test reads its instance
with ``ctx.options(DeviceTestOptions)``; an instruction declares a parameter
annotated with a registered class and receives the instance.
``DeployInstructionOptions`` is an instruction's own ``options=`` class: it
inherits ``RepoOptions``, so each shared field stays one flag.

``@options`` (``from otto import options``) is otto's name for pydantic's
dataclass decorator: decorating a class with it makes the class a pydantic
dataclass, so its fields are validated at construction. It is not the standard
library's ``@dataclass``.

Copy this module as a starting point, or import these classes directly:

>>> from otto.examples.options import RepoOptions, DeviceTestOptions
>>> RepoOptions().device_type
'router'
>>> DeviceTestOptions(firmware="2.1").firmware
'2.1'
>>> from pydantic import ValidationError
>>> try:
...     RepoOptions(retries=-1)
... except ValidationError:
...     print("rejected")
rejected

Registering a class is one call in an init module. This example removes the
registration again afterwards; an init module never does:

>>> from otto import register_options
>>> from otto.params import OPTIONS, options_key, verbs_for
>>> register_options(DeviceTestOptions, verbs=["test"])
>>> verbs_for(DeviceTestOptions)
['test']
>>> OPTIONS.unregister(options_key(DeviceTestOptions))
"""

from typing import Annotated

import typer
from pydantic import Field

from otto import options


@options
class RepoOptions:
    """Repo-wide options: register them for ``["run", "test"]``.

    Every field becomes a CLI flag on ``otto test`` and on every ``otto run``
    instruction.
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
class DeviceTestOptions:
    """Test-only options: register them for ``["test"]``.

    They add ``--firmware`` and ``--check-interfaces/--no-check-interfaces``
    to ``otto test``: a ``bool`` field becomes an on/off flag pair.
    """

    firmware: Annotated[
        str,
        typer.Option(help="Firmware version to validate against."),
    ] = "latest"
    check_interfaces: Annotated[
        bool,
        typer.Option(help="Also check every interface's link state."),
    ] = True


@options
class DeployInstructionOptions(RepoOptions):
    """Instruction options: inherits the repo-wide flags and adds ``--debug/--no-debug``."""

    debug: Annotated[
        bool,
        typer.Option(help="Deploy debug products instead of field products."),
    ] = False
