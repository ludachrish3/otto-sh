"""``otto run`` subcommand: decorator and Typer app for user-defined run instructions."""

import dataclasses
from collections.abc import Callable, Coroutine
from typing import (
    Annotated,
    Any,
    ParamSpec,
)

import typer
from rich import print as rprint
from rich.table import Table

from ..registry import Registry
from ..result import CommandResult
from ..utils import async_typer_command
from .invoke import make_registry_group, prepare_command_target

P = ParamSpec("P")


@dataclasses.dataclass(frozen=True)
class InstructionEntry:
    """One registered instruction: its Typer sub-app + defining module."""

    name: str
    sub_app: typer.Typer
    module: str


# ---------------------------------------------------------------------------
# Module-level registry — populated by @instruction() as init modules are
# imported during startup; consumed lazily by run_app's RegistryBackedGroup.
# ---------------------------------------------------------------------------
INSTRUCTIONS: Registry[InstructionEntry] = Registry(
    "instruction", register_hint="@otto.instruction()"
)

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


def list_instructions_callback(value: bool) -> None:
    """Print all available run instructions (one panel per repo) and exit when the flag is set."""
    if not value:
        return
    from ..configmodule import get_repos  # lazy import — avoids circular dependency

    panels = [repo.get_instructions_panel() for repo in get_repos()]
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


def instruction(*args: Any, options: type | None = None, **kwargs: Any) -> Callable[..., Any]:
    """Register an async function as an ``otto run`` subcommand.

    When *options* is a dataclass, the decorator expands its fields (including
    inherited ones) into individual CLI flags — exactly like
    ``@register_suite()`` does for suite options.  The original function must
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

    def decorator(
        func: Callable[P, Coroutine[Any, Any, CommandResult]],
    ) -> Callable[P, CommandResult]:
        target = prepare_command_target(func, options)
        app = typer.Typer()
        new_instruction = app.command(*args, **kwargs)(async_typer_command(target))

        # Mirror Typer's own name derivation (typer.main.get_command_name):
        # explicit name (positional or name= kwarg) wins, else the function
        # name with underscores replaced with dashes. Getting this wrong
        # would silently break `otto run <name>` for existing callers.
        func_name = getattr(func, "__name__", repr(func))
        explicit_name = args[0] if args and isinstance(args[0], str) else kwargs.get("name")
        cmd_name = explicit_name or typer.main.get_command_name(func_name)

        func_module = getattr(func, "__module__", "<unknown>")
        INSTRUCTIONS.register(
            cmd_name,
            InstructionEntry(name=cmd_name, sub_app=app, module=func_module),
            origin=func_module,
        )
        return new_instruction

    return decorator
