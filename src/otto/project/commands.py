"""Project instructions as ``otto run`` commands: one merged command per name.

``otto.instructions.INSTRUCTIONS`` derives one command per project instruction
from the declared bodies; ``otto.cli.run`` builds it when ``otto run`` resolves
it. Its flags are the union of every body's options and every class
registered for the ``run`` verb, deduplicated by declaring class, and each
body is handed its own class at dispatch
(:func:`otto.project.orchestrator.run_project_instruction`).
:func:`check_project_instruction_options` checks the bodies' options once
every repo's init has run (bootstrap), so a clash is a startup error.
"""

import inspect
from typing import TYPE_CHECKING

from ..instructions import PROJECT_INSTRUCTIONS, ProjectInstruction

if TYPE_CHECKING:
    from ..params import OptionsOrigin


def body_origins(entry: ProjectInstruction) -> "list[OptionsOrigin]":
    """List each body's options class with the repo that declared it, in walk order."""
    # Function-scope: `otto.params` imports typer, and this module stays CLI-free.
    from ..params import OptionsOrigin

    return [
        OptionsOrigin(body.options_cls, body.repo)
        for body in entry.bodies
        if body.options_cls is not None
    ]


def merged_option_params(entry: ProjectInstruction) -> list[inspect.Parameter]:
    """Merge every body's options fields into one command's parameters."""
    from ..params import merge_option_params

    return merge_option_params(body_origins(entry), what=f"project instruction {entry.spec.name!r}")


def check_project_instruction_options() -> None:
    """Merge every project instruction's body options, refusing a clash.

    Run once every repo has spoken (bootstrap, after the init loop): a repo's
    override is what ADDS flags to a merged command, so only then is the flag
    set known. Reads the tables and writes nothing, so running it again is
    harmless. The command itself -- whose flags add the ``run`` verb's
    classes -- is built when ``otto run`` resolves it, the only path that
    should pay for resolving those classes.

    Raises:
        otto.params.OptionsCollisionError: Two bodies' options classes declare
            the same field from different classes; the message names both
            repos.
    """
    for name in PROJECT_INSTRUCTIONS.names():
        merged_option_params(PROJECT_INSTRUCTIONS.get(name))
