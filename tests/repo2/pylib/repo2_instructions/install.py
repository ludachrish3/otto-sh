from typing import (
    Annotated,
)

import typer

from otto.cli.run import instruction
from otto.logger import get_logger

logger = get_logger()


@instruction()
def install_repo2(
    debug: Annotated[
        bool,
        typer.Option(
            "--field/--debug",
            help="Use field or debug products.",
        ),
    ] = False,
):

    logger.info(f"This is a test instruction in repo2: {debug}")
