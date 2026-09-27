"""Shared Typer option callbacks used across multiple CLI subapps."""

import typer


def list_hosts_callback(value: bool) -> None:
    """Print all host IDs from the current lab and exit."""
    if not value:
        return
    # Here, not at the top: the lab lives in the fleet module, which imports
    # the host base class, and every command that offers this option imports
    # this module to build its help.
    from ..config.fleet import get_lab

    lab = get_lab()
    typer.echo("")
    for host in lab.hosts:
        typer.echo(f"\u2022 {host}")
    typer.echo("")
