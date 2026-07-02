"""Instruction that executes a single command on a docker container host.

Intended for e2e tests that verify ``otto run`` drives a real
``DockerContainerHost`` end-to-end.  The instruction is intentionally
minimal: it cats a fixture marker file and returns the output, letting the
test assert specific text in the subprocess stdout.

Run it against the veggies lab::

    OTTO_SUT_DIRS=<repo1 dir> otto -l veggies run run-on-container \\
        --on <element>_seed.repo1.api

The container must already be running (or auto-start-capable) when this
instruction executes.
"""

from typing import Annotated

import typer

from otto import options
from otto.cli.run import instruction
from otto.configmodule.configmodule import get_host
from otto.logger import get_otto_logger
from otto.result import CommandResult

logger = get_otto_logger()


@options
class _Options:
    on: Annotated[
        str,
        typer.Option(
            "--on",
            help="Container host id to target (e.g. carrot_seed.repo1.api).",
        ),
    ] = "carrot_seed.repo1.api"


@instruction(options=_Options)
async def run_on_container(opts: _Options) -> CommandResult:
    """Cat the repo1 fixture marker from a docker container host."""
    host = get_host(opts.on)
    result = await host.oneshot("cat /etc/repo1-marker.txt")
    logger.info(f"run-on-container [{opts.on}]: {result.value!r}")
    if result.is_ok:
        # Print the raw output so the caller (subprocess test) can assert it.
        print(result.value)  # noqa: T201 — intentional: surface to subprocess stdout
    return result
