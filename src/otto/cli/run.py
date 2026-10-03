"""``otto run`` subcommand: the Typer app for user-defined run instructions."""

import inspect
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any, cast

import typer
from rich import print as rprint

from ..instructions import INSTRUCTIONS, InstructionEntry, ProjectInstruction

# Re-export: the documented import moved to otto.instructions.
from ..instructions import instruction as instruction  # noqa: PLC0414 — explicit re-export
from .invoke import make_registry_group, prepare_command_target

if TYPE_CHECKING:
    from _typeshed import DataclassInstance
    from rich.panel import Panel


def _project_leaf(project: ProjectInstruction) -> Callable[..., Any]:
    """Build the leaf that dispatches project instruction *project* through the orchestrator.

    The flags are every body's options fields followed by those of every
    class registered for ``run``, merged by declaring class. The leaf binds
    the parsed values on the active context before the orchestrator runs, so
    a body reads a verb-wide class through ``ctx.options(Cls)``, while each
    body's own class is still built from the same flat values.

    Marked ``DRY_RUN_SELF_FINISHING_ATTR``: this leaf, like every leaf
    ``prepare_command_target`` builds, validates its own options under
    ``--dry-run`` and finishes the dry run itself (see the body below) --
    project instructions have their own dispatch path (this function, not
    ``otto.cli.invoke._wrap_with_options``) and so need the marker stamped on
    by hand. No hidden ``typer.Context`` parameter: the leaf reads the
    current one the same way the wrapper does (see the body below), so its
    public signature is exactly the merged options fields.
    """
    from ..params import merge_option_params, sensitive_field_names, verb_option_classes
    from ..project.commands import body_origins
    from .invoke import DRY_RUN_SELF_FINISHING_ATTR, SENSITIVE_FIELDS_ATTR

    name = project.spec.name
    own_origins = body_origins(project)
    verb_origins = verb_option_classes("run")
    params = merge_option_params(
        own_origins + verb_origins,
        what=f"project instruction {name!r}",
    )
    sensitive_names: set[str] = set()
    for origin in own_origins + verb_origins:
        sensitive_names |= sensitive_field_names(cast("type[DataclassInstance]", origin.cls))

    async def _leaf(**kw: Any) -> Any:
        # Function-scope: the orchestrator is heavy and the CLI must stay light.
        from typer._click.globals import get_current_context

        from ..context import get_context
        from ..params import OptionsValidationError
        from ..project import orchestrator
        from .invoke import (
            _leaf_declares_preview,
            command_spec,
            dry_run_requested,
            finish_dry_run,
            usage_error_from,
        )

        # ``silent=True``: a test -- or a library caller reaching the leaf
        # directly, bypassing the CLI entirely -- pushes no click `Context` at
        # all, and never having gone through the CLI seam is exactly the case
        # a dry run cannot apply to, so `None` here IS "not a dry run". The
        # cast: see the identical one in ``otto.cli.invoke``'s own wrapper --
        # `typer.Context` is the alias this codebase types every ctx parameter
        # with, but the vendored base class `get_current_context` actually
        # returns IS what typer's own commands hand out under that alias at
        # runtime.
        click_ctx = cast("typer.Context | None", get_current_context(silent=True))
        dry = click_ctx is not None and dry_run_requested(click_ctx)
        ctx = get_context()
        try:
            ctx.bind_verb_options("run", kw)
            # Validates every applicable body's own options class up front --
            # the SAME per-repo walk the real path's `_run_bodies` performs --
            # so a bad value in a body's own class fails identically whether
            # or not `--dry-run` was requested, before any body runs. Under a
            # real run the result is otherwise unused and the run's own walk
            # announces the skips, so this one stays quiet; under a dry run it
            # is the only walk, so it announces, and the branch below prints it.
            own = orchestrator.project_instruction_body_options(name, ctx, kw, announce=dry)
        except OptionsValidationError as e:
            raise usage_error_from(e) from e
        if click_ctx is not None and dry:
            # Own classes first (one per distinct body, walk order, deduplicated
            # by class) then the verb's OTHER registered classes -- same rule
            # `otto.cli.invoke._wrap_with_options` follows for a standalone
            # instruction. Built from the SAME flat `kw` `bind_verb_options`
            # just used, never `ctx.params` (pre-conversion values).
            own_types = {type(instance) for instance in own}
            verb_instances = [
                ctx.options(origin.cls) for origin in verb_origins if origin.cls not in own_types
            ]
            if project.spec.dry_run_preview:
                from rich import get_console

                from ..project.plan import plan_instruction
                from ..project.render import render_plan

                # Verbatim: a plan line is a host command, so a `[` must not be read
                # as markup, a `:name:` as an emoji, or a long line hard-wrapped.
                text = render_plan(name, plan_instruction(name, ctx, kw))
                if text:  # no repo applies: print nothing, not a blank line
                    get_console().print(
                        text, markup=False, emoji=False, highlight=False, soft_wrap=True
                    )
            preview = command_spec(click_ctx).dry_run_preview or _leaf_declares_preview(click_ctx)
            await finish_dry_run(click_ctx, own + verb_instances, preview=preview)
        return await orchestrator.run_project_instruction(name, kw)

    _leaf.__name__ = name.replace("-", "_")
    _leaf.__qualname__ = _leaf.__name__
    _leaf.__doc__ = project.spec.help
    # Both, not just the signature: the completion cache serialises a command
    # from `inspect.signature`, while typer reads the ANNOTATIONS to find each
    # parameter's `typer.Option` metadata.
    _leaf.__signature__ = inspect.Signature(params)  # ty: ignore[unresolved-attribute]
    _leaf.__annotations__ = {p.name: p.annotation for p in params}
    setattr(_leaf, DRY_RUN_SELF_FINISHING_ATTR, True)
    setattr(_leaf, SENSITIVE_FIELDS_ATTR, frozenset(sensitive_names))
    return _leaf


