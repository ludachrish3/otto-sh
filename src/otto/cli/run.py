import dataclasses
import functools
import inspect
from typing import (
    Annotated,
    Any,
    Callable,
    Coroutine,
    ParamSpec,
    get_type_hints,
)

import typer
from rich import print as rprint
from rich.table import Table

from ..logger import getOttoLogger
from ..params import options_params
from ..utils import (
    CommandStatus,
    async_typer_command,
)

logger = getOttoLogger()

P = ParamSpec("P")

run_app = typer.Typer(
    name='run',
    no_args_is_help=True,
    context_settings={
        'help_option_names': ['-h', '--help'],
    },
)


def list_instructions_callback(value: bool) -> None:
    if not value:
        return
    from ..configmodule import getRepos  # lazy import — avoids circular dependency
    panels = [repo.getInstructionsPanel() for repo in getRepos()]
    table = Table(show_header=False, show_footer=False, box=None, expand=True, padding=(0, 1, 1, 1))
    for _ in panels:
        table.add_column(ratio=1)
    table.add_row(*panels)
    rprint(table)
    raise typer.Exit()


@run_app.callback(
)
def main(
    ctx: typer.Context,
    list_instructions: Annotated[bool,
        typer.Option('--list-instructions',
            callback=list_instructions_callback,
            is_eager=True,
            help='List available instructions and exit.',
        )
    ] = False,
):
    if ctx.resilient_parsing:
        return

    if ctx.invoked_subcommand is not None:
        logger.create_output_dir("run", f"{ctx.invoked_subcommand}")
        from ..configmodule import tryGetConfigModule
        from ..reservations import gate
        gate(tryGetConfigModule())


def instruction(*args: Any, options: type | None = None, **kwargs: Any):
    """Register an async function as an ``otto run`` subcommand.

    When *options* is a dataclass, the decorator expands its fields (including
    inherited ones) into individual CLI flags — exactly like
    ``@register_suite()`` does for suite options.  The original function must
    declare a parameter annotated with the options class; the decorator
    replaces it with the expanded fields and, at call time, constructs the
    populated dataclass instance before forwarding it to the function.

    Usage without options (unchanged from before)::

        @instruction()
        async def deploy(debug: Annotated[bool, typer.Option()] = False):
            ...

    Usage with an options dataclass::

        @dataclass
        class _Opts(RepoOptions):
            debug: Annotated[bool, typer.Option()] = False

        @instruction(options=_Opts)
        async def deploy(opts: _Opts):
            print(opts.debug)

    The *same* dataclass may be inherited by a suite's inner ``Options``
    class, giving both ``otto test`` and ``otto run`` subcommands a
    uniform set of repo-wide flags.
    """
    def decorator(func: Callable[P, Coroutine[Any, Any, CommandStatus]]) -> Callable[P, CommandStatus]:
        target = func

        if options is not None and dataclasses.is_dataclass(options):
            target = _wrap_with_options(func, options)

        app = typer.Typer()
        new_instruction = app.command(*args, **kwargs)(async_typer_command(target))
        run_app.add_typer(app)
        return new_instruction

    return decorator


def _wrap_with_options(
    func: Callable[..., Any],
    opts_cls: type,
) -> Callable[..., Any]:
    """Build a wrapper that expands an options dataclass into CLI parameters.

    The wrapper:
    1. Accepts the expanded dataclass fields as keyword arguments.
    2. Constructs the dataclass instance from those kwargs.
    3. Forwards it to *func* in the position of the original options parameter.
    """
    sig = inspect.signature(func)
    hints = get_type_hints(func, include_extras=True)

    # Find the parameter annotated with the options class
    opts_param_name: str | None = None
    for name, hint in hints.items():
        if hint is opts_cls:
            opts_param_name = name
            break

    if opts_param_name is None:
        raise TypeError(
            f"instruction {getattr(func, '__name__', repr(func))!r} declares options={opts_cls.__name__} "
            f"but has no parameter annotated as {opts_cls.__name__}"
        )

    # Build new parameter list: replace the opts param with expanded fields
    opts_field_names = {f.name for f in dataclasses.fields(opts_cls)}
    expanded = options_params(opts_cls)

    new_params: list[inspect.Parameter] = []
    for p in sig.parameters.values():
        if p.name == opts_param_name:
            new_params.extend(expanded)
        else:
            # Ensure all params are KEYWORD_ONLY for a consistent Typer signature
            if p.kind != inspect.Parameter.KEYWORD_ONLY:
                p = p.replace(kind=inspect.Parameter.KEYWORD_ONLY)
            new_params.append(p)

    @functools.wraps(func)
    async def wrapper(**kw: Any) -> Any:
        # Split kwargs: dataclass fields vs. remaining params
        opts_kw = {k: kw.pop(k) for k in list(kw) if k in opts_field_names}
        opts_instance = opts_cls(**opts_kw)
        kw[opts_param_name] = opts_instance
        return await func(**kw)

    setattr(wrapper, '__signature__', inspect.Signature(new_params))
    return wrapper
