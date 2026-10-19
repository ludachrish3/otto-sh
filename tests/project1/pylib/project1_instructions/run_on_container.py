"""Instruction that executes a single command on a docker container host.

Intended for e2e tests that verify ``otto run`` drives a real
``DockerContainerHost`` end-to-end.  The instruction is intentionally
minimal: it cats a fixture marker file and returns the output, letting the
test assert specific text in the subprocess stdout.

Run it against the unix lab::

    OTTOPATH=<project1 dir> otto -l unix run run-on-container \\
        --on <element>.project1.api

The container must already be running when this instruction executes
(``otto docker compose up project1`` first): otto never starts one on use.
"""

import logging
from typing import Annotated

import typer

from otto import options
from otto.fleet import get_host
from otto.instructions import instruction
from otto.result import CommandResult

logger = logging.getLogger(__name__)


@options
class _Options:
    on: Annotated[
        str,
        typer.Option(
            "--on",
            help="Container host id to target (e.g. test1.project1.api).",
        ),
    ] = "test1.project1.api"


@instruction(options=_Options)
async def run_on_container(opts: _Options) -> CommandResult:
    """Cat the project1 fixture marker from a docker container host."""
    host = get_host(opts.on)
    result = await host.exec("cat /etc/project1-marker.txt")
    logger.info(f"run-on-container [{opts.on}]: {result.value!r}")
    if result.is_ok:
        # Print the raw output so the caller (subprocess test) can assert it.
        print(result.value)  # noqa: T201 — intentional: surface to subprocess stdout
    return result
