"""Publishing project instructions as ``otto run`` commands, one merged command per name.

Runs AFTER every repo's init (bootstrap phase 2), because only then is the
flag set known: ``otto run install`` shows the union of every registered
body's options, deduplicated by declaring class, and each body is handed its
own class at dispatch (:func:`otto.project.orchestrator.run_project_instruction`).
"""

import inspect
from typing import Any

import typer

from ..errors import OttoError
from ..instructions import (
    INSTRUCTIONS,
    PROJECT_INSTRUCTIONS,
    InstructionEntry,
    ProjectInstruction,
)
from ..params import declaring_class, options_params


class OptionsCollisionError(OttoError):
    """Two repos declared the same field name on one project instruction from different classes."""


def _who(repo: str | None) -> str:
    return "otto" if repo is None else f"repo {repo!r}"


def merged_option_params(entry: ProjectInstruction) -> list[inspect.Parameter]:
    """Merge every body's options fields, one parameter per declaring class.

    Bodies contribute in registration order (otto first, then repos in
    dependency order), so first-party flags lead the ``--help``. A field two
    classes inherit from ONE base is one parameter; the same name introduced
    by two classes is a collision, refused here so the user learns at
    bootstrap rather than by one repo silently reading the other's value.
    """
    seen: dict[str, tuple[type, str | None]] = {}
    params: list[inspect.Parameter] = []
    for body in entry.bodies:
        if body.options_cls is None:
            continue
        for param in options_params(body.options_cls):
            declared = declaring_class(body.options_cls, param.name)
            if param.name in seen:
                prior, prior_repo = seen[param.name]
                if prior is not declared:
                    raise OptionsCollisionError(
                        f"project instruction {entry.spec.name!r}: field {param.name!r} is "
                        f"declared by both {_who(prior_repo)} "
                        f"({prior.__module__}.{prior.__qualname__}) and {_who(body.repo)} "
                        f"({declared.__module__}.{declared.__qualname__}); share one base "
                        "class, in a required dependency or a library package, or rename "
                        "the field"
                    )
                continue
            seen[param.name] = (declared, body.repo)
            params.append(param.replace(kind=inspect.Parameter.KEYWORD_ONLY))
    return params


def _command_for(entry: ProjectInstruction) -> typer.Typer:
    """Build the Typer sub-app whose one command dispatches *entry* through the orchestrator."""
    name = entry.spec.name
    params = merged_option_params(entry)

    async def _leaf(**kw: Any) -> Any:
        # Function-scope: the orchestrator is heavy and the CLI must stay light.
        from . import orchestrator

        return await orchestrator.run_project_instruction(name, kw)

    _leaf.__name__ = name.replace("-", "_")
    _leaf.__qualname__ = _leaf.__name__
    _leaf.__doc__ = entry.spec.help
    # Both, not just the signature: the completion cache serialises a command
    # from `inspect.signature`, while typer reads the ANNOTATIONS to find each
    # parameter's `typer.Option` metadata.
    _leaf.__signature__ = inspect.Signature(params)  # ty: ignore[unresolved-attribute]
    _leaf.__annotations__ = {p.name: p.annotation for p in params}
    app = typer.Typer()
    app.command(name, help=entry.spec.help)(_leaf)
    return app


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
        republish = name in INSTRUCTIONS and INSTRUCTIONS.origin(name) == __name__
        INSTRUCTIONS.register(
            name,
            InstructionEntry(
                name=name,
                sub_app=_command_for(entry),
                module=entry.spec.module,
                registered_by=None,
            ),
            overwrite=republish,
            origin=__name__,
        )
