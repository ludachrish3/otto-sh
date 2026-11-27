"""Plugin commands registered the way a project registers its own, for the CLI tests.

- ``flash`` (:data:`flash_app`): a group with the leaves ``erase`` and ``verify``
  (two, so Typer keeps it a group), and the sub-group ``image`` with the leaf
  ``write`` (``flash image write``);
- ``burn`` (:func:`burn`): a single command registered by its function;
- ``lazyburn``: a single command registered by the lazy ``"mod:attr"`` loader
  :data:`LAZYBURN`, which names :func:`lazyburn`;
- ``solo`` (:data:`solo_app`): a one-command Typer app, which Typer flattens to
  the bare leaf;
- ``board`` (:data:`board_app`): a group whose callback reads :class:`Acme` and
  which also runs with no child, registered on its own (:func:`registered_board`).

Every one registers ``lab_free``, so a dispatch runs the preamble's gates and
then the body, with no lab. Each leaf is built with :class:`RecordingCommand`, which
appends :func:`~otto.cli.invoke.scope_selection` to :data:`SELECTIONS` every
time click asks it for its parameters: on a dispatch's parse, a ``--help``'s
parse and render, and a completion parse alike, the point where a command's
flags are built. Each body appends ``(maybe_root_options(ctx),
scope_selection(ctx))`` to :data:`CALLS`, its context to :data:`CONTEXTS`, and
what it was handed to :data:`RECEIVED`: its own keyword arguments (the context
aside) and ``otto.get_options(Acme).debug_level``.

:func:`registered` registers all four for a block, empties the lists, and
unregisters on the way out.
"""

import contextlib
import dataclasses
from collections.abc import Generator
from typing import Annotated, Any

import click
import typer
from typer.core import TyperCommand

import otto

SELECTIONS: "list[Any]" = []
"""Every selection a fixture leaf read when its parameters were asked for."""

CALLS: "list[tuple[Any, Any]]" = []
"""``(maybe_root_options(ctx), scope_selection(ctx))`` from each body that ran."""

CONTEXTS: "list[typer.Context]" = []
"""The context of each body that ran, for a test that reads its chain afterwards."""


@otto.options
class Acme:
    """An options class whose one flag reads an environment variable."""

    debug_level: Annotated[int, typer.Option(envvar="ACME_DEBUG", help="Acme's debug level.")] = 0


@dataclasses.dataclass(frozen=True)
class Received:
    """What one fixture body was handed."""

    command: str
    kwargs: "dict[str, Any]"
    """The body's own keyword arguments, the context aside."""
    debug_level: int
    """``otto.get_options(Acme).debug_level`` as the body read it."""


RECEIVED: "list[Received]" = []
"""What each body that ran was handed, in the order they ran."""


class RecordingCommand(TyperCommand):
    """A Typer leaf that records the selection each time its parameters are asked for."""

    def get_params(self, ctx: click.Context) -> "list[click.Parameter]":
        from otto.cli.invoke import scope_selection

        SELECTIONS.append(scope_selection(ctx))
        return super().get_params(ctx)


def _record_call(ctx: typer.Context, command: str, **kwargs: Any) -> None:
    from otto.cli.invoke import maybe_root_options, scope_selection

    CALLS.append((maybe_root_options(ctx), scope_selection(ctx)))
    CONTEXTS.append(ctx)
    RECEIVED.append(Received(command, kwargs, otto.get_options(Acme).debug_level))


flash_app = typer.Typer(name="flash", help="Flash a board (test plugin).")


@flash_app.command("erase", cls=RecordingCommand)
def erase(ctx: typer.Context) -> None:
    """Erase the board."""
    _record_call(ctx, "flash erase")


@flash_app.command("verify", cls=RecordingCommand)
def verify(ctx: typer.Context) -> None:
    """Verify the board."""
    _record_call(ctx, "flash verify")


image_app = typer.Typer(name="image", help="Board images (test plugin).")
flash_app.add_typer(image_app)


@image_app.command("write", cls=RecordingCommand)
def write(ctx: typer.Context, force: bool = False) -> None:
    """Write an image."""
    _record_call(ctx, "flash image write", force=force)


def burn(ctx: typer.Context, loud: bool = False) -> None:
    """Burn the board in (test plugin)."""
    _record_call(ctx, "burn", loud=loud)


def lazyburn(ctx: typer.Context, count: int = 1) -> None:
    """Burn the board in, loaded lazily (test plugin)."""
    _record_call(ctx, "lazyburn", count=count)


LAZYBURN = f"{__name__}:lazyburn"
"""The lazy ``"mod:attr"`` loader ``lazyburn`` is registered with."""


solo_app = typer.Typer(name="solo", help="One command (test plugin).")


@solo_app.command("solo", cls=RecordingCommand)
def solo(ctx: typer.Context) -> None:
    """The one command."""
    _record_call(ctx, "solo")


GROUP_READS: "list[Any]" = []
"""What :data:`board_app`'s callback read of :class:`Acme`: the instance, or the error raised."""


board_app = typer.Typer(
    name="board",
    help="A board's group that also runs alone (test plugin).",
    invoke_without_command=True,
)
"""``board``: a group whose callback reads :class:`Acme`, and runs with no child too.

Its leaves are ``status`` and ``reset``. Not registered by :func:`registered`:
a test that drives it registers it (:func:`registered_board`).
"""


@board_app.callback()
def board(ctx: typer.Context) -> None:
    """The board's group."""
    try:
        GROUP_READS.append(otto.get_options(Acme))
    except Exception as e:  # the pending error is what a test reads back
        GROUP_READS.append(e)
        raise


@board_app.command("status", cls=RecordingCommand)
def status(ctx: typer.Context) -> None:
    """The board's status."""
    _record_call(ctx, "board status")


@board_app.command("reset", cls=RecordingCommand)
def reset(ctx: typer.Context) -> None:
    """Reset the board."""
    _record_call(ctx, "board reset")


@contextlib.contextmanager
def registered_board(*, accepts_options: "bool | None" = None) -> "Generator[None, None, None]":
    """Register ``board`` (lab-free) for the block, the lists emptied, and unregister it after."""
    from otto.cli.registry import CLI_COMMANDS, register_cli_command

    _clear()
    register_cli_command("board", board_app, lab_free=True, accepts_options=accepts_options)
    try:
        yield
    finally:
        CLI_COMMANDS.unregister(name="board")
        _clear()


@contextlib.contextmanager
def registered(
    *, accepts_options: "bool | None" = None, lab_free: bool = True
) -> "Generator[None, None, None]":
    """Register ``flash``, ``burn``, ``lazyburn`` and ``solo`` for the block, the lists emptied.

    *accepts_options* is each registration's own: ``True`` makes the four,
    lab-free as they are, offer the flags of registered options classes.
    *lab_free* ``False`` makes the four use the lab.
    """
    from otto.cli.registry import CLI_COMMANDS, register_cli_command

    loaders = {"flash": flash_app, "burn": burn, "lazyburn": LAZYBURN, "solo": solo_app}
    _clear()
    for name, loader in loaders.items():
        register_cli_command(name, loader, lab_free=lab_free, accepts_options=accepts_options)
    try:
        yield
    finally:
        for name in loaders:
            CLI_COMMANDS.unregister(name=name)
        _clear()


def _clear() -> None:
    for recorded in (SELECTIONS, CALLS, CONTEXTS, RECEIVED, GROUP_READS):
        recorded.clear()
