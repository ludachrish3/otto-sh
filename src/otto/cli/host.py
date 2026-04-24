"""
otto host — run commands, transfer files, and log in to lab hosts.

Commands:
    otto host <host_id> run <commands...>
    otto host <host_id> put <src...> <dest>
    otto host <host_id> get <src...> <dest>
"""

from pathlib import Path
from typing import Annotated, Optional, get_args

import click
import typer
from rich import print as rprint

from ..configmodule import all_hosts, get_host
from ..host.connections import TermType
from ..host.transfer import FileTransferType
from ..logger import getOttoLogger
from ..utils import async_typer_command
from .callbacks import list_hosts_callback

logger = getOttoLogger()


def _host_id_completer(ctx: typer.Context, incomplete: str) -> list[str]:
    """Shell-completion source for the ``host_id`` positional argument.

    Prefers the completion-cache entry populated by the slow path (same file
    that backs suite/instruction completion, wiped by
    ``--clear-autocomplete-cache``). Falls through to a live ``hosts.json``
    scan on cache miss so first-run completion still works.
    """
    from ..configmodule import getCompletionNames, getRepos
    from ..configmodule.completion_cache import collect_host_ids

    cached = getCompletionNames()
    if cached is not None and isinstance(cached.get('hosts'), list):
        ids = cached['hosts']
    else:
        ids = collect_host_ids(getRepos())

    return sorted(h for h in ids if h.startswith(incomplete))

host_app = typer.Typer(
    name='host',
    help='Run commands and transfer files on lab hosts.',
    context_settings={
        'help_option_names': ['-h', '--help'],
    },
)


def _resolve_host(host_id: str):
    try:
        return get_host(host_id)
    except KeyError:
        rprint(f"[red]Error:[/red] No host with ID '{host_id}'.")
        rprint("Available hosts:")
        for h in all_hosts():
            rprint(f"  - {h.id}")
        raise typer.Exit(1)


@host_app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    host_id: Annotated[str, typer.Argument(
        help="Host ID to operate on.",
        autocompletion=_host_id_completer,
    )] = "",
    hop: Annotated[str, typer.Option('--hop', help="Host ID to use as an SSH hop to reach the target.")] = "",
    term: Annotated[Optional[str], typer.Option(
        '--term',
        click_type=click.Choice(list(get_args(TermType))),
        help="Override the terminal protocol for this session.",
    )] = None,
    transfer: Annotated[Optional[str], typer.Option(
        '--transfer',
        click_type=click.Choice(list(get_args(FileTransferType))),
        help="Override the file transfer protocol for this session.",
    )] = None,
    list_hosts: Annotated[bool,
        typer.Option('--list-hosts',
            callback=list_hosts_callback,
            is_eager=True,
            help='Show all valid host IDs.',
        ),
    ] = False,
) -> None:
    if ctx.resilient_parsing:
        return

    if not host_id or ctx.invoked_subcommand is None:
        rprint(ctx.get_help())
        raise typer.Exit()

    logger.create_output_dir("host", f"{ctx.invoked_subcommand}")
    from ..configmodule import tryGetConfigModule
    from ..reservations import gate
    gate(tryGetConfigModule())

    host = _resolve_host(host_id)

    if hop:
        _resolve_host(hop)  # Validate the hop host exists
        host.hop = hop
        host.rebuild_connections()

    if term:
        host.set_term_type(term)

    if transfer:
        host.set_transfer_type(transfer)

    ctx.obj = host


async def _run(
    ctx: typer.Context,
    commands: Annotated[list[str], typer.Argument(help="Commands to execute on the host.")],
) -> None:
    """Execute one or more commands on a remote host."""
    host = ctx.obj
    try:
        result = await host.run(commands)
        if not result.status.is_ok:
            raise typer.Exit(1)
    finally:
        await host.close()


async def _put(
    ctx: typer.Context,
    src: Annotated[list[Path], typer.Argument(help="Local file(s) to upload.")],
    dest: Annotated[Path, typer.Argument(help="Remote destination directory.")],
) -> None:
    """Upload files to a remote host."""
    host = ctx.obj
    try:
        status, msg = await host.put(src, dest)
        if not status.is_ok:
            rprint(f"[red]Transfer failed:[/red] {msg}")
            raise typer.Exit(1)
        rprint(f"[green]Transfer complete.[/green]")
    finally:
        await host.close()


async def _get(
    ctx: typer.Context,
    src: Annotated[list[str], typer.Argument(help="Remote file path(s) to download.")],
    dest: Annotated[Path, typer.Argument(help="Local destination directory.")],
) -> None:
    """Download files from a remote host."""
    host = ctx.obj
    try:
        src_paths = [Path(s) for s in src]
        status, msg = await host.get(src_paths, dest)
        if not status.is_ok:
            rprint(f"[red]Transfer failed:[/red] {msg}")
            raise typer.Exit(1)
        rprint(f"[green]Download complete.[/green]")
    finally:
        await host.close()


async def _login(
    ctx: typer.Context,
) -> None:
    """Open an interactive shell on a remote host.

    Stdin/stdout are bridged to the remote terminal in raw mode, and
    the remote output stream is simultaneously recorded to the normal
    ``otto.log`` for the invocation. Press ``Ctrl+]`` to disconnect
    locally; ``exit``/``logout`` also ends the session normally.
    """
    host = ctx.obj
    try:
        await host.interact()
    finally:
        await host.close()


host_app.command(name="run")(async_typer_command(_run))
host_app.command(name="put")(async_typer_command(_put))
host_app.command(name="get")(async_typer_command(_get))
host_app.command(name="login")(async_typer_command(_login))
