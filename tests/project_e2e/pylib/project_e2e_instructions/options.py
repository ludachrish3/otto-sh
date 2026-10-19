"""``otto test``'s project-wide options for the e2e fixture project."""

from typing import Annotated

import typer

from otto import options


@options
class E2EFixtureOptions:
    label: Annotated[str, typer.Option(help="Label for the e2e fixture run.")] = "e2e"
