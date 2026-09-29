"""Options shared by repo1's tests and instructions.

``repo1_instructions`` registers both classes for their verbs:
``RepoOptions`` for ``otto run`` and ``otto test``, so ``--device-type`` and
``--lab-env`` are flags on every instruction and on ``otto test``;
``DeviceTestOptions`` for ``otto test`` only. A test reads its instance with
``ctx.options(DeviceTestOptions)``; an instruction whose own ``options=``
class inherits ``RepoOptions`` gets the shared fields as one flag each.
"""

from typing import Annotated

import typer

from otto import options


@options
class RepoOptions:
    """Repo-wide options, registered for ``otto run`` and ``otto test``."""

    device_type: Annotated[
        str,
        typer.Option(
            help="Type of device under test (e.g. 'router', 'switch').",
        ),
    ] = "router"

    lab_env: Annotated[
        str,
        typer.Option(
            help="Lab environment to target (e.g. 'staging', 'production').",
        ),
    ] = "staging"


@options
class DeviceTestOptions:
    """Test-only options, registered for ``otto test`` alone."""

    firmware: Annotated[
        str,
        typer.Option(
            help="Firmware version to validate against.",
        ),
    ] = "latest"

    check_interfaces: Annotated[
        bool,
        typer.Option(
            help="When True, verify all expected interfaces are up.",
        ),
    ] = True
