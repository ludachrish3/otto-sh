"""Publishing project instructions as ``otto run`` commands, one merged command per name.

Runs AFTER every repo's init (bootstrap phase 2), because only then is the
flag set known: ``otto run install`` shows the union of every registered
body's options and every class registered for the ``run`` verb, deduplicated
by declaring class, and each body is handed its own class at dispatch
(:func:`otto.project.orchestrator.run_project_instruction`). The publish
registers a factory; the command is built when ``otto run`` resolves it.
"""

import functools
import inspect
from typing import TYPE_CHECKING, Any, cast

import typer

from ..instructions import (
    INSTRUCTIONS,
    PROJECT_INSTRUCTIONS,
    InstructionEntry,
    ProjectInstruction,
)
from ..params import OptionsOrigin, merge_option_params, verb_option_classes

if TYPE_CHECKING:
    from _typeshed import DataclassInstance


def _body_origins(entry: ProjectInstruction) -> list[OptionsOrigin]:
    return [
        OptionsOrigin(body.options_cls, body.repo)
        for body in entry.bodies
        if body.options_cls is not None
    ]


def merged_option_params(entry: ProjectInstruction) -> list[inspect.Parameter]:
    """Merge every body's options fields into one command's parameters."""
    return merge_option_params(
        _body_origins(entry), what=f"project instruction {entry.spec.name!r}"
    )


def _command_for(entry: ProjectInstruction) -> typer.Typer:
    """Build the Typer sub-app whose one command dispatches *entry* through the orchestrator.

    The flags are every body's options fields followed by those of every
    class registered for ``run``, merged by declaring class. The leaf binds
    the parsed values on the active context before the orchestrator runs, so
    a body reads a verb-wide class through ``ctx.options(Cls)``, while each
    body's own class is still built from the same flat values.

    Marked ``DRY_RUN_SELF_FINISHING_ATTR``: this leaf, like every leaf
    ``prepare_command_target`` builds, validates its own options under
    ``--dry-run`` and finishes the dry run itself (see the body below) —
    project instructions have their own dispatch path (this module, not
    ``otto.cli.invoke._wrap_with_options``) and so need the marker stamped on
    by hand. No hidden ``typer.Context`` parameter: the leaf reads the
    current one the same way the wrapper does (see the body below), so its
    public signature is exactly the merged options fields.
    """
    from ..cli.invoke import DRY_RUN_SELF_FINISHING_ATTR, SENSITIVE_FIELDS_ATTR
    from ..params import sensitive_field_names

    name = entry.spec.name
    body_origins = _body_origins(entry)
    verb_origins = verb_option_classes("run")
    params = merge_option_params(
        body_origins + verb_origins,
        what=f"project instruction {name!r}",
    )
    sensitive_names: set[str] = set()
    for origin in body_origins + verb_origins:
        sensitive_names |= sensitive_field_names(cast("type[DataclassInstance]", origin.cls))

    async def _leaf(**kw: Any) -> Any:
        # Function-scope: the orchestrator is heavy and the CLI must stay light.
        from typer._click.globals import get_current_context

        from ..cli.invoke import (
            _leaf_declares_preview,
            command_spec,
            dry_run_requested,
            finish_dry_run,
        )
        from ..context import get_context
        from . import orchestrator

        # ``silent=True``: a test -- or a library caller reaching the leaf
        # directly off `INSTRUCTIONS`, bypassing the CLI entirely -- pushes no
        # click `Context` at all, and never having gone through the CLI seam
        # is exactly the case a dry run cannot apply to, so `None` here IS
        # "not a dry run". The cast: see the identical one in
        # ``otto.cli.invoke``'s own wrapper -- `typer.Context` is the alias
        # this codebase types every ctx parameter with, but the vendored base
        # class `get_current_context` actually returns IS what typer's own
        # commands hand out under that alias at runtime.
        click_ctx = cast("typer.Context | None", get_current_context(silent=True))
        ctx = get_context()
        ctx.bind_verb_options("run", kw)
        if click_ctx is not None and dry_run_requested(click_ctx):
            # Own classes first (one per distinct body, walk order, deduplicated
            # by class) then the verb's OTHER registered classes -- same rule
            # `otto.cli.invoke._wrap_with_options` follows for a standalone
            # instruction. Built from the SAME flat `kw` `bind_verb_options`
            # just used, never `ctx.params` (pre-conversion values).
            own = orchestrator.project_instruction_dry_run_options(name, ctx, kw)
            own_types = {type(instance) for instance in own}
            verb_instances = [
                ctx.options(origin.cls) for origin in verb_origins if origin.cls not in own_types
            ]
            preview = command_spec(click_ctx).dry_run_preview or _leaf_declares_preview(click_ctx)
            await finish_dry_run(click_ctx, own + verb_instances, preview=preview)
        return await orchestrator.run_project_instruction(name, kw)

    _leaf.__name__ = name.replace("-", "_")
    _leaf.__qualname__ = _leaf.__name__
    _leaf.__doc__ = entry.spec.help
    # Both, not just the signature: the completion cache serialises a command
    # from `inspect.signature`, while typer reads the ANNOTATIONS to find each
    # parameter's `typer.Option` metadata.
    _leaf.__signature__ = inspect.Signature(params)  # ty: ignore[unresolved-attribute]
    _leaf.__annotations__ = {p.name: p.annotation for p in params}
    setattr(_leaf, DRY_RUN_SELF_FINISHING_ATTR, True)
    setattr(_leaf, SENSITIVE_FIELDS_ATTR, frozenset(sensitive_names))
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
                make_app=functools.partial(_command_for, entry),
                module=entry.spec.module,
                registered_by=None,
            ),
            overwrite=republish,
            origin=__name__,
        )
