"""``otto test``'s repo-wide options for the e2e fixture repo."""

from typing import Annotated

import typer

from otto import options


@options
class E2EFixtureOptions:
    label: Annotated[str, typer.Option(help="Label for the e2e fixture run.")] = "e2e"