def build_instruction_app(entry: InstructionEntry) -> typer.Typer:
    """Build ``otto run <name>``'s Typer app from its registry entry, on resolution.

    Built here and not at registration: the flags include every options
    class registered for ``run``, which a later init module may still add,
    and resolving those classes may import their modules -- a cost only
    ``otto run`` should pay. A clash between the instruction's own fields
    and the verb's is found here, before any body runs.
    """
    app = typer.Typer()
    if entry.project is not None:
        app.command(entry.name, help=entry.project.spec.help)(_project_leaf(entry.project))
        return app
    if entry.handler is None:  # unreachable: __post_init__ requires handler xor project
        raise ValueError(f"instruction {entry.name!r} has neither handler nor project set")
    target = prepare_command_target(
        entry.handler, entry.options_cls, verb="run", repo=entry.registered_by
    )
    app.command(entry.name, help=entry.help)(target)
    return app


# `cls=` is set here (module scope, after INSTRUCTIONS exists) rather than via
# a later app.info mutation, so run_app resolves every child instruction
# lazily through the same idiom as the root app's CLI_COMMANDS group.
run_app = typer.Typer(
    name="run",
    no_args_is_help=True,
    cls=make_registry_group(INSTRUCTIONS, app_of=build_instruction_app),
    context_settings={
        "help_option_names": ["-h", "--help"],
    },
)


def first_party_instructions_panel() -> "Panel | None":
    """Build the ``otto defaults`` panel, or None when otto registered nothing.

    Attribution is by MODULE, exactly like ``Repo.get_instructions_panel``'s
    ``init``-prefix match, so every instruction lands in exactly one panel:
    otto's defaults are published by ``otto.project.commands`` under
    ``otto.project.actions`` and a repo's live under its own init modules.
    Matching on the first-party NAMES instead would
    put a repo's instruction in otto's panel the day one slips past the
    decorator's guard -- hiding the very collision the guard exists to shout
    about.

    None rather than an empty panel: otto always has defaults in a real run
    (bootstrap imports them), so an empty one only ever appears in a test that
    stripped the registry, and advertising a section with nothing in it reads
    like otto lost them.
    """
    from rich.panel import Panel
    from rich.text import Text

    names = [entry.name for _, entry in INSTRUCTIONS.items() if entry.module.startswith("otto.")]
    if not names:
        return None
    # No subtitle, where a repo panel carries its dependency summary: panels
    # share the terminal's width, so anything written there is truncated
    # mid-sentence as soon as a second repo shows up.
    return Panel(
        Text("\n".join(f"• {name}" for name in names)),
        title=Text("otto defaults", style="bold not dim"),
        border_style="dim",
        padding=(1, 5, 1, 1),
        expand=True,
    )


def list_instructions_callback(value: bool) -> None:
    """Print all available run instructions (one panel per repo) and exit when the flag is set."""
    if not value:
        return
    from rich.table import Table

    from ..config import get_repos  # lazy import — avoids circular dependency

    panels = [repo.get_instructions_panel() for repo in get_repos()]
    # Ahead of the repos: these are the verbs every lab has, and the ones a
    # reader must recognize as taken before writing an instruction of their own.
    first_party = first_party_instructions_panel()
    if first_party is not None:
        panels.insert(0, first_party)
    table = Table(show_header=False, show_footer=False, box=None, expand=True, padding=(0, 1, 1, 1))
    for _ in panels:
        table.add_column(ratio=1)
    table.add_row(*panels)
    rprint(table)
    raise typer.Exit


@run_app.callback()
def main(
    ctx: typer.Context,
    list_instructions: Annotated[  # noqa: ARG001 — required by Typer eager callback option signature
        bool,
        typer.Option(
            "--list-instructions",
            callback=list_instructions_callback,
            is_eager=True,
            help="List available instructions and exit.",
        ),
    ] = False,
) -> None:
    """Run a registered instruction on the lab; `--list-instructions` shows what is available."""
    # Developer note: this callback only handles the eager `--list-instructions`
    # flag; the real work runs in the leaf preamble. Output-dir creation and the
    # reservation gate live in the shared leaf-invoke
    # `otto.cli.invoke.command_preamble`, so a subcommand `--help` (which exits
    # before invoke) can never create a spurious dir.
    if ctx.resilient_parsing:
        return
