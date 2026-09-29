"""Publishing project instructions as ``otto run`` commands, one merged command per name.

Runs AFTER every repo's init (bootstrap phase 2), because only then is the
flag set known: ``otto run install`` shows the union of every registered
body's options and every class registered for the ``run`` verb, deduplicated
by declaring class, and each body is handed its own class at dispatch
(:func:`otto.project.orchestrator.run_project_instruction`). The publish
registers a data entry; ``otto.cli.run`` builds the command when ``otto run``
resolves it.
"""

import inspect
from typing import TYPE_CHECKING

from ..instructions import (
    INSTRUCTIONS,
    PROJECT_INSTRUCTIONS,
    InstructionEntry,
    ProjectInstruction,
)

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


def publish_project_instructions() -> None:
    """Register one ``otto run`` command per project instruction into ``INSTRUCTIONS``.

    ``registered_by`` is None for every one of them, first-party or repo-added:
    the dispatch gate refuses an instruction whose OWNER is inactive, but a
    project instruction has a body per repo and the walk's own applicability
    filter skips each inactive one. ``module`` is the first declarer's, which
    is what ``--list-instructions`` attributes panels by.

    A name this module published before is re-published; any OTHER prior
    entry is left for the registry to refuse -- that is the backstop for a
    repo that hand-built an ``InstructionEntry`` under a project instruction's
    name, exactly as the pre-import of the six used to be.

    THE REPUBLISH KEY IS THE REGISTRY'S OWN ATTRIBUTION, not the entry's
    ``module``: a repo that declares a project instruction in ``widget.init``
    AND hand-registers an entry under the same name from that same module
    would otherwise match on ``module`` and be overwritten silently -- the one
    case the backstop exists for.
    """
    for name, entry in PROJECT_INSTRUCTIONS.items():
        # The bodies' own clash is a startup error, found here. The command
        # itself -- whose flags add the `run` verb's classes -- is built when
        # `otto run` resolves it, the only path that should pay for resolving
        # those classes.
        merged_option_params(entry)
        republish = name in INSTRUCTIONS and INSTRUCTIONS.origin(name) == __name__
        INSTRUCTIONS.register(
            name,
            InstructionEntry(
                name=name,
                module=entry.spec.module,
                project=entry,
                registered_by=None,
            ),
            overwrite=republish,
            origin=__name__,
        )
