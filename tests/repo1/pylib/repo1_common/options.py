"""Repo-wide options shared by every repo1 suite and instruction.

Inherit ``RepoOptions`` from your suite's inner ``Options`` dataclass or
from the dataclass you pass to ``@instruction(options=...)`` and every
field below becomes a CLI flag on both ``otto test`` and ``otto run``
subcommands.
"""

from dataclasses import dataclass
from typing import Annotated

import typer


@dataclass
class RepoOptions:
    device_type: Annotated[str, typer.Option(
        help="Type of device under test (e.g. 'router', 'switch').",
    )] = "router"

    lab_env: Annotated[str, typer.Option(
        help="Lab environment to target (e.g. 'staging', 'production').",
    )] = "staging"
