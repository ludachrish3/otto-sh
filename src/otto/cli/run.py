"""``otto run`` subcommand: the Typer app for user-defined run instructions."""

import inspect
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any, cast

import typer
from rich import print as rprint

from ..config._typer import get_current_context
from ..instruction_catalog import (
    INSTRUCTION_GROUP,
    INSTRUCTIONS,
    InstructionEntry,
    ProjectInstruction,
)

# Re-export: the documented import is otto.instructions; this spelling predates it.
from ..instruction_catalog import instruction as instruction  # noqa: PLC0414 — explicit re-export
from .invoke import make_registry_group, prepare_command_target

if TYPE_CHECKING:
    from rich.panel import Panel


def _project_leaf(project: ProjectInstruction) -> Callable[..., Any]:
    """Build the leaf that dispatches project instruction *project* through the orchestrator.

    Its signature is empty: the flags are every body's options fields, which
    the leaf says are its own (``OwnOptions.bodies``), and those of every
    class covering ``otto run <name>``, attached for each invocation and
    merged by declaring class (``otto.cli.invoke.offered_options``). The
    invocation prepares them before the leaf runs, the covering classes
    built and the body classes recorded for the walk
    (``otto.cli.invoke.prepare_command_options``); called by other code, the
    leaf prepares them itself, every value given and every project kept.
    It then builds every applicable body's own class once, from that
    record, before any body runs -- so a bad value fails the same way with
    or without ``--dry-run`` -- and keeps each instance in the record, so the
    run's walk hands the same objects back to the bodies and a hook reads
    them with ``otto.get_options``. A flag nobody typed reaches only the
    classes the command offers. Under ``--dry-run`` the leaf finishes the
    dry run itself (see the body below).
    """
    from ..instruction_catalog import instruction_path
    from ..project.commands import body_origins
    from .invoke import OWN_OPTIONS_ATTR, OwnOptions

    name = project.spec.name
    own = OwnOptions(
        bodies=lambda: body_origins(project), finishes_dry_run=True, every_project=True
    )

    async def _leaf(**kw: Any) -> Any:
        # Function-scope: the orchestrator is heavy and the CLI must stay light.
        from ..params import OptionsValidationError, options_snapshot
        from ..project import orchestrator
        from ..run_context import get_context
        from .invoke import (
            _leaf_declares_preview,
            command_spec,
            dry_run_requested,
            finish_dry_run,
            leaf_preparation,
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
        prepared = leaf_preparation(_leaf, kw, own, instruction_path(name))
        with prepared.scope():
            ctx = get_context()
            try:
                # Every applicable body's own class, built once from the record
                # -- the SAME per-project walk the run performs -- so a bad value
                # fails before any body runs. Under a real run the run's own
                # walk announces the skips, so this one stays quiet; under a dry
                # run it is the only walk, so it announces.
                walk = orchestrator.prepare_project_instruction(
                    name, ctx, kw, announce=dry, source=options_snapshot(prepared.recorded)
                )
                own_instances = walk.own
            except OptionsValidationError as e:
                raise usage_error_from(e) from e
            # A class the command offers is prepared once and travels with the
            # recorded flags: the run's walk is handed these very objects.
            for instance in own_instances:
                prepared.recorded.instances[type(instance)] = instance
            if click_ctx is not None and dry:
                # Own classes first (one per distinct body, walk order) then the
                # covering classes -- the order a standalone instruction's
                # wrapper shows its own class and the covering ones in.
                own_types = {type(instance) for instance in own_instances}
                covering_instances = [
                    instance for cls, instance in prepared.instances.items() if cls not in own_types
                ]
                if project.spec.dry_run_preview:
                    from ..console import print_literal
                    from ..project.plan import plan_instruction, plan_lab_tail
                    from ..project.render import render_plan

                    # Verbatim: a plan line is a host command, so a `[` must not be
                    # read as markup, a `:name:` as an emoji, or a long line
                    # hard-wrapped.
                    text = render_plan(
                        name,
                        plan_instruction(name=name, ctx=ctx, kwargs=kw, recorded=prepared.recorded),
                        plan_lab_tail(name=name, ctx=ctx, kwargs=kw, recorded=prepared.recorded),
                    )
                    if text:  # nothing applies: print nothing, not a blank line
                        print_literal(text)
                preview = command_spec(click_ctx).dry_run_preview or _leaf_declares_preview(
                    click_ctx
                )
                await finish_dry_run(click_ctx, own_instances + covering_instances, preview=preview)
            return await orchestrator.run_project_instruction(name, kw, source=walk.source)

    _leaf.__name__ = name.replace("-", "_")
    _leaf.__qualname__ = _leaf.__name__
    _leaf.__doc__ = project.spec.help
    _leaf.__signature__ = inspect.Signature([])  # ty: ignore[missing-attribute]
    setattr(_leaf, OWN_OPTIONS_ATTR, own)
    return _leaf


def instruction_help(entry: InstructionEntry) -> str | None:
    """Return the help ``otto run <name>`` shows for *entry*: the one place it is chosen.

    A project instruction's declared help; else the entry's ``help=``, else
    its handler's docstring.
    """
    if entry.project is not None:
        return entry.project.spec.help
    return entry.help or inspect.getdoc(entry.handler)


def build_instruction_app(entry: InstructionEntry) -> typer.Typer:
    """Build ``otto run <name>``'s Typer app from its registry entry, on resolution.

    The command holds its own parameters only: the flags of the classes
    covering ``otto run <name>`` (and a project instruction's body classes)
    are attached for each invocation, and resolved only when a parse needs
    them (``otto.cli.invoke.offered_options``). Nothing is resolved here. A
    clash between the instruction's own fields and a covering class's is
    found by that parse, before any body runs.

    *entry* must already be registered: its owning project, which the dispatch
    gate reads, is looked up in ``INSTRUCTIONS`` by name, and an unregistered
    name raises rather than running ownerless.
    """
    from ..instruction_catalog import instruction_path

    app = typer.Typer()
    if entry.project is not None:
        app.command(entry.name, help=instruction_help(entry))(_project_leaf(entry.project))
        return app
    if entry.handler is None:  # unreachable: __post_init__ requires handler xor project
        raise ValueError(f"instruction {entry.name!r} has neither handler nor project set")
    target = prepare_command_target(
        entry.handler,
        entry.options_cls,
        project=INSTRUCTIONS.project(name=entry.name),
        path=instruction_path(entry.name),
    )
    app.command(entry.name, help=instruction_help(entry))(target)
    return app


# `cls=` is set here (module scope, after INSTRUCTIONS exists) rather than via
# a later app.info mutation, so run_app resolves every child instruction
# lazily through the same idiom as the root app's CLI_COMMANDS group.
run_app = typer.Typer(
    name=INSTRUCTION_GROUP,
    no_args_is_help=True,
    cls=make_registry_group(INSTRUCTIONS, app_of=build_instruction_app),
    context_settings={
        "help_option_names": ["-h", "--help"],
    },
)


def first_party_instructions_panel() -> "Panel | None":
    """Build the ``otto defaults`` panel, or None when otto registered nothing.

    Attribution is by MODULE, exactly like ``Project.get_instructions_panel``'s
    ``init``-prefix match, so every instruction lands in exactly one panel:
    otto's defaults are declared in ``otto.project.actions`` and a project's
    live under its own init modules.
    Matching on the first-party NAMES instead would
    put a project's instruction in otto's panel the day one slips past the
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
    # No subtitle, where a project panel carries its dependency summary: panels
    # share the terminal's width, so anything written there is truncated
    # mid-sentence as soon as a second project shows up.
    return Panel(
        Text("\n".join(f"• {name}" for name in names)),
        title=Text("otto defaults", style="bold not dim"),
        border_style="dim",
        padding=(1, 5, 1, 1),
        expand=True,
    )


def list_instructions_callback(ctx: typer.Context, value: bool) -> None:
    """Print every run instruction (one panel per participating project) and exit.

    The projects are the listing's own (:func:`otto.cli.invoke.listing_participation`):
    with a lab, those active for it; without one, the pre-lab selection. A
    project left out gets no panel.
    """
    if not value or ctx.resilient_parsing:
        return
    from rich.table import Table

    # The composition root: importing it installs the initiator that composes
    # the projects this reads (spec 2026-10-11 placement-and-phases §3.12).
    from .. import bootstrap

    del bootstrap
    from ..declared import get_projects
    from .invoke import listing_participation

    panels = [
        project.get_instructions_panel()
        for project in listing_participation(ctx, get_projects()).kept
    ]
    # Ahead of the projects: these are the verbs every lab has, and the ones a
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
