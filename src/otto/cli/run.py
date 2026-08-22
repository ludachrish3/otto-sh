"""``otto run`` subcommand: decorator and Typer app for user-defined run instructions."""

import inspect
from collections.abc import Callable, Coroutine
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ParamSpec,
)

import typer
from rich import print as rprint
from rich.table import Table

from ..instructions import FIRST_PARTY_INSTRUCTIONS, INSTRUCTIONS, InstructionEntry
from ..registry import get_registering_repo
from .invoke import make_registry_group, prepare_command_target

if TYPE_CHECKING:
    from rich.panel import Panel

P = ParamSpec("P")


# `cls=` is set here (module scope, after INSTRUCTIONS exists) rather than via
# a later app.info mutation, so run_app resolves every child instruction
# lazily through the same idiom as the root app's CLI_COMMANDS group.
run_app = typer.Typer(
    name="run",
    no_args_is_help=True,
    cls=make_registry_group(INSTRUCTIONS),
    context_settings={
        "help_option_names": ["-h", "--help"],
    },
)


def first_party_instructions_panel() -> "Panel | None":
    """Build the ``otto defaults`` panel, or None when otto registered nothing.

    Attribution is by MODULE, exactly like ``Repo.get_instructions_panel``'s
    ``init``-prefix match, so every instruction lands in exactly one panel:
    otto's defaults live in ``otto.project.instructions`` and a repo's live
    under its own init modules. Matching on the first-party NAMES instead would
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
    """Handle the eager ``--list-instructions`` flag; real work runs in the leaf preamble.

    Output-dir creation and the reservation gate moved to the shared
    leaf-invoke :func:`~otto.cli.invoke.command_preamble`, so a subcommand
    ``--help`` (which exits before invoke) can never create a spurious dir.
    """
    if ctx.resilient_parsing:
        return


# The handler's PARAMETERS are threaded through unchanged (``P``), so a decorated
# instruction keeps its signature at every call site instead of decaying to
# ``Any`` -- which is what ty's ``dynamic-function-decorator-return`` reports.
# Its RETURN stays ``Any`` on purpose: the leaf-invoke bridge renders whatever a
# handler hands back, and first-party and repo instructions between them already
# return ``CommandResult``, ``Result``, ``None`` and bare payloads. The narrower
# ``CommandResult`` this used to claim was never true and never checked, because
# the outer ``Callable[..., Any]`` erased it before anything could look.
_Handler = Callable[P, Coroutine[Any, Any, Any]]


def instruction(
    *args: Any, options: type | None = None, **kwargs: Any
) -> Callable[[_Handler[P]], _Handler[P]]:
    """Register an async function as an ``otto run`` subcommand.

    The handler must be ``async def`` — a plain ``def`` raises :exc:`TypeError`
    at decoration, because only a coroutine reaches the lifecycle bridge that
    sweeps the instruction's hosts and converts an interrupt into a clean
    exit. ``async def`` is necessary, not sufficient: the interrupt policy is
    driven by the event loop, so a body that blocks it (a bare
    ``subprocess.run``, ``time.sleep``) is no more interruptible than a sync
    one. Lab work belongs in ``await host.…``; local blocking work belongs in
    ``asyncio.to_thread``.

    This is the sugar's check, and THE ASYNC RULE IS THE ONLY ONE THAT IS
    RE-APPLIED when ``otto run`` INVOKES a leaf (``CommandSpec.async_leaves``),
    so a directly-registered ``InstructionEntry``, an ``@run_app.command()``,
    or a sub-group added with ``add_typer`` cannot route around *that*. The
    first-party name guard further down this function has no such twin: it runs
    at decoration or not at all — see the comment beside it for what covers the
    routes it never sees.

    When *options* is a dataclass, the decorator expands its fields (including
    inherited ones) into individual CLI flags — exactly like ``OttoSuite``'s
    auto-registration does for suite options.  The original function must
    declare a parameter annotated with the options class; the decorator
    replaces it with the expanded fields and, at call time, constructs the
    populated dataclass instance before forwarding it to the function.

    If the function declares a parameter annotated as ``OttoContext``, that
    parameter is stripped from the CLI signature and injected at call time from
    the active context (DI-friendly, additive — existing handlers are unaffected).

    Usage without options (unchanged from before)::

        @instruction()
        async def deploy(debug: Annotated[bool, typer.Option()] = False): ...

    Usage with an options dataclass::

        @dataclass
        class _Opts(RepoOptions):
            debug: Annotated[bool, typer.Option()] = False


        @instruction(options=_Opts)
        async def deploy(opts: _Opts):
            print(opts.debug)

    Usage with OttoContext injection::

        @instruction()
        async def status(ctx: OttoContext) -> CommandResult:
            host = ctx.get_host("router")
            ...

    The *same* dataclass may be inherited by a suite's inner ``Options``
    class, giving both ``otto test`` and ``otto run`` subcommands a
    uniform set of repo-wide flags.
    """

    def decorator(func: _Handler[P]) -> _Handler[P]:
        # Checked on `func` itself, with no ``__wrapped__`` unwrap: unlike the
        # group-callback guard in cli/invoke.wrap_leaf_callbacks, which sees a
        # callback typer has already update_wrapper'd, this runs before typer
        # touches anything, so `func` IS the user's function. Stricter than the
        # bridge's own contract (which accepts anything RETURNING a coroutine)
        # and deliberately so: a sync instruction already died at runtime the
        # moment it used ctx or options=, since both _inject_ctx and
        # _wrap_with_options `await func(...)`. This makes a partial, late,
        # confusing failure into a total, early, explained one.
        if not inspect.iscoroutinefunction(func):
            raise TypeError(
                f"instruction {getattr(func, '__name__', repr(func))!r} must be "
                "`async def`: the leaf-invoke bridge detects the COROUTINE a leaf "
                "returns, so a plain `def` registers and runs but never enters the "
                "command lifecycle — hosts it opens are not swept, and SIGINT/SIGTERM "
                "are not turned into a clean exit. A body with nothing to await is "
                "still correct as `async def`; a wrapper around an async function "
                "should itself be `async def` and await it."
            )

        # No self-wrapping: the registered async handler runs under the command
        # lifecycle via the leaf-invoke wrapper's coroutine bridge
        # (cli/invoke._wrap_invoke) when `otto run <name>` dispatches it.
        target = prepare_command_target(func, options)
        app = typer.Typer()
        new_instruction = app.command(*args, **kwargs)(target)

        # Mirror Typer's own name derivation (typer.main.get_command_name):
        # explicit name (positional or name= kwarg) wins, else the function
        # name with underscores replaced with dashes. Getting this wrong
        # would silently break `otto run <name>` for existing callers.
        func_name = getattr(func, "__name__", repr(func))
        explicit_name = args[0] if args and isinstance(args[0], str) else kwargs.get("name")
        cmd_name = explicit_name or typer.main.get_command_name(func_name)

        # A repo may not claim a first-party name. Overriding lab behavior
        # happens in ProjectActions -- which `otto run install` AND the
        # ensure_installed fixture both route through -- so shadowing the
        # instruction would move only the CLI half and let the two answer
        # differently. Refused BEFORE the register call below: otherwise the
        # repo's entry lands first and the collision surfaces (if at all) as
        # the registry's generic "already registered", which says nothing
        # about where the override belongs.
        #
        # Keyed on the registering-repo marker, never on the name alone:
        # otto's own registration runs outside any repo's init (bootstrap
        # phase 2) and must pass whatever order the imports happen in.
        #
        # THIS GUARD COVERS THE DECORATOR AND NOTHING ELSE. A repo that builds
        # an InstructionEntry and calls INSTRUCTIONS.register() itself never
        # reaches this line. What stops it there is bootstrap's ORDER —
        # otto.project.instructions is imported before any repo init, so the
        # six names are already taken and the registry refuses the second
        # registration — with the registry's generic "already registered",
        # which is exactly the message this guard exists to improve on. The
        # order is load-bearing on that route, not belt-and-braces.
        repo_name = get_registering_repo()
        if repo_name is not None and cmd_name in FIRST_PARTY_INSTRUCTIONS:
            raise ValueError(
                f"repo {repo_name!r} defines instruction {cmd_name!r}, which is a "
                "first-party default. Override lab behavior by registering a "
                "ProjectActions subclass instead (see docs/guide/run/defaults.md), "
                "or rename the instruction."
            )

        func_module = getattr(func, "__module__", "<unknown>")
        INSTRUCTIONS.register(
            cmd_name,
            InstructionEntry(name=cmd_name, sub_app=app, module=func_module),
            origin=func_module,
        )
        return new_instruction

    return decorator
