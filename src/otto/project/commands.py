"""Project instructions as ``otto run`` commands: one merged command per name.

``otto.instruction_catalog.INSTRUCTIONS`` derives one command per project instruction
from the declared bodies; ``otto.cli.run`` builds it when ``otto run`` resolves
it. Its flags are the union of every body's options and every class
registered for the ``run`` verb, deduplicated by declaring class, and each
body is handed its own class at dispatch
(:func:`otto.project.orchestrator.run_project_instruction`).
Two bodies' options that clash are found when that one command's flags are
built, so the clash stops that command and no other.
"""

from typing import TYPE_CHECKING

from ..instruction_catalog import ProjectInstruction

if TYPE_CHECKING:
    from ..params import OptionsOrigin


def body_origins(entry: ProjectInstruction) -> "list[OptionsOrigin]":
    """List each body's options class with the project that declared it, in walk order."""
    # Function-scope: `otto.params` imports typer, and this module stays CLI-free.
    from ..params import OptionsOrigin

    return [
        OptionsOrigin(body.options_cls, body.project)
        for body in entry.bodies
        if body.options_cls is not None
    ]
