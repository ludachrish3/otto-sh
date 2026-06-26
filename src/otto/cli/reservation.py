"""``otto reservation`` — read-only helpers over the configured reservation backend.

Subcommands:

- ``otto --lab LAB reservation whoami`` — show the resolved identity and backend.
- ``otto --lab LAB reservation check``  — run the reservation check and print a
  human-readable report. Useful as a pre-flight before a long ``otto test``.

Both reuse the top-level ``--lab`` option — no redundant flags here.
"""

import typer
from rich import print as rprint

from ..logger import get_otto_logger
from ..reservations import (
    MissingReservationError,
    check_reservations,
    required_resources,
)

logger = get_otto_logger()

reservation_app = typer.Typer(
    name='reservation',
    no_args_is_help=True,
    help='Inspect and verify lab reservations.',
    context_settings={
        'help_option_names': ['-h', '--help'],
    },
)


@reservation_app.callback()
def reservation_callback(ctx: typer.Context) -> None:
    """Inspect and verify lab reservations."""
    if ctx.resilient_parsing:
        return
    # Mirror run/host/test/cov: set up this invocation's output directory
    # (which also prunes old logs per the retention policy), only for a real
    # subcommand — never on group ``--help``/no-args.
    if ctx.invoked_subcommand is not None:
        logger.create_output_dir('reservation', ctx.invoked_subcommand)


@reservation_app.command()
def whoami(ctx: typer.Context) -> None:
    """Show the resolved reservation identity and backend."""
    from ..configmodule import get_lab
    res = ctx.meta.get("otto_reservation")
    backend = None
    if res is not None:
        backend = res.backend or (
            res.backend_factory() if res.backend_factory else None
        )
    backend_name = backend.backend_name() if backend else "<none>"
    identity = res.identity if res else None
    if identity is None:
        rprint("[yellow]No identity resolved (did the top-level callback run?)[/yellow]")
        raise typer.Exit(1)

    rprint(
        f"username: [bold]{identity.username}[/bold]\n"
        f"source:   {identity.source}\n"
        f"backend:  {backend_name}\n"
        f"lab:      {get_lab().name}"
    )


@reservation_app.command()
def check(ctx: typer.Context) -> None:
    """Run the reservation check for the top-level ``--lab`` and report."""
    from ..configmodule import get_lab
    res = ctx.meta.get("otto_reservation")

    backend = None
    if res is not None:
        backend = res.backend or (
            res.backend_factory() if res.backend_factory else None
        )
    if res is None or backend is None or res.identity is None:
        rprint("[red]Reservation backend or identity not configured.[/red]")
        raise typer.Exit(1)

    lab = get_lab()
    username = res.identity.username
    needed = required_resources(lab)

    rprint(
        f"Checking reservations for [bold]{username}[/bold] "
        f"against lab [bold]{lab.name}[/bold]"
    )
    rprint(f"Required resources: {sorted(needed)}")

    try:
        check_reservations(lab, username, backend)
    except MissingReservationError as e:
        rprint(f"[red]{e}[/red]")
        raise typer.Exit(1) from e

    rprint("[green]OK — all required resources are reserved.[/green]")
