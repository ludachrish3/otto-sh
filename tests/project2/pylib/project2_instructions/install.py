import logging
from typing import (
    Annotated,
)

import typer

from otto.instructions import instruction

logger = logging.getLogger(__name__)


@instruction()
async def install_project2(
    debug: Annotated[
        bool,
        typer.Option(
            "--field/--debug",
            help="Use field or debug products.",
        ),
    ] = False,
):

    logger.info(f"This is a test instruction in project2: {debug}")
