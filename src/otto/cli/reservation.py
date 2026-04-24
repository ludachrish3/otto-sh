"""``otto reservation`` — read-only helpers over the configured reservation backend.

Subcommands:

- ``otto --lab LAB reservation whoami`` — show the resolved identity and backend.
- ``otto --lab LAB reservation check``  — run the reservation check and print a
  human-readable report. Useful as a pre-flight before a long ``otto test``.

Both reuse the top-level ``--lab`` option — no redundant flags here.
"""

import typer
from rich import print as rprint

from ..configmodule import getConfigModule
from ..reservations import (
    MissingReservationError,
    check_reservations,
    required_resources,
)

reservation_app = typer.Typer(
    name='reservation',
    no_args_is_help=True,
    help='Inspect and verify lab reservations.',
    context_settings={
        'help_option_names': ['-h', '--help'],
    },
)


@reservation_app.command()
def whoami() -> None:
    """Show the resolved reservation identity and backend."""
    cm = getConfigModule()
    backend_name = (
        cm.reservation_backend.backend_name()
        if cm.reservation_backend is not None
        else '<none>'
    )
    identity = cm.identity
    if identity is None:
        rprint("[yellow]No identity resolved (did the top-level callback run?)[/yellow]")
        raise typer.Exit(1)

    rprint(
        f"username: [bold]{identity.username}[/bold]\n"
        f"source:   {identity.source}\n"
        f"backend:  {backend_name}\n"
        f"lab:      {cm.lab.name}"
    )


@reservation_app.command()
def check() -> None:
    """Run the reservation check for the top-level ``--lab`` and report."""
    cm = getConfigModule()

    if cm.reservation_backend is None or cm.identity is None:
        rprint("[red]Reservation backend or identity not configured.[/red]")
        raise typer.Exit(1)

    username = cm.identity.username
    needed = required_resources(cm.lab)

    rprint(
        f"Checking reservations for [bold]{username}[/bold] "
        f"against lab [bold]{cm.lab.name}[/bold]"
    )
    rprint(f"Required resources: {sorted(needed)}")

    try:
        check_reservations(cm.lab, username, cm.reservation_backend)
    except MissingReservationError as e:
        rprint(f"[red]{e}[/red]")
        raise typer.Exit(1) from e

    rprint("[green]OK — all required resources are reserved.[/green]")
